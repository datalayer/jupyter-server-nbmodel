import asyncio
import json

from jupyter_client.kernelspec import NATIVE_KERNEL_NAME

import pytest

TEST_TIMEOUT = 60


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
            '{"output_type": "execute_result", "metadata": {}, "data": {"text/plain": "<IPython.core.display.HTML object>", "text/html": "<p><b>Jupyter</b> rocks.</p>"}, "execution_count": 1}',
        ),
    ),
)
async def test_post_execute(jp_fetch, pending_kernel_is_ready, snippet, output):
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

    response = await jp_fetch(
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
