from __future__ import annotations

import asyncio
import json
import os
import typing as t
import uuid
from dataclasses import asdict, dataclass
from functools import partial
from http import HTTPStatus

import jupyter_server
import jupyter_server.services
import jupyter_server.services.kernels
import jupyter_server.services.kernels.kernelmanager
import nbformat
import tornado
from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin

from .log import get_logger

if t.TYPE_CHECKING:
    import jupyter_client
    from nbformat import NotebookNode

    try:
        import jupyter_server_ydoc
        import pycrdt as y
        from jupyter_ydoc.ynotebook import YNotebook
    except ImportError:
        # optional dependencies
        ...


@dataclass(frozen=True)
class InputRequest:
    """input_request data"""

    prompt: str
    password: bool


@dataclass(frozen=True)
class InputDescription:
    """Pending input request data"""

    parent_header: dict
    input_request: InputRequest


@dataclass
class PendingInput:
    """Pending input."""

    request_id: str | None = None
    content: InputDescription | None = None

    def is_pending(self) -> bool:
        """Whether a pending input is ongoing or not."""
        return self.request_id is not None

    def clear(self) -> None:
        """Clear pending input."""
        self.request_id = None
        self.content = None


NO_RESULT = object()


# FIXME should we use caching to retrieve faster at least the document
async def _get_ycell(
    ydoc: jupyter_server_ydoc.app.YDocExtension | None,
    metadata: dict | None,
) -> y.Map | None:
    """Get the cell from which the execution was triggered.

    Args:
        ydoc: The YDoc jupyter server extension
        metadata: Execution context
    Returns:
        The cell
    """
    if ydoc is None:
        msg = "jupyter-collaboration extension is not installed on the server. Outputs won't be written within the document."  # noqa: E501
        get_logger().warning(msg)
        return None

    document_id = metadata.get("document_id")
    cell_id = metadata.get("cell_id")

    if document_id is None or cell_id is None:
        msg = (
            "document_id and cell_id not defined. The outputs won't be written within the document."
        )
        get_logger().debug(msg)
        return None

    notebook: YNotebook | None = await ydoc.get_document(room_id=document_id, copy=False)

    if notebook is None:
        msg = f"Document with ID {document_id} not found."
        get_logger().warning(msg)
        return None

    ycells = filter(lambda c: c["id"] == cell_id, notebook.ycells)

    ycell = next(ycells, None)
    if ycell is None:
        msg = f"Cell with ID {cell_id} not found in document {document_id}."
        get_logger().warning(msg)
        return None
    else:
        # Check if there is more than one cell
        if next(ycells, None) is not None:
            get_logger().warning("Multiple cells have the same ID '%s'.", cell_id)

    if ycell["cell_type"] != "code":
        msg = f"Cell with ID {cell_id} of document {document_id} is not of type code."
        get_logger().error(msg)
        raise KeyError(
            msg,
        )

    return ycell


def _output_hook(outputs: list[NotebookNode], ycell: y.Map | None, msg: dict) -> None:
    """Callback on execution request when an output is emitted.

    Args:
        outputs: A list of previously emitted outputs
        ycell: The cell being executed
        msg: The output message
    """
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


def _stdin_hook(kernel_id: str, request_id: str, pending_input: PendingInput, msg: dict) -> None:
    """Callback on stdin message.

    It will register the pending input as temporary answer to the execution request.

    Args:
        kernel_id: The Kernel ID
        request_id: The request ID that triggers the input request
        pending_input: The pending input description.
            This object will be mutated with useful information from ``msg``.
        msg: The stdin msg
    """
    get_logger().debug(f"Execution request {kernel_id} received a input request.")
    if PendingInput.request_id is not None:
        get_logger().error(
            f"Execution request {kernel_id} received a input request while waiting for an input.\n{msg}"  # noqa: E501
        )

    header = msg["header"].copy()
    header["date"] = header["date"].isoformat()
    pending_input.request_id = request_id
    pending_input.content = InputDescription(
        parent_header=header, input_request=InputRequest(**msg["content"])
    )


async def _execute_snippet(
    client: jupyter_client.asynchronous.client.AsyncKernelClient,
    ydoc: jupyter_server_ydoc.app.YDocExtension | None,
    snippet: str,
    metadata: dict | None,
    stdin_hook: t.Callable[[dict], None] | None,
) -> dict[str, t.Any]:
    """Snippet executor

    Args:
        client: Kernel client
        ydoc: Jupyter server YDoc extension
        snippet: The code snippet to execute
        metadata: The code snippet metadata; e.g. to define the snippet context
        stdin_hook: The stdin message callback
    Returns:
        The execution status and outputs.
    """
    ycell = None
    if metadata is not None:
        ycell = await _get_ycell(ydoc, metadata)
        if ycell is not None:
            # Reset cell
            with ycell.doc.transaction():
                del ycell["outputs"][:]
                ycell["execution_count"] = None

    outputs = []

    # FIXME we don't check if the session is consistent (aka the kernel is linked to the document)
    #   - should we?
    reply = await ensure_async(
        client.execute_interactive(
            snippet,
            # FIXME stream partial results
            output_hook=partial(_output_hook, outputs, ycell),
            stdin_hook=stdin_hook if client.allow_stdin else None,
        )
    )

    reply_content = reply["content"]

    if ycell is not None:
        ycell["execution_count"] = reply_content.get("execution_count")

    return {
        "status": reply_content["status"],
        "execution_count": reply_content.get("execution_count"),
        # FIXME quid for buffers
        "outputs": json.dumps(outputs),
    }


async def _kernel_worker(
    kernel_id: str,
    client: jupyter_client.asynchronous.client.AsyncKernelClient,
    ydoc: jupyter_server_ydoc.app.YDocExtension | None,
    queue: asyncio.Queue,
    results: dict,
    pending_input: PendingInput,
) -> None:
    """Process execution request in order for a kernel."""
    get_logger().debug(f"Starting worker to process execution requests of kernel {kernel_id}…")
    to_raise = None
    while True:
        try:
            uid, snippet, metadata = await queue.get()
            get_logger().debug(f"Processing execution request {uid} for kernel {kernel_id}…")
            get_logger().debug("%s %s %s", uid, snippet, metadata)
            client.session.session = uid
            # FIXME
            # client.session.username = username
            results[uid] = await _execute_snippet(
                client, ydoc, snippet, metadata, partial(_stdin_hook, kernel_id, uid, pending_input)
            )

            queue.task_done()
            get_logger().debug(f"Execution request {uid} processed for kernel {kernel_id}.")
        except (asyncio.CancelledError, KeyboardInterrupt, RuntimeError) as e:
            get_logger().debug(
                f"Stopping execution requests worker for kernel {kernel_id}…", exc_info=e
            )
            # Empty the queue
            while not queue.empty():
                queue.task_done()
            to_raise = e
            break
        except BaseException as e:
            get_logger().error(
                f"Failed to process execution request {uid} for kernel {kernel_id}.", exc_info=e
            )
            if not queue.empty():
                queue.task_done()

    if to_raise is not None:
        raise to_raise


class ExecutionStack:
    """Execution request stack.

    It is keeping track of the execution requests.

    The request result can only be queried once.
    """

    def __init__(
        self,
        manager: jupyter_server.services.kernels.kernelmanager.AsyncMappingKernelManager,
        ydoc_extension: jupyter_server_ydoc.app.YDocExtension | None,
    ):
        self.__manager = manager
        self.__ydoc = ydoc_extension
        # Store execution results per kernelID per execution request ID
        self.__execution_results: dict[str, dict[str, t.Any]] = {}
        # Cache kernel clients
        self.__kernel_clients: dict[str, jupyter_client.asynchronous.client.AsyncKernelClient] = {}
        # Store pending input per kernel ID
        self.__pending_inputs: dict[str, PendingInput] = {}
        # Store execution request parameters in order per kernel ID
        self.__tasks: dict[str, asyncio.Queue] = {}
        # Execution request queue worker per kernel ID
        self.__workers: dict[str, asyncio.Task] = {}

    def __del__(self):
        if (
            len(self.__workers)
            + len(self.__tasks)
            + len(self.__kernel_clients)
            + len(self.__pending_inputs)
        ):
            get_logger().warning(
                "Deleting active ExecutionStack. Be sure to call `await ExecutionStack.dispose()`."
            )
            self.dispose()

    async def dispose(self) -> None:
        get_logger().debug("Disposing ExecutionStack…")
        for worker in self.__workers.values():
            worker.cancel()

        for kernel_id, input_ in self.__pending_inputs.items():
            if input_.is_pending():
                await self.send_input(kernel_id, "")
        self.__pending_inputs.clear()
        await asyncio.wait_for(asyncio.gather(w for w in self.__workers.values()), timeout=3)
        self.__workers.clear()

        await asyncio.wait_for(asyncio.gather(q.join() for q in self.__tasks.values()), timeout=3)
        self.__tasks.clear()

        for client in self.__kernel_clients.values():
            client.stop_channels()
        self.__kernel_clients.clear()
        get_logger().debug("ExecutionStack has been disposed.")

    async def cancel(self, kernel_id: str, timeout: float | None = None) -> None:
        """Cancel execution for kernel ``kernel_id``.

        Args:
            kernel_id : Kernel identifier
            timeout: Timeout to await for completion in seconds

        Raises:
            TimeoutError: if a task is not cancelled in time
        """
        # FIXME connect this to kernel lifecycle
        get_logger().debug(f"Cancel execution for kernel {kernel_id}.")
        try:
            worker = self.__workers.pop(kernel_id, None)
            if worker is not None:
                worker.cancel()
                await asyncio.wait_for(worker, timeout=timeout)
        finally:
            try:
                queue = self.__tasks.pop(kernel_id, None)
                if queue is not None:
                    await asyncio.wait_for(queue.join(), timeout=timeout)
            finally:
                client = self.__kernel_clients.pop(kernel_id, None)
                if client is not None:
                    client.stop_channels()

    async def send_input(self, kernel_id: str, value: str) -> None:
        """Send input ``value`` to the kernel ``kernel_id``.

        Args:
            kernel_id : Kernel identifier
            value: Input value
        """
        try:
            client = self._get_client(kernel_id)
        except KeyError as e:
            raise ValueError(f"Unable to find kernel {kernel_id}") from e

        # only send stdin reply if there *was not* another request
        # or execution finished while we were reading.
        if not (await client.stdin_channel.msg_ready() or await client.shell_channel.msg_ready()):
            client.input(value)
            self.__pending_inputs[kernel_id].clear()

    def get(self, kernel_id: str, uid: str) -> t.Any:
        """Get the request ``uid`` results, its pending input or None.

        Args:
            kernel_id : Kernel identifier
            uid : Request identifier

        Returns:
            Any: None if the request is pending else its result or the kernel pending input.

        Raises:
            ValueError: If the request ``uid`` does not exists.
        """
        kernel_results = self.__execution_results.get(kernel_id, {})
        if uid not in kernel_results:
            raise ValueError(f"Execution request {uid} for kernel {kernel_id} does not exists.")

        if self.__pending_inputs[kernel_id].is_pending():
            get_logger().info(f"Kernel '{kernel_id}' has a pending input.")
            # Check the request id is the one matching the appearance of the input
            # Otherwise another cell still looking for its results may capture the
            # pending input
            input_ = self.__pending_inputs[kernel_id]
            if uid == input_.request_id:
                return asdict(input_.content)

        result = kernel_results[uid]
        if result == NO_RESULT:
            return None
        else:
            return kernel_results.pop(uid)

    def put(self, kernel_id: str, snippet: str, metadata: dict | None = None) -> str:
        """Add a asynchronous execution request.

        Args:
            kernel_id: Kernel ID
            snippet: Snippet to be executed
            metadata: [optional] Snippet metadata

        Returns:
            Request identifier
        """
        uid = str(uuid.uuid4())

        if kernel_id not in self.__execution_results:
            self.__execution_results[kernel_id] = {}
        # Make the stack aware a request `uid` exists.
        self.__execution_results[kernel_id][uid] = NO_RESULT
        if kernel_id not in self.__pending_inputs:
            self.__pending_inputs[kernel_id] = PendingInput()
        if kernel_id not in self.__tasks:
            self.__tasks[kernel_id] = asyncio.Queue()

        self.__tasks[kernel_id].put_nowait((uid, snippet, metadata))

        if kernel_id not in self.__workers:
            self.__workers[kernel_id] = asyncio.create_task(
                _kernel_worker(
                    kernel_id,
                    self._get_client(kernel_id),
                    self.__ydoc,
                    self.__tasks[kernel_id],
                    self.__execution_results[kernel_id],
                    self.__pending_inputs[kernel_id],
                )
            )
        return uid

    def _get_client(self, kernel_id: str) -> jupyter_client.asynchronous.client.AsyncKernelClient:
        """Get the cached kernel client for ``kernel_id``.

        Args:
            kernel_id: The kernel ID
        Returns:
            The client for the given kernel.
        """
        if kernel_id not in self.__kernel_clients:
            km = self.__manager.get_kernel(kernel_id)
            self.__kernel_clients[kernel_id] = km.client()

        return self.__kernel_clients[kernel_id]


class ExecuteHandler(ExtensionHandlerMixin, APIHandler):
    """Handle request for snippet execution."""

    def initialize(
        self,
        name: str,
        execution_stack: ExecutionStack,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._execution_stack = execution_stack

    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        """
        Execute a code snippet within the kernel

        Args:
            kernel_id: Kernel ID

        Json Body Required:
            code (str): code to execute
            metadata (dict): [optional]
                document_id (str): Realtime collaboration document unique identifier
                cell_id (str): to-execute cell identifier
        """
        body = self.get_json_body()

        snippet = body.get("code")
        metadata = body.get("metadata", {})

        if kernel_id not in self.kernel_manager:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)

        uid = self._execution_stack.put(kernel_id, snippet, metadata)

        self.set_status(HTTPStatus.ACCEPTED)
        self.set_header("Location", f"/api/kernels/{kernel_id}/requests/{uid}")
        self.finish("{}")


class InputHandler(ExtensionHandlerMixin, APIHandler):
    """Handle request for input reply."""

    def initialize(
        self, name: str, execution_stack: ExecutionStack, *args: t.Any, **kwargs: t.Any
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._stack = execution_stack

    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        """
        Send an input value to kernel ``kernel_id``.

        Args:
            kernel_id: Kernel identifier

        Json Body Required:
            input (str): Input value
        """
        if kernel_id not in self.kernel_manager:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)

        body = self.get_json_body()

        await self._stack.send_input(kernel_id, body["input"])

        self.set_status(HTTPStatus.CREATED)


class RequestHandler(ExtensionHandlerMixin, APIHandler):
    """Handler for /api/kernels/<kernel_id>/requests/<request_id>"""

    def initialize(
        self, name: str, execution_stack: ExecutionStack, *args: t.Any, **kwargs: t.Any
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._stack = execution_stack

    @tornado.web.authenticated
    def get(self, kernel_id: str, request_id: str) -> None:
        """`GET /api/kernels/<kernel_id>/requests/<request_id>` Returns the request ``uid`` status.

        Status are:

        * 200: Request result is returned
        * 202: Request is pending
        * 300: Request has a pending input
        * 500: Request ends with errors

        Args:
            kernel_id: Kernel identifier
            request_id: Request identifier

        Raises:
            404 if request ``request_id`` for ``kernel_id`` does not exist
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
