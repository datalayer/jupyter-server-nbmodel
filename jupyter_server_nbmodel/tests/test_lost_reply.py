# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

"""A lost ``execute_reply`` must not wedge the kernel's request queue."""

import asyncio
import json

import pytest
from jupyter_client.kernelspec import NATIVE_KERNEL_NAME

from jupyter_server_nbmodel.tests.test_handlers import (
    TEST_TIMEOUT,
    pending_kernel_is_ready,  # noqa: F401 - fixture
    wait_for_request,
)


async def _start_kernel(jp_fetch, pending_kernel_is_ready) -> str:  # noqa: F811
    r = await jp_fetch(
        "api", "kernels", method="POST", body=json.dumps({"name": NATIVE_KERNEL_NAME})
    )
    kernel_id = json.loads(r.body.decode())["id"]
    await pending_kernel_is_ready(kernel_id)
    return kernel_id


async def _execute(jp_fetch, kernel_id: str, code: str, timeout: float = TEST_TIMEOUT):
    return await wait_for_request(
        jp_fetch,
        "api",
        "kernels",
        kernel_id,
        "execute",
        method="POST",
        body=json.dumps({"code": code}),
        timeout=timeout,
    )


async def _connect_other_client(jp_serverapp, kernel_id: str):
    """Connect another client the way any server-side caller would.

    ``KernelManager.client()`` clones the manager's session, session id
    included, and the session id is the ZMQ identity of the shell and stdin
    sockets. ipykernel sets ``ROUTER_HANDOVER`` on both, so a client connecting
    with an identity already in use takes its route over.
    """
    client = jp_serverapp.kernel_manager.get_kernel(kernel_id).client()
    client.start_channels()
    await client.wait_for_ready(timeout=TEST_TIMEOUT / 3)
    return client


@pytest.mark.timeout(TEST_TIMEOUT * 2)
async def test_execute_survives_another_client_on_the_kernel(
    jp_fetch,
    jp_serverapp,
    pending_kernel_is_ready,  # noqa: F811
):
    """Another client on the kernel does not take the execution replies over."""
    kernel_id = await _start_kernel(jp_fetch, pending_kernel_is_ready)

    # The first execution creates the stack's client for this kernel.
    first = await _execute(jp_fetch, kernel_id, "1")
    assert json.loads(first.body)["status"] == "ok"

    other = await _connect_other_client(jp_serverapp, kernel_id)
    try:
        response = await _execute(jp_fetch, kernel_id, "1 + 1")
        payload = json.loads(response.body)
        assert response.code == 200
        assert payload["status"] == "ok"
        assert json.loads(payload["outputs"])[0]["data"]["text/plain"] == "2"
    finally:
        other.stop_channels()

    r = await jp_fetch("api", "kernels", kernel_id, method="DELETE")
    assert r.code == 204
    await asyncio.sleep(1)
