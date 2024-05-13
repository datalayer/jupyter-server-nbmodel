from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
import typing as t
import uuid
from functools import partial
from http import HTTPStatus

import nbformat
import tornado
from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin

from .log import get_logger

if t.TYPE_CHECKING:
    import jupyter_client

    try:
        import jupyter_server_ydoc
        import pycrdt as y
        from jupyter_ydoc.ynotebook import YNotebook
    except ImportError:
        # optional dependencies
        ...


class ExecutionStack:
    """Execution request stack.

    The request result can only be queried once.
    """

    def __init__(self):
        self.__pending_inputs: dict[str, dict] = {}
        self.__tasks: dict[str, asyncio.Task] = {}

    def __del__(self):
        for task in filter(lambda t: not t.cancelled(), self.__tasks.values()):
            task.cancel()

    def cancel(self, uid: str) -> None:
        """Cancel the request ``uid``.

        Args:
            uid: Task identifier
        """
        get_logger().debug(f"Cancel request {uid}.")
        if uid not in self.__tasks:
            raise ValueError(f"Request {uid} does not exists.")

        self.__tasks[uid].cancel()

    def get(self, kernel_id: str, uid: str) -> t.Any:
        """Get the request ``uid`` results or None.

        Args:
            kernel_id : Kernel identifier
            uid : Request index

        Returns:
            Any: None if the request is pending else its result

        Raises:
            ValueError: If the request `uid` does not exists.
            asyncio.CancelledError: If the request `uid` was cancelled.
        """
        if uid not in self.__tasks:
            raise ValueError(f"Request {uid} does not exists.")

        if kernel_id in self.__pending_inputs:
            return self.__pending_inputs.pop(kernel_id)

        if self.__tasks[uid].done():
            task = self.__tasks.pop(uid)
            return task.result()
        else:
            return None

    def put(self, km: jupyter_client.manager.KernelManager, snippet: str, ycell: y.Map) -> str:
        """Add a asynchronous execution request.

        Args:
            task: Asynchronous task
            *args : arguments of the task

        Returns:
            Request identifier
        """
        uid = str(uuid.uuid4())

        self.__tasks[uid] = asyncio.create_task(
            execute_task(uid, km, snippet, ycell, partial(self._stdin_hook, km.kernel_id))
        )
        return uid

    def _stdin_hook(self, kernel_id, msg) -> None:
        get_logger().info(f"Execution request {kernel_id} received a input request {msg!s}")
        if kernel_id in self.__pending_inputs:
            get_logger().error(f"Execution request {kernel_id} received a input request while waiting for an input.\n{msg}")

        header = msg["header"].copy()
        header["date"] = header["date"].isoformat()
        self.__pending_inputs[kernel_id] = {"parent_header": header, "input_request": msg["content"]}


async def execute_task(
    uid, km: jupyter_client.manager.KernelManager, snippet: str, ycell: y.Map, stdin_hook
) -> t.Any:
    try:
        get_logger().debug(f"Will execute request {uid}.")
        result = await _execute_snippet(uid, km, snippet, ycell, stdin_hook)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        exception_type, _, tb = sys.exc_info()
        result = {
            "type": exception_type.__qualname__,
            "error": str(e),
            "message": repr(e),
            "traceback": traceback.format_tb(tb),
        }
        get_logger().error("Error for request %s.", result)
    else:
        get_logger().debug(f"Has executed request {uid}.")

    return result


async def _execute_snippet(
    uid: str,
    km: jupyter_client.client.KernelClient,
    snippet: str,
    ycell: y.Map,
    stdin_hook,
) -> dict[str, t.Any]:
    client = km.client()
    client.session.session = uid
    # FIXME
    # client.session.username = username

    if ycell is not None:
        # Reset cell
        del ycell["outputs"][:]
        ycell["execution_count"] = None

    outputs = []

    # FIXME we don't check if the session is consistent (aka the kernel is linked to the document)
    #   - should we?
    try:
        reply = await ensure_async(
            client.execute_interactive(
                snippet,
                output_hook=partial(_output_hook, ycell, outputs),
                stdin_hook=stdin_hook if client.allow_stdin else None,
            )
        )

        reply_content = reply["content"]

        if ycell is not None:
            ycell["execution_count"] = reply_content["execution_count"]

        return {
            "status": reply_content["status"],
            "execution_count": reply_content["execution_count"],
            # FIXME quid for buffers
            "outputs": json.dumps(outputs),
        }
    finally:
        del client


def _output_hook(ycell, outputs, msg) -> None:
    msg_type = msg["header"]["msg_type"]
    if msg_type in ("display_data", "stream", "execute_result", "error"):
        # FIXME support for version
        output = nbformat.v4.output_from_msg(msg)
        outputs.append(output)

        if ycell is not None:
            cell_outputs = ycell["outputs"]
            if msg_type == "stream":
                with cell_outputs.doc.transaction():
                    text = output["text"]

                    # FIXME Logic is quite complex at https://github.com/jupyterlab/jupyterlab/blob/7ae2d436fc410b0cff51042a3350ba71f54f4445/packages/outputarea/src/model.ts#L518
                    if text.endswith((os.linesep, "\n")):
                        text = text[:-1]

                    if (not cell_outputs) or (cell_outputs[-1]["name"] != output["name"]):
                        output["text"] = [text]
                        cell_outputs.append(output)
                    else:
                        last_output = cell_outputs[-1]
                        last_output["text"].append(text)
                        cell_outputs[-1] = last_output
            else:
                with cell_outputs.doc.transaction():
                    cell_outputs.append(output)

    elif msg_type == "clear_output":
        # FIXME msg.content.wait - if true should clear at the next message
        outputs.clear()

        if ycell is not None:
            del ycell["outputs"][:]

    elif msg_type == "update_display_data":
        # FIXME
        ...


class ExecuteHandler(ExtensionHandlerMixin, APIHandler):
    """Handle request for snippet execution."""

    def initialize(
        self,
        name: str,
        ydoc_extension: jupyter_server_ydoc.app.YDocExtension | None,
        execution_stack: ExecutionStack,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._execution_stack = execution_stack
        self._ydoc = ydoc_extension

    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        """
        Execute a code snippet within the kernel

        Args:
            kernel_id: Kernel ID

        Json Body Required:
            code (str): code to execute

            OR

            document_id (str): Realtime collaboration document unique identifier
            cell_id (str):  to-execute cell identifier
        """
        body = self.get_json_body()

        snippet = body.get("code")
        # From RTC model
        if snippet is None:
            document_id = body.get("document_id")
            cell_id = body.get("cell_id")

            if document_id is None or cell_id is None:
                msg = "Either code or document_id and cell_id must be defined in the request body."
                get_logger().error(msg)
                raise tornado.web.HTTPError(
                    status_code=HTTPStatus.BAD_REQUEST,
                    reason=msg,
                )

            if self._ydoc is None:
                msg = "jupyter-collaboration extension is not installed on the server."
                get_logger().error(msg)
                raise tornado.web.HTTPError(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR, reason=msg
                )

            notebook: YNotebook = await self._ydoc.get_document(document_id=document_id, copy=False)

            if notebook is None:
                msg = f"Document with ID {document_id} not found."
                get_logger().error(msg)
                raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)

            ycells = filter(lambda c: c["id"] == cell_id, notebook.ycells)
            ycell = None
            try:
                ycell = next(ycells)
            except StopIteration:
                msg = f"Cell with ID {cell_id} not found in document {document_id}."
                get_logger().error(msg)
                raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)  # noqa: B904
            else:
                # Check if there is more than one cell
                try:
                    next(ycells)
                except StopIteration:
                    get_logger().warning("Multiple cells have the same ID '%s'.", cell_id)

            if ycell["cell_type"] != "code":
                msg = f"Cell with ID {cell_id} of document {document_id} is not of type code."
                get_logger().error(msg)
                raise tornado.web.HTTPError(
                    status_code=HTTPStatus.BAD_REQUEST,
                    reason=msg,
                )

            snippet = str(ycell["source"])

        try:
            km = self.kernel_manager.get_kernel(kernel_id)
        except KeyError as e:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg, exc_info=e)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg) from e

        uid = self._execution_stack.put(km, snippet, ycell)

        self.set_status(HTTPStatus.ACCEPTED)
        self.set_header("Location", f"/api/kernels/{kernel_id}/requests/{uid}")
        self.finish("{}")


class InputHandler(ExtensionHandlerMixin, APIHandler):
    """Handle request for input reply."""

    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        body = self.get_json_body()

        try:
            km = self.kernel_manager.get_kernel(kernel_id)
        except KeyError as e:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg, exc_info=e)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg) from e

        client = km.client()

        try:
            # only send stdin reply if there *was not* another request
            # or execution finished while we were reading.
            if not (await client.stdin_channel.msg_ready() or await client.shell_channel.msg_ready()):
                client.input(body["input"])
        finally:
            del client


class RequestHandler(ExtensionHandlerMixin, APIHandler):
    """Handler for /api/kernels/<kernel_id>/requests/<request_id>"""

    def initialize(
        self, name: str, execution_stack: ExecutionStack, *args: t.Any, **kwargs: t.Any
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._stack = execution_stack

    @tornado.web.authenticated
    def get(self, kernel_id: str, request_id: str) -> None:
        """`GET /api/kernels/<kernel_id>/requests/<id>` Returns the request ``uid`` status.

        Status are:

        * 200: Request result is returned
        * 202: Request is pending
        * 300: Request has a pending input
        * 500: Request ends with errors

        Args:
            index: Request identifier

        Raises:
            404 if request ``uid`` does not exist
        """
        try:
            r = self._stack.get(kernel_id, request_id)
        except ValueError as err:
            raise tornado.web.HTTPError(404, reason=str(err)) from err
        else:
            if r is None:
                self.set_status(202)
                self.finish("{}")
            else:
                if "error" in r:
                    self.set_status(500)
                    self.log.debug(f"{r}")
                elif "input_request" in r:
                    self.set_status(300)
                    self.set_header("Location", f"/api/kernels/{kernel_id}/input")
                else:
                    self.set_status(200)
                self.finish(json.dumps(r))

    @tornado.web.authenticated
    def delete(self, kernel_id: str, request_id: str) -> None:
        """`DELETE /api/kernels/<kernel_id>/requests/<id>` cancels the request ``uid``.

        Status are:
        * 204: Request cancelled

        Args:
            uid: Request uid

        Raises:
            404 if request ``uid`` does not exist
        """
        try:
            self._stack.cancel(request_id)
        except ValueError as err:
            raise tornado.web.HTTPError(404, reason=str(err)) from err
        else:
            self.set_status(204)
            self.finish()
