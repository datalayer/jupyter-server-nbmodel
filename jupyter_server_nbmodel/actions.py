# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio
import json
import os
import threading
import typing as t
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
                    if (not cell_outputs) or (cell_outputs[-1].get("name", None) != output["name"]):
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
    # FIXME we don't check if the session is consistent (aka the kernel is linked to the document)
    #   - should we?
    output_hook = partial(_output_hook, outputs, ycell)
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
            results[uid] = await _execute_snippet(
                client, ydoc, snippet, metadata, partial(_stdin_hook, kernel_id, uid, pending_input)
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
