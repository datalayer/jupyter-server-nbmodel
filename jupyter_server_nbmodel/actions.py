# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio
import json
import os
import typing as t

from functools import partial
from datetime import datetime, timezone

import nbformat

from jupyter_core.utils import ensure_async

from jupyter_server_nbmodel.models import (
    PendingInput,
    InputDescription,
    InputRequest,
)
from jupyter_server_nbmodel.log import get_logger
from jupyter_server_nbmodel.event_logger import event_logger


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


def handle_carriage_return(s: str) -> str:
    """Handle text the same way that a terminal emulator would display it

    Args:
        s: The message
    Returns:
        Message with carriage returns handled in the text
    """
    lines = s.split('\n')
    processed_lines = []

    for line in lines:
        result = []
        i = 0
        while i < len(line):
            if line[i] == '\r':
                # Move cursor to start of the line
                # Reset the result buffer and prepare to overwrite
                i += 1
                overwrite_chars = []
                while i < len(line) and line[i] != '\r':
                    overwrite_chars.append(line[i])
                    i += 1
                for j, c in enumerate(overwrite_chars):
                    if j < len(result):
                        result[j] = c
                    else:
                        result.append(c)
            else:
                result.append(line[i])
                i += 1
        processed_lines.append(''.join(result))

    return '\n'.join(processed_lines)


def handle_backspace(s: str) -> str:
    """Simulate backspaces in the text

    Args:
        s: The message
    Returns:
        The message with backspaces applied
    """
    new_str = []
    for c in s:
        if c == '\b':
            if len(new_str) > 0 and new_str[-1] not in ('\n', '\r'):
                new_str.pop()
        else:
            new_str.append(c)
    return ''.join(new_str)


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

                    if text.endswith((os.linesep, "\n")):
                        text = text[:-1]

                    if (not cell_outputs) or (cell_outputs[-1]["name"] != output["name"]):
                        output["text"] = [handle_carriage_return(handle_backspace(text))]
                        cell_outputs.append(output)
                    else:
                        last_output = cell_outputs[-1]
                        old_text = last_output["text"][-1] if len(last_output["text"]) > 0 else ""
                        combined_text = old_text + text
                        if '\r' in combined_text or '\b' in combined_text:
                            if combined_text[-1] == '\r':
                                suffix = '\r'
                                combined_text = combined_text[:-1]
                            else:
                                suffix = ''
                            new_text = handle_carriage_return(handle_backspace(combined_text)) + suffix
                            last_output["text"][-1] = new_text
                        else:
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
    header["date"] = header["date"] if isinstance(header["date"], str) else header["date"].isoformat()
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
    time_info = {}
    if metadata is not None:
        ycell = await _get_ycell(ydoc, metadata)
        if ycell is not None:
            execution_start_time = datetime.now(timezone.utc).isoformat()[:-6]
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
                    "timestamp": execution_start_time
                }
            )
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
        execution_end_time = datetime.now(timezone.utc).isoformat()[:-6]
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
                "success": reply_content["status"]=="ok",
                "kernel_error": _get_error(outputs),
                "timestamp": execution_end_time
            }
        )
    return {
        "status": reply_content["status"],
        "execution_count": reply_content.get("execution_count"),
        # FIXME quid for buffers
        "outputs": json.dumps(outputs),
    }


async def kernel_worker(
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
            from jupyter_server.gateway.managers import GatewayKernelClient
            if isinstance(client, GatewayKernelClient) and client.channel_socket is None:
                get_logger().debug(f"start channels {kernel_id}")
                await client.start_channels()
            results[uid] = await _execute_snippet(
                client, ydoc, snippet, metadata, partial(_stdin_hook, kernel_id, uid, pending_input)
            )
            queue.task_done()
            get_logger().debug(f"Execution request {uid} processed for kernel {kernel_id}.")
        except (asyncio.CancelledError, KeyboardInterrupt, RuntimeError) as e:
            results[uid] = {"error": str(e)}
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
