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
@pytest.mark.parametrize("snippet,output",
    (
        ("print('hello buddy')", '{"output_type": "stream", "name": "stdout", "text": "hello buddy\\n"}'),
        ("a = 1", '{}'),
        ("1 / 0", '{}')
    )
)
async def test_post_execute(jp_fetch, pending_kernel_is_ready, snippet, output):
    # Start the first kernel
    r = await jp_fetch(
        "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
    )
    kernel1 = json.loads(r.body.decode())
    await pending_kernel_is_ready(kernel1["id"])

    response = await jp_fetch(
        "api",
        "kernels",
        kernel1["id"],
        "execute",
        method="POST",
        body=json.dumps({"code": snippet}),
    )

    assert response.code == 200
    payload = json.loads(response.body)
    assert payload == {
        "status": "ok",
        "execution_count": 1,
        "outputs": f'[{output}]',
    }

    await jp_fetch("api", "kernels", kernel1["id"], method="DELETE")
    assert response.code == 204
