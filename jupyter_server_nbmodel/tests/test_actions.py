"""Unit tests for execution actions."""

import asyncio

from pycrdt import Array, Doc, Text

from jupyter_server_nbmodel.actions import (
    _apply_terminal_controls,
    _output_hook,
    _StreamState,
    dedup_task_queue,
)


def _stream_message(text: str) -> dict:
    return {
        "header": {"msg_type": "stream"},
        "content": {"name": "stdout", "text": text},
    }


def test_output_hook_preserves_stream_newlines() -> None:
    """Merged stream chunks retain separators when the notebook is reloaded."""
    emitted = []
    state = _StreamState()
    doc = Doc()
    persisted = doc.get("outputs", type=Array)
    ycell = {"outputs": persisted}

    _output_hook(emitted, ycell, state, _stream_message("1\n"))
    _output_hook(emitted, ycell, state, _stream_message("2\n"))

    assert len(persisted) == 1
    assert persisted[0]["output_type"] == "stream"
    assert persisted[0]["name"] == "stdout"
    assert isinstance(persisted[0]["text"], Text)
    assert str(persisted[0]["text"]) == "1\n2\n"


def test_output_hook_appends_to_reloaded_stream_text() -> None:
    """A stream loaded as collaborative Text remains writable after reload."""
    emitted = []
    state = _StreamState()
    original_doc = Doc()
    original_outputs = original_doc.get("outputs", type=Array)
    _output_hook(emitted, {"outputs": original_outputs}, state, _stream_message("before reload\n"))

    reloaded_doc = Doc()
    reloaded_doc.apply_update(original_doc.get_update())
    reloaded_outputs = reloaded_doc.get("outputs", type=Array)
    peer_state = original_doc.get_state()

    _output_hook(emitted, {"outputs": reloaded_outputs}, state, _stream_message("after reload\n"))
    original_doc.apply_update(reloaded_doc.get_update(peer_state))

    assert str(reloaded_outputs[0]["text"]) == "before reload\nafter reload\n"
    assert str(original_outputs[0]["text"]) == "before reload\nafter reload\n"


def test_output_hook_does_not_append_browser_snapshot_twice() -> None:
    """A snapshot already inserted by polling remains unchanged by the hook."""
    emitted = []
    state = _StreamState()
    doc = Doc()
    persisted = doc.get("outputs", type=Array)
    ycell = {"outputs": persisted}

    _output_hook(emitted, ycell, state, _stream_message("1\n"))
    # Simulate the browser fallback applying the next accumulated snapshot
    # before the matching collaborative hook is integrated locally.
    persisted[0]["text"] += "2\n"
    _output_hook(emitted, ycell, state, _stream_message("2\n"))

    assert len(persisted) == 1
    assert str(persisted[0]["text"]) == "1\n2\n"


def test_apply_terminal_controls_backspace_and_carriage_return() -> None:
    """Terminal control processing with ``\\b`` and ``\\r`` across messages.

    Simulates::

        print('1110\\b', end='', flush=True)  # "111" (backspace deletes '0')
        print('11', end='', flush=True)       # "11111"
        print('\\r2 ', end='', flush=True)    # "2 111" (CR + overwrite)
        print('3', end='', flush=True)        # "2 311"
        print('4')                            # "2 341\\n"
    """
    text, cursor = _apply_terminal_controls("", "1110\b", 0)
    assert (text, cursor) == ("111", 3)

    text, cursor = _apply_terminal_controls(text, "11", cursor)
    assert (text, cursor) == ("11111", 5)

    text, cursor = _apply_terminal_controls(text, "\r2 ", cursor)
    assert (text, cursor) == ("2 111", 2)

    text, cursor = _apply_terminal_controls(text, "3", cursor)
    assert (text, cursor) == ("2 311", 3)

    text, cursor = _apply_terminal_controls(text, "4\n", cursor)
    assert (text, cursor) == ("2 341\n", 6)


def test_apply_terminal_controls_carriage_return_stays_on_its_line() -> None:
    """A carriage return returns to the start of the line, not of the text.

    And it overwrites that line rather than clearing it, so what the shorter
    new text does not reach stays — the trailing ``d`` of ``second`` here.
    """
    text, cursor = _apply_terminal_controls("", "first\nsecond\rthird", 0)
    assert (text, cursor) == ("first\nthirdd", 11)


def test_apply_terminal_controls_resumes_a_pending_control_character() -> None:
    """Text ending in ``\\r`` or ``\\b`` carries the cursor of whoever stored it.

    The hook never leaves one there, but a notebook saved mid-stream and the
    HTTP snapshot the browser applies both can, and the cursor does not travel
    with the text.
    """
    text, cursor = _apply_terminal_controls("100%\r", "done", len("100%\r"))
    assert (text, cursor) == ("done", 4)

    text, cursor = _apply_terminal_controls("ab\b", "c", len("ab\b"))
    assert (text, cursor) == ("ac", 2)

    # The line it returns to is the last one, not the first: resuming at the
    # start of the text would have written "third" over "first".
    text, cursor = _apply_terminal_controls("first\nsecond\r", "third", len("first\nsecond\r"))
    assert (text, cursor) == ("first\nthirdd", 11)


def test_output_hook_overwrites_a_progress_line_in_place() -> None:
    """A progress bar rewrites its line instead of adding one per update.

    This is https://github.com/datalayer/jupyter-server-nbmodel/issues/39: a
    ``tqdm`` bar arrives as one stream message per update, each starting with a
    carriage return.
    """
    emitted = []
    state = _StreamState()
    doc = Doc()
    persisted = doc.get("outputs", type=Array)
    ycell = {"outputs": persisted}

    _output_hook(emitted, ycell, state, _stream_message("\r 10%|#         | 10/100"))
    _output_hook(emitted, ycell, state, _stream_message("\r 50%|#####     | 50/100"))
    _output_hook(emitted, ycell, state, _stream_message("\r100%|##########| 100/100"))

    assert len(persisted) == 1
    assert str(persisted[0]["text"]) == "100%|##########| 100/100"


def test_output_hook_keeps_the_stream_of_a_resumed_execution() -> None:
    """A cell already carrying the stream is continued, not duplicated."""
    emitted = []
    doc = Doc()
    persisted = doc.get("outputs", type=Array)
    ycell = {"outputs": persisted}

    _output_hook(emitted, ycell, _StreamState(), _stream_message("first\n"))
    # A second execution hook, with no memory of what the cell already shows.
    _output_hook(emitted, ycell, _StreamState(), _stream_message("second\n"))

    assert len(persisted) == 1
    assert str(persisted[0]["text"]) == "first\nsecond\n"


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
