# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio
import json
import threading
import typing as t
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial

import nbformat
from jupyter_core.utils import ensure_async

from jupyter_server_nbmodel.event_logger import event_logger
from jupyter_server_nbmodel.log import get_logger
from jupyter_server_nbmodel.models import (
    InputDescription,
    InputRequest,
    PendingInput,
)

if t.TYPE_CHECKING:
    from nbformat import NotebookNode

    try:
        import jupyter_server_ydoc
        import pycrdt as y
        from jupyter_ydoc.ynotebook import YNotebook
    except ImportError:
        # optional dependencies
        ...


# FIXME should we use caching to retrieve faster at least the document.
async def _get_ycell(
    ydoc: jupyter_server_ydoc.app.YDocExtension | None,
    metadata: dict | None,
) -> y.Map | None:
    """Get the cell from which the execution was triggered.

    Args:
        ydoc: The YDoc jupyter server extension
        metadata: Execution context, including the notebook path and cell ID
    Returns:
        The cell
    """
    if ydoc is None:
        msg = "jupyter-collaboration extension is not installed on the server. Outputs won't be written within the document."  # noqa: E501
        get_logger().warning(msg)
        return None
    document_id = metadata.get("document_id")
    document_path = metadata.get("document_path")
    cell_id = metadata.get("cell_id")
    if (document_id is None and document_path is None) or cell_id is None:
        msg = (
            "Neither document_path nor document_id is defined with cell_id. "
            "The outputs won't be written within the document."
        )
        get_logger().debug(msg)
        return None
    notebook: YNotebook | None = None
    if isinstance(document_path, str) and document_path:
        # The document_id is collaborative state and may refer to a room from
        # before a server restart. Resolve the current live room from the file
        # path first so updates reach the browser that is actually connected.
        notebook = await ydoc.get_document(
            path=document_path,
            content_type="notebook",
            file_format="json",
            copy=False,
        )
    if notebook is None and document_id is not None:
        notebook = await ydoc.get_document(room_id=document_id, copy=False)
    if notebook is None:
        msg = f"Document at path {document_path!r} with ID {document_id!r} not found."
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


@dataclass
class StreamState:
    """The stream this hook is writing into a cell, as the server sees it.

    The kernel sends the raw bytes a terminal would receive, control
    characters included, so the text of the cell is not their concatenation:
    it is what a terminal would be showing. Holding the result here — rather
    than deriving it from the cell — keeps it the authority on what the kernel
    printed, whoever else writes to the shared document.
    """

    #: Name of the stream, ``stdout`` or ``stderr``.
    name: str = ""
    #: What the stream shows, control characters applied.
    text: str = ""
    #: Where the next character lands in ``text``.
    cursor: int = 0

    def reset(self, name: str = "", text: str = "") -> None:
        self.name = name
        self.text = text
        self.cursor = len(text)


def _apply_terminal_controls(text: str, new_text: str, cursor: int) -> tuple[str, int]:
    """Write ``new_text`` at ``cursor``, applying ``\\b``, ``\\r`` and ``\\n``.

    Mirrors ``Private.processText`` of JupyterLab, in
    ``packages/outputarea/src/model.ts``, so that a cell executed on the server
    shows what the same cell executed in the browser would show — a progress
    bar overwriting its line rather than one line per update.

    Args:
        text: What the stream shows so far
        new_text: The text the kernel just emitted
        cursor: Where in ``text`` the new text starts landing

    Returns:
        The text the stream now shows, and the new cursor
    """
    chars = list(text)

    # A control character left at the end of the text is a cursor made
    # durable: whoever stored that text — a notebook saved mid-stream, or the
    # browser applying an HTTP snapshot — could not store the cursor with it.
    # Put it back in front of the new text and let the loop below apply it.
    if chars and chars[-1] in ("\b", "\r"):
        new_text = chars.pop() + new_text
        cursor = min(max(cursor - 1, 0), len(chars))

    for char in new_text:
        match char:
            case "\b":
                if cursor > 0 and chars[cursor - 1] != "\n":
                    del chars[cursor - 1]
                    cursor -= 1
            case "\r":
                while cursor > 0 and chars[cursor - 1] != "\n":
                    cursor -= 1
            case "\n":
                chars.append("\n")
                cursor = len(chars)
            case _:
                if cursor < len(chars):
                    chars[cursor] = char
                else:
                    chars.append(char)
                cursor += 1

    return "".join(chars), cursor


def _stream_text(value: t.Any) -> str:
    """The text of a stream output, however the shared document holds it."""
    if isinstance(value, list):
        return "".join(value)
    return str(value) if value else ""


def _common_prefix_length(left: str, right: str) -> int:
    """How many leading characters the two texts share."""
    shared = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        shared += 1
    return shared


def _output_hook(
    outputs: list[NotebookNode],
    ycell: y.Map | None,
    stream_state: StreamState,
    msg: dict,
) -> None:
    """Callback on execution request when an output is emitted.

    Args:
        outputs: A list of previously emitted outputs
        ycell: The cell being executed
        stream_state: The stream being written, across the messages of one execution
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
                from pycrdt import Map, Text

                last_output = cell_outputs[-1] if len(cell_outputs) else None
                if last_output is not None and last_output.get("name") != output["name"]:
                    last_output = None

                if last_output is None:
                    stream_state.reset(output["name"])
                elif stream_state.name != output["name"]:
                    # The cell already carries this stream although this hook
                    # has not written it: a resumed execution, or the browser
                    # recovery. Continue that text instead of starting a
                    # second output beside it.
                    stream_state.reset(output["name"], _stream_text(last_output["text"]))

                stream_state.text, stream_state.cursor = _apply_terminal_controls(
                    stream_state.text, output["text"], stream_state.cursor
                )
                expected_text = stream_state.text

                if last_output is None:
                    with cell_outputs.doc.transaction():
                        youtput = dict(output)
                        youtput["text"] = Text(expected_text)
                        cell_outputs.append(Map(youtput))
                    return

                # The browser fallback may already have inserted this snapshot
                # in the shared document, in which case there is nothing to do
                # rather than a second append.
                previous_text = last_output["text"]
                actual_text = _stream_text(previous_text)
                if actual_text == expected_text or actual_text.startswith(expected_text):
                    return

                if isinstance(previous_text, Text):
                    # Only what changed: a stream that grows is appended to,
                    # and one that a carriage return rewrote has its tail
                    # replaced. Never the whole value — restructuring a nested
                    # Yjs value is what once left a notebook empty.
                    shared = _common_prefix_length(actual_text, expected_text)
                    with cell_outputs.doc.transaction():
                        if shared < len(actual_text):
                            del previous_text[shared:]
                        previous_text += expected_text[shared:]
                else:
                    with cell_outputs.doc.transaction():
                        last_output["text"] = Text(expected_text)
            else:
                # Any other output ends the stream; the next one starts a new
                # output rather than writing into this one.
                stream_state.reset()
                # Pending HTTP reconciliation may have inserted this complete
                # non-stream output before the YDoc update reaches the server.
                # Avoid persisting the same execute_result/error twice.
                current = cell_outputs[-1] if cell_outputs else None
                current_value = current.to_py() if hasattr(current, "to_py") else current
                if current_value != dict(output):
                    with cell_outputs.doc.transaction():
                        cell_outputs.append(output)
    elif msg_type == "clear_output":
        # FIXME msg.content.wait - if true should clear at the next message
        outputs.clear()
        stream_state.reset()
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
    header["date"] = (
        header["date"] if isinstance(header["date"], str) else header["date"].isoformat()
    )
    pending_input.request_id = request_id
    pending_input.content = InputDescription(
        parent_header=header, input_request=InputRequest(**msg["content"])
    )


def _get_error(outputs):
    return "\n".join(
        f"{output['ename']}: {output['evalue']}"
        for output in outputs
        if output.get("output_type") == "error"
    )


def _threadsafe_hook(
    loop: asyncio.AbstractEventLoop, callback: t.Callable[[dict], None] | None
) -> t.Callable[[dict], None] | None:
    """Run a synchronous remote-client hook safely on the server event loop."""
    if callback is None:
        return None

    def hook(message: dict) -> None:
        completed = threading.Event()
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                callback(message)
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()

        loop.call_soon_threadsafe(invoke)
        completed.wait()
        if errors:
            raise errors[0]

    return hook


async def _run_in_thread(callback: t.Callable[[], t.Any]) -> t.Any:
    """Run a blocking callback without relying on the global asyncio executor."""
    result: list[t.Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(callback())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.01)
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


async def _execute_snippet(
    client: t.Any,
    ydoc: jupyter_server_ydoc.app.YDocExtension | None,
    snippet: str,
    metadata: dict | None,
    stdin_hook: t.Callable[[dict], None] | None,
    progress: dict[str, t.Any] | None = None,
) -> dict[str, t.Any]:
    """Snippet executor

    Args:
        client: Kernel client
        ydoc: Jupyter server YDoc extension
        snippet: The code snippet to execute
        metadata: The code snippet metadata; e.g. to define the snippet context
        stdin_hook: The stdin message callback
        progress: Mutable execution progress exposed by the request status endpoint
    Returns:
        The execution status and outputs.
    """
    ycell = None
    time_info = {}
    if metadata is not None:
        ycell = await _get_ycell(ydoc, metadata)
        if ycell is not None:
            execution_start_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            # Reset cell
            with ycell.doc.transaction():
                del ycell["outputs"][:]
                ycell["execution_count"] = None
                ycell["execution_state"] = "running"
                if "execution" in ycell["metadata"]:
                    del ycell["metadata"]["execution"]
                request_id = metadata.get("request_id")
                kernel_id = metadata.get("kernel_id")
                if request_id and kernel_id:
                    ycell["metadata"]["jupyter_server_nbmodel"] = {
                        "requestId": request_id,
                        "kernelId": kernel_id,
                        "requestUrl": f"/api/kernels/{kernel_id}/requests/{request_id}",
                    }
                if metadata.get("record_timing", False):
                    time_info = ycell["metadata"].get("execution", {})
                    time_info["shell.execute_reply.started"] = execution_start_time
                    # for compatibility with jupyterlab-execute-time also set:
                    time_info["iopub.execute_input"] = execution_start_time
                    ycell["metadata"]["execution"] = time_info
            # Emit cell execution start event
            event_logger.emit(
                schema_id="https://events.jupyter.org/jupyter_server_nbmodel/cell_execution/v1",
                data={
                    "event_type": "execution_start",
                    "cell_id": metadata["cell_id"],
                    "document_id": metadata["document_id"],
                    "timestamp": execution_start_time,
                },
            )
    outputs = []
    if progress is not None:
        # Keep the same list throughout execution so every output hook is
        # immediately visible to request polling without copying kernel
        # messages or waiting for the final execute reply.
        progress["outputs"] = outputs
    # FIXME we don't check if the session is consistent (aka the kernel is linked to the document)
    #   - should we?
    output_hook = partial(_output_hook, outputs, ycell, StreamState())
    if not getattr(client, "_server_nbmodel_remote", False):
        reply = await ensure_async(
            client.execute_interactive(
                snippet,
                output_hook=output_hook,
                stdin_hook=stdin_hook if client.allow_stdin else None,
            )
        )
    else:
        # Remote WebSocket clients are synchronous. Run their blocking receive
        # loop in a worker thread while applying YDoc updates on the main loop.
        loop = asyncio.get_running_loop()
        reply = await _run_in_thread(
            partial(
                client.execute_interactive,
                snippet,
                output_hook=_threadsafe_hook(loop, output_hook),
                stdin_hook=_threadsafe_hook(loop, stdin_hook if client.allow_stdin else None),
            )
        )
    reply_content = reply["content"]
    if ycell is not None:
        execution_end_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with ycell.doc.transaction():
            ycell["execution_count"] = reply_content.get("execution_count")
            ycell["execution_state"] = "idle"
            if "jupyter_server_nbmodel" in ycell["metadata"]:
                del ycell["metadata"]["jupyter_server_nbmodel"]
            if metadata and metadata.get("record_timing", False):
                if reply_content["status"] == "ok":
                    time_info["shell.execute_reply"] = execution_end_time
                else:
                    time_info["execution_failed"] = execution_end_time
                ycell["metadata"]["execution"] = time_info
        # Emit cell execution end event
        event_logger.emit(
            schema_id="https://events.jupyter.org/jupyter_server_nbmodel/cell_execution/v1",
            data={
                "event_type": "execution_end",
                "cell_id": metadata["cell_id"],
                "document_id": metadata["document_id"],
                "success": reply_content["status"] == "ok",
                "kernel_error": _get_error(outputs),
                "timestamp": execution_end_time,
            },
        )
    return {
        "status": reply_content["status"],
        "execution_count": reply_content.get("execution_count"),
        # FIXME quid for buffers
        "outputs": json.dumps(outputs),
    }


async def dedup_task_queue(q: asyncio.Queue) -> list[str]:
    """
    Deduplicate tasks in an asyncio.Queue by keeping only the last
    submitted task per cell_id.

    Problem:
        After "Restart Kernel and Run All Cells", tasks from the previous
        run may still remain in the queue. This causes duplicate tasks
        for the same cell_id to exist. When consumed, both the old and
        new tasks run, leading to duplicated execution.

    Solution:
        This function drains the queue, keeps only the last occurrence
        of each cell_id, and puts those tasks back into the queue in the
        correct order. That way, each cell_id has only one pending task.
    """
    if q.empty():
        return []

    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except asyncio.QueueEmpty:
            break

    # Keep items without cell_id and only the latest one for duplicated cell_id.
    last_idx: dict[str, int] = {}
    for i, item in enumerate(items):
        metadata = item[2] if len(item) > 2 else None
        cell_id = metadata.get("cell_id") if isinstance(metadata, dict) else None
        if isinstance(cell_id, str):
            last_idx[cell_id] = i

    keep_indices: set[int] = set()
    for i, item in enumerate(items):
        metadata = item[2] if len(item) > 2 else None
        cell_id = metadata.get("cell_id") if isinstance(metadata, dict) else None
        if isinstance(cell_id, str):
            if last_idx.get(cell_id) == i:
                keep_indices.add(i)
        else:
            keep_indices.add(i)

    dropped_uids: list[str] = []
    for i, item in enumerate(items):
        # Balance the original put before optionally re-queuing the item.
        q.task_done()
        if i in keep_indices:
            await q.put(item)
        else:
            dropped_uids.append(item[0])

    return dropped_uids


async def kernel_worker(
    kernel_id: str,
    client: t.Any,
    ydoc: jupyter_server_ydoc.app.YDocExtension | None,
    queue: asyncio.Queue,
    results: dict,
    pending_input: PendingInput,
) -> None:
    """Process execution request in order for a kernel."""
    get_logger().debug(f"Starting worker to process execution requests of kernel {kernel_id}…")
    to_raise = None
    while True:
        uid = None
        try:
            uid, snippet, metadata = await queue.get()
            for dropped_uid in await dedup_task_queue(queue):
                results[dropped_uid] = {
                    "error": "Superseded by a newer queued execution for the same cell_id."
                }
            get_logger().debug(f"Processing execution request {uid} for kernel {kernel_id}…")
            get_logger().debug("%s %s %s", uid, snippet, metadata)
            # FIXME
            # client.session.username = username
            from jupyter_server.gateway.managers import GatewayKernelClient

            if isinstance(client, GatewayKernelClient) and client.channel_socket is None:
                get_logger().debug(f"start channels {kernel_id}")
                await client.start_channels()
            progress = {"pending": True, "outputs": []}
            results[uid] = progress
            execution_metadata = dict(metadata or {})
            execution_metadata.update({"request_id": uid, "kernel_id": kernel_id})
            results[uid] = await _execute_snippet(
                client,
                ydoc,
                snippet,
                execution_metadata,
                partial(_stdin_hook, kernel_id, uid, pending_input),
                progress,
            )
            get_logger().debug(f"Execution request {uid} processed for kernel {kernel_id}.")

            # stop other tasks if one hits error
            if results[uid]["status"] == "error":
                while not queue.empty():
                    dropped_uid, _, _ = queue.get_nowait()
                    results[dropped_uid] = {
                        "error": "Execution cancelled because a previous queued request failed."
                    }
                    queue.task_done()
        except (asyncio.CancelledError, KeyboardInterrupt, RuntimeError) as e:
            if uid is not None:
                results[uid] = {"error": str(e)}
            get_logger().debug(
                f"Stopping execution requests worker for kernel {kernel_id}…", exc_info=e
            )
            # Empty the queue
            while not queue.empty():
                dropped_uid, _, _ = queue.get_nowait()
                results[dropped_uid] = {"error": "Execution cancelled."}
                queue.task_done()
            to_raise = e
            break
        except BaseException as e:
            if uid is not None:
                results[uid] = {"error": str(e)}
            get_logger().error(
                f"Failed to process execution request {uid} for kernel {kernel_id}.", exc_info=e
            )
        finally:
            if uid is not None:
                queue.task_done()
    if to_raise is not None:
        raise to_raise
