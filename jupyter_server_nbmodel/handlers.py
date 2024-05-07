from __future__ import annotations

import json
import typing as t
from http import HTTPStatus

import nbformat
import tornado
from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin

from .log import get_logger

if t.TYPE_CHECKING:
    try:
        import pycrdt as y
        import jupyter_server_ydoc
        from jupyter_ydoc.ynotebook import YNotebook
    except ImportError:
        # optional dependencies
        ...


class ExecuteHandler(ExtensionHandlerMixin, APIHandler):
    def initialize(
        self,
        name: str,
        ydoc_extension: "jupyter_server_ydoc.app.YDocExtension" | None,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._ydoc = ydoc_extension
        self._outputs = []
        self._ycell: y.Map | None = None

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
            try:
                self._ycell = next(ycells)
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

            if self._ycell["cell_type"] != "code":
                msg = f"Cell with ID {cell_id} of document {document_id} is not of type code."
                get_logger().error(msg)
                raise tornado.web.HTTPError(
                    status_code=HTTPStatus.BAD_REQUEST,
                    reason=msg,
                )

            snippet = str(self._ycell["source"])

        try:
            km = self.kernel_manager.get_kernel(kernel_id)
        except KeyError as e:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg, exc_info=e)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg) from e

        client = km.client()

        if self._ycell is not None:
            # Reset cell
            del self._ycell["outputs"][:]
            self._ycell["execution_count"] = None

        # FIXME set the username of client.session to server user
        # FIXME we don't check if the session is consistent (aka the kernel is linked to the document) - should we?
        try:
            reply = await ensure_async(
                client.execute_interactive(
                    snippet,
                    output_hook=self._output_hook,
                    stdin_hook=self._stdin_hook if client.allow_stdin else None,
                )
            )

            reply_content = reply["content"]

            if self._ycell is not None:
                self._ycell["execution_count"] = reply_content["execution_count"]

            self.finish(
                {
                    "status": reply_content["status"],
                    "execution_count": reply_content["execution_count"],
                    # FIXME quid for buffers
                    "outputs": json.dumps(self._outputs),
                }
            )

        finally:
            self._outputs.clear()
            self._ycell = None
            del client

    def _output_hook(self, msg) -> None:
        msg_type = msg["header"]["msg_type"]
        if msg_type in ("display_data", "stream", "execute_result", "error"):
            # FIXME support for version
            output = nbformat.v4.output_from_msg(msg)
            get_logger().info("Got an output. %s", output)
            self._outputs.append(output)

            if self._ycell is not None:
                # FIXME support for 'stream'
                outputs = self._ycell["outputs"]
                with outputs.doc.transaction():
                    outputs.append(output)


    def _stdin_hook(self, msg) -> None:
        get_logger().info("Code snippet execution is waiting for an input.")
