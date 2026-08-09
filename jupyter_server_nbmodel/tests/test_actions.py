"""Unit tests for execution actions."""

import asyncio

from pycrdt import Array, Doc, Text

from jupyter_server_nbmodel.actions import _output_hook, dedup_task_queue


def _stream_message(text: str) -> dict:
    return {
        "header": {"msg_type": "stream"},
        "content": {"name": "stdout", "text": text},
    }


def test_output_hook_preserves_stream_newlines() -> None:
    """Merged stream chunks retain separators when the notebook is reloaded."""
    emitted = []
    doc = Doc()
    persisted = doc.get("outputs", type=Array)
    ycell = {"outputs": persisted}

    _output_hook(emitted, ycell, _stream_message("1\n"))
    _output_hook(emitted, ycell, _stream_message("2\n"))

    assert len(persisted) == 1
    assert persisted[0]["output_type"] == "stream"
    assert persisted[0]["name"] == "stdout"
    assert isinstance(persisted[0]["text"], Text)
    assert str(persisted[0]["text"]) == "1\n2\n"


def test_output_hook_appends_to_reloaded_stream_text() -> None:
    """A stream loaded as collaborative Text remains writable after restart."""
    original_doc = Doc()
    original_outputs = original_doc.get("outputs", type=Array)
    _output_hook([], {"outputs": original_outputs}, _stream_message("before restart\n"))

    reloaded_doc = Doc()
    reloaded_doc.apply_update(original_doc.get_update())
    reloaded_outputs = reloaded_doc.get("outputs", type=Array)
    peer_state = original_doc.get_state()

    _output_hook([], {"outputs": reloaded_outputs}, _stream_message("after restart\n"))
    original_doc.apply_update(reloaded_doc.get_update(peer_state))

    assert str(reloaded_outputs[0]["text"]) == "before restart\nafter restart\n"
    assert str(original_outputs[0]["text"]) == "before restart\nafter restart\n"


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
