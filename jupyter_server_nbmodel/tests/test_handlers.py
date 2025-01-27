import asyncio
import datetime
import json
import re
import nbformat

import pytest
from jupyter_client.kernelspec import NATIVE_KERNEL_NAME

TEST_TIMEOUT = 15
SLEEP = 0.25


REQUEST_REGEX = re.compile(r"^/api/kernels/\w+-\w+-\w+-\w+-\w+/requests/\w+-\w+-\w+-\w+-\w+$")


async def _wait_request(fetch, endpoint: str):
    """Poll periodically to fetch the execution request result."""
    start_time = datetime.datetime.now()
    elapsed = 0.0
    while elapsed < 0.9 * TEST_TIMEOUT:
        await asyncio.sleep(SLEEP)
        response = await fetch(endpoint, raise_error=False)
        if response.code >= 400:
            response.rethrow()
        if response.code != 202:
            return response

        elapsed = (datetime.datetime.now() - start_time).total_seconds()

    raise TimeoutError(f"Request {endpoint} timed out ({elapsed}s).")


async def wait_for_request(fetch, *args, **kwargs):
    """Wait for execution request."""
    r = await fetch(*args, **kwargs)
    assert r.code == 202
    location = r.headers["Location"]
    assert REQUEST_REGEX.match(location) is not None

    ans = await _wait_request(fetch, location)
    return ans


@pytest.fixture()
def pending_kernel_is_ready(jp_serverapp):
    async def _(kernel_id, ready=None):
        km = jp_serverapp.kernel_manager
        if getattr(km, "use_pending_kernels", False):
            kernel = km.get_kernel(kernel_id)
            if getattr(kernel, "ready", None):
                new_ready = kernel.ready
                # Make sure we get a new ready promise (for a restart)
                while new_ready == ready:
                    await asyncio.sleep(0.1)
                if not isinstance(new_ready, asyncio.Future):
                    new_ready = asyncio.wrap_future(new_ready)
                await new_ready
                return new_ready

    return _


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize(
    "snippet,output",
    (
        (
            "print('hello buddy')",
            '{"output_type": "stream", "name": "stdout", "text": "hello buddy\\n"}',
        ),
        ("a = 1", ""),
        (
            """from IPython.display import HTML
HTML('<p><b>Jupyter</b> rocks.</p>')""",
            '{"output_type": "execute_result", "metadata": {}, "data": {"text/plain": "<IPython.core.display.HTML object>", "text/html": "<p><b>Jupyter</b> rocks.</p>"}, "execution_count": 1}',  # noqa: E501
        ),
    ),
)
async def test_post_execute(jp_fetch, pending_kernel_is_ready, snippet, output):
    r = await jp_fetch(
        "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
    )
    kernel = json.loads(r.body.decode())
    await pending_kernel_is_ready(kernel["id"])

    response = await wait_for_request(
        jp_fetch,
        "api",
        "kernels",
        kernel["id"],
        "execute",
        method="POST",
        body=json.dumps({"code": snippet}),
    )

    assert response.code == 200
    payload = json.loads(response.body)
    assert payload == {
        "status": "ok",
        "execution_count": 1,
        "outputs": f"[{output}]",
    }

    response2 = await jp_fetch("api", "kernels", kernel["id"], method="DELETE")
    assert response2.code == 204

    await asyncio.sleep(1)


@pytest.mark.timeout(TEST_TIMEOUT)
@pytest.mark.parametrize(
    "snippet,output",
    (
        (
            "1 / 0",
            '{"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero", "traceback": ["\\u001b[0;31m---------------------------------------------------------------------------\\u001b[0m", "\\u001b[0;31mZeroDivisionError\\u001b[0m                         Traceback (most recent call last)", "Cell \\u001b[0;32mIn[1], line 1\\u001b[0m\\n\\u001b[0;32m----> 1\\u001b[0m \\u001b[38;5;241;43m1\\u001b[39;49m\\u001b[43m \\u001b[49m\\u001b[38;5;241;43m/\\u001b[39;49m\\u001b[43m \\u001b[49m\\u001b[38;5;241;43m0\\u001b[39;49m\\n", "\\u001b[0;31mZeroDivisionError\\u001b[0m: division by zero"]}',  # noqa: E501
        ),
    ),
)
async def test_post_erroneous_execute(jp_fetch, pending_kernel_is_ready, snippet, output):
    # Start the first kernel
    r = await jp_fetch(
        "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
    )
    kernel = json.loads(r.body.decode())
    await pending_kernel_is_ready(kernel["id"])

    response = await wait_for_request(
        jp_fetch,
        "api",
        "kernels",
        kernel["id"],
        "execute",
        method="POST",
        body=json.dumps({"code": snippet}),
    )

    assert response.code == 200
    payload = json.loads(response.body)
    assert payload == {
        "status": "error",
        "execution_count": 1,
        "outputs": f"[{output}]",
    }

    response2 = await jp_fetch("api", "kernels", kernel["id"], method="DELETE")
    assert response2.code == 204

    await asyncio.sleep(1)


@pytest.mark.timeout(TEST_TIMEOUT)
async def test_execution_timing_metadata(jp_root_dir, jp_fetch, pending_kernel_is_ready, rtc_create_notebook, jp_serverapp):
    snippet = "a = 1"
    nb = nbformat.v4.new_notebook(
        cells=[nbformat.v4.new_code_cell(source=snippet, execution_count=1)]
    )
    nb_content = nbformat.writes(nb, version=4)
    path, _ = await rtc_create_notebook("test.ipynb", nb_content, store=True)
    collaboration = jp_serverapp.web_app.settings["jupyter_server_ydoc"]
    fim = jp_serverapp.web_app.settings["file_id_manager"]
    document_id = f'json:notebook:{fim.get_id("test.ipynb")}'
    cell_id = nb["cells"][0].get("id")

    r = await jp_fetch(
        "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
    )
    kernel = json.loads(r.body.decode())
    await pending_kernel_is_ready(kernel["id"])

    response = await wait_for_request(
        jp_fetch,
        "api",
        "kernels",
        kernel["id"],
        "execute",
        method="POST",
        body=json.dumps({
            "code": snippet,
            "metadata": {
                "cell_id": cell_id,
                "document_id": document_id,
                "record_timing": True
            }
        }),
    )
    assert response.code == 200

    document = await collaboration.get_document(
        path=path, content_type="notebook", file_format="json", copy=False
    )
    cell_data = document.get()["cells"][0]
    assert 'execution' in cell_data['metadata'], "'execution' does not exist in 'metadata'"

    # Assert that start and end time exist in 'execution'
    execution = cell_data['metadata']['execution']
    assert 'shell.execute_reply.started' in execution, "'shell.execute_reply.started' does not exist in 'execution'"
    assert 'shell.execute_reply' in execution, "'shell.execute_reply' does not exist in 'execution'"

    started_time = execution['shell.execute_reply.started']
    reply_time = execution['shell.execute_reply']

    started_dt = datetime.datetime.fromisoformat(started_time)
    reply_dt = datetime.datetime.fromisoformat(reply_time)

    # Assert that reply_time is greater than started_time
    assert reply_dt > started_dt, "The reply time is not greater than the started time."
    response2 = await jp_fetch("api", "kernels", kernel["id"], method="DELETE")
    assert response2.code == 204
    await asyncio.sleep(1)


@pytest.mark.timeout(TEST_TIMEOUT)
async def test_post_input_execute(jp_fetch, pending_kernel_is_ready):
    # Start the first kernel
    r = await jp_fetch(
        "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
    )
    kernel = json.loads(r.body.decode())
    await pending_kernel_is_ready(kernel["id"])

    response = await jp_fetch(
        "api",
        "kernels",
        kernel["id"],
        "execute",
        method="POST",
        body=json.dumps({"code": "input('Age:')"}),
    )
    assert response.code == 202
    location = response.headers["Location"]

    response2 = await _wait_request(jp_fetch, location)

    assert response2.code == 300
    payload = json.loads(response2.body)
    assert "parent_header" in payload
    assert payload["input_request"] == {"prompt": "Age:", "password": False}

    response3 = await jp_fetch(
        "api", "kernels", kernel["id"], "input", method="POST", body=json.dumps({"input": "42"})
    )
    assert response3.code == 201

    response4 = await _wait_request(jp_fetch, location)
    assert response4.code == 200
    payload2 = json.loads(response4.body)
    assert payload2 == {
        "status": "ok",
        "execution_count": 1,
        "outputs": '[{"output_type": "execute_result", "metadata": {}, "data": {"text/plain": "\'42\'"}, "execution_count": 1}]',  # noqa: E501
    }

    r2 = await jp_fetch("api", "kernels", kernel["id"], method="DELETE")
    assert r2.code == 204

    await asyncio.sleep(1)


# FIXME
# @pytest.mark.timeout(TEST_TIMEOUT)
# async def test_cancel_execute(jp_fetch, pending_kernel_is_ready):
#     # Start the first kernel
#     r = await jp_fetch(
#         "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
#     )
#     kernel = json.loads(r.body.decode())
#     await pending_kernel_is_ready(kernel["id"])

#     response = await jp_fetch(
#         "api",
#         "kernels",
#         kernel["id"],
#         "execute",
#         method="POST",
#         body=json.dumps({"code": """import time
# time.sleep(10)
# print("end")
# """}),
#     )

#     assert response.code == 202
#     location = response.headers["Location"]

#     # Cancel task
#     response2 = await jp_fetch(location, method="DELETE")

#     assert response2.code == 204

#     response3 = await jp_fetch(location)
#     payload = json.loads(response3.body)
#     assert payload == {
#         "status": "error",
#         "execution_count": 1
#     }

#     r2 = await jp_fetch("api", "kernels", kernel["id"], method="DELETE")
#     assert r2.code == 204
