"""Unit tests for execution actions."""

import asyncio
from contextlib import nullcontext

from jupyter_server_nbmodel.actions import _output_hook, dedup_task_queue


class _Outputs(list):
    """Minimal shared output array used to exercise streaming persistence."""

    class _Doc:
        @staticmethod
        def transaction():
            return nullcontext()

    doc = _Doc()


def _stream_message(text: str) -> dict:
    return {
        "header": {"msg_type": "stream"},
        "content": {"name": "stdout", "text": text},
    }


def test_output_hook_preserves_stream_newlines() -> None:
    """Merged stream chunks retain separators when the notebook is reloaded."""
    emitted = []
    persisted = _Outputs()
    ycell = {"outputs": persisted}

    _output_hook(emitted, ycell, _stream_message("1\n"))
    _output_hook(emitted, ycell, _stream_message("2\n"))

    assert persisted == [
        {"output_type": "stream", "name": "stdout", "text": "1\n2\n"}
    ]
    assert persisted[0]["text"] == "1\n2\n"


async def test_dedup_task_queue_empty():
    """An empty queue does not contain superseded requests."""
    queue = asyncio.Queue()

    assert await dedup_task_queue(queue) == []


async def test_dedup_task_queue_keeps_latest_cell_request():
    """Only older requests for the same string cell ID are discarded."""
    queue = asyncio.Queue()
    items = [
        ("first", "old", {"cell_id": "cell-1"}),
        ("without-cell", "unkeyed", None),
        ("latest", "new", {"cell_id": "cell-1"}),
        ("non-string-cell", "other", {"cell_id": 1}),
    ]
    for item in items:
        await queue.put(item)

    dropped = await dedup_task_queue(queue)

    assert dropped == ["first"]
    remaining = []
    while not queue.empty():
        remaining.append(queue.get_nowait())
        queue.task_done()
    assert remaining == items[1:]
    await asyncio.wait_for(queue.join(), timeout=1)
