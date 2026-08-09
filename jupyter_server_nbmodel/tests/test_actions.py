"""Unit tests for execution actions."""

import asyncio

from jupyter_server_nbmodel.actions import dedup_task_queue


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
