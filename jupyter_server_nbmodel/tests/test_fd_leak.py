"""Tests for ZMQ context cleanup in ExecutionStack.cancel() and dispose().

Verifies that cancelling or disposing an ExecutionStack properly destroys
the ZMQ context associated with kernel clients, preventing FD leaks.
"""

import asyncio

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_client():
    """Create a mock AsyncKernelClient with a ZMQ context."""
    client = MagicMock()
    client._created_context = True
    client.context = MagicMock()
    client.context.destroy = MagicMock()
    client.stop_channels = MagicMock()
    return client


@pytest.fixture
def execution_stack():
    """Create an ExecutionStack instance with mocked dependencies."""
    from jupyter_server_nbmodel.execution_stack import ExecutionStack

    manager = MagicMock()
    ydoc_extension = None
    return ExecutionStack(manager, ydoc_extension)


async def test_cancel_destroys_zmq_context(execution_stack, mock_client):
    """cancel() should destroy the client's ZMQ context after stopping channels."""
    kernel_id = "test-kernel-id"

    clients = execution_stack._ExecutionStack__kernel_clients
    clients[kernel_id] = mock_client

    await execution_stack.cancel(kernel_id, timeout=1.0)

    mock_client.stop_channels.assert_called_once()
    mock_client.context.destroy.assert_called_once_with(linger=0)


async def test_cancel_skips_external_context(execution_stack, mock_client):
    """cancel() should not destroy externally-provided contexts."""
    kernel_id = "test-kernel-id"
    mock_client._created_context = False

    clients = execution_stack._ExecutionStack__kernel_clients
    clients[kernel_id] = mock_client

    await execution_stack.cancel(kernel_id, timeout=1.0)

    mock_client.stop_channels.assert_called_once()
    mock_client.context.destroy.assert_not_called()


async def test_dispose_destroys_all_zmq_contexts(execution_stack):
    """dispose() should destroy contexts for all cached clients."""
    clients = execution_stack._ExecutionStack__kernel_clients
    workers = execution_stack._ExecutionStack__workers
    tasks = execution_stack._ExecutionStack__tasks

    mock_clients = []
    for i in range(3):
        kid = f"kernel-{i}"
        client = MagicMock()
        client._created_context = True
        client.context = MagicMock()
        client.stop_channels = MagicMock()
        clients[kid] = client
        mock_clients.append(client)

        # Create a completed worker task so gather() succeeds
        async def _noop():
            pass

        workers[kid] = asyncio.create_task(_noop())
        tasks[kid] = asyncio.Queue()

    # Let the noop tasks complete
    await asyncio.sleep(0.01)

    await execution_stack.dispose()

    for client in mock_clients:
        client.stop_channels.assert_called_once()
        client.context.destroy.assert_called_once_with(linger=0)


async def test_cancel_handles_none_context(execution_stack, mock_client):
    """cancel() should handle clients with no context gracefully."""
    kernel_id = "test-kernel-id"
    mock_client.context = None

    clients = execution_stack._ExecutionStack__kernel_clients
    clients[kernel_id] = mock_client

    await execution_stack.cancel(kernel_id, timeout=1.0)
    mock_client.stop_channels.assert_called_once()
