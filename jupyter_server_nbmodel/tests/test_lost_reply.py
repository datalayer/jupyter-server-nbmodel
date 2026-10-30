# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

"""A lost ``execute_reply`` must not wedge the kernel's request queue."""

import asyncio
import json

import pytest
from jupyter_client.kernelspec import NATIVE_KERNEL_NAME

from jupyter_server_nbmodel.execution_stack import ExecutionStack
from jupyter_server_nbmodel.tests.test_handlers import (
    SLEEP,
    TEST_TIMEOUT,
    pending_kernel_is_ready,  # noqa: F401 - fixture
    wait_for_request,
)


@pytest.fixture
def jp_server_config(jp_server_config, request):
    """The test server config, with the extension settings a test is parametrized with."""
    return {**jp_server_config, "Extension": getattr(request, "param", {})}


@pytest.fixture
def shared_session_id(monkeypatch):
    """Give the stack's client the manager's session id, as it used to have.

    Another client on the kernel then takes the stack's route over and its
    execute replies are lost — the failure the stack has to recover from.
    """

    def _get_local_client(self, kernel_id):
        clients = self._ExecutionStack__kernel_clients
        if kernel_id not in clients:
            clients[kernel_id] = self._ExecutionStack__manager.get_kernel(kernel_id).client()
        return clients[kernel_id]

    monkeypatch.setattr(ExecutionStack, "_get_local_client", _get_local_client)


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


async def _post_execute(jp_fetch, kernel_id: str, code: str) -> str:
    response = await jp_fetch(
        "api", "kernels", kernel_id, "execute", method="POST", body=json.dumps({"code": code})
    )
    assert response.code == 202
    return response.headers["Location"]


async def _wait_terminal(jp_fetch, location: str, timeout: float = TEST_TIMEOUT):
    """Poll ``location`` until the request is no longer pending, errors included."""
    for _ in range(int(timeout / SLEEP)):
        response = await jp_fetch(location, raise_error=False)
        if response.code != 202:
            return response
        await asyncio.sleep(SLEEP)
    raise TimeoutError(f"Request {location} never reached a terminal state.")


async def _wait_running(jp_fetch, location: str, timeout: float = TEST_TIMEOUT) -> None:
    """Poll ``location`` until the kernel has started running the request."""
    for _ in range(int(timeout / SLEEP)):
        response = await jp_fetch(location, raise_error=False)
        if response.code == 202 and json.loads(response.body)["request_status"] == "running":
            return
        await asyncio.sleep(SLEEP)
    raise TimeoutError(f"Request {location} never started running.")


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


@pytest.mark.timeout(TEST_TIMEOUT * 2)
@pytest.mark.parametrize("jp_server_config", [{"execute_reply_timeout": 1}], indirect=True)
async def test_lost_reply_fails_the_request_and_the_queue_carries_on(
    jp_fetch,
    jp_serverapp,
    pending_kernel_is_ready,  # noqa: F811
    shared_session_id,
):
    """A lost execute reply fails its request only; the next one runs."""
    kernel_id = await _start_kernel(jp_fetch, pending_kernel_is_ready)

    lost = await _post_execute(jp_fetch, kernel_id, "import time\ntime.sleep(2)\n1 + 1")
    queued = await _post_execute(jp_fetch, kernel_id, "2 + 2")
    # Another client connecting while the cell runs takes the reply over; the
    # outputs, broadcast on IOPub, still arrive.
    await _wait_running(jp_fetch, lost)
    other = await _connect_other_client(jp_serverapp, kernel_id)
    try:
        response = await _wait_terminal(jp_fetch, lost)
        assert response.code == 500
        assert "execute_reply" in json.loads(response.body)["error"]

        # The request queued behind it runs, on a client the stack reconnected.
        response = await _wait_terminal(jp_fetch, queued)
        assert response.code == 200
        payload = json.loads(response.body)
        assert payload["status"] == "ok"
        assert json.loads(payload["outputs"])[0]["data"]["text/plain"] == "4"
    finally:
        other.stop_channels()

    r = await jp_fetch("api", "kernels", kernel_id, method="DELETE")
    assert r.code == 204
    await asyncio.sleep(1)


@pytest.mark.timeout(TEST_TIMEOUT)
async def test_a_stopped_worker_is_restarted(
    jp_fetch,
    pending_kernel_is_ready,  # noqa: F811
    monkeypatch,
):
    """A worker that could not connect does not leave later requests queued forever."""
    get_client = ExecutionStack._get_client
    attempts = []

    async def flaky_get_client(self, kernel_id):
        attempts.append(kernel_id)
        if len(attempts) == 1:
            msg = "Kernel unreachable."
            raise ConnectionError(msg)
        return await get_client(self, kernel_id)

    monkeypatch.setattr(ExecutionStack, "_get_client", flaky_get_client)
    kernel_id = await _start_kernel(jp_fetch, pending_kernel_is_ready)

    response = await _wait_terminal(jp_fetch, await _post_execute(jp_fetch, kernel_id, "1"))
    assert response.code == 500
    assert "Kernel unreachable." in json.loads(response.body)["error"]

    response = await _wait_terminal(jp_fetch, await _post_execute(jp_fetch, kernel_id, "2"))
    assert response.code == 200
    assert json.loads(response.body)["status"] == "ok"

    r = await jp_fetch("api", "kernels", kernel_id, method="DELETE")
    assert r.code == 204
    await asyncio.sleep(1)
