# Copyright (c) 2024-2025 Datalayer, Inc.
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio
import typing as t
import uuid

from dataclasses import asdict

import jupyter_server
import jupyter_server.services
import jupyter_server.services.kernels
import jupyter_server.services.kernels.kernelmanager

from jupyter_server_nbmodel.models import PendingInput
from jupyter_server_nbmodel.actions import kernel_worker
from jupyter_server_nbmodel.log import get_logger


if t.TYPE_CHECKING:
    import jupyter_client
    from nbformat import NotebookNode
    try:
        import jupyter_server_ydoc
        import pycrdt as y
        from jupyter_ydoc.ynotebook import YNotebook
    except ImportError:
        # optional dependencies
        ...


NO_RESULT = object()


class ExecutionStack:
    """Execution request stack.

    It is keeping track of the execution requests.

    The request result can only be queried once.
    """

    def __init__(
        self,
        manager: jupyter_server.services.kernels.kernelmanager.AsyncMappingKernelManager,
        ydoc_extension: jupyter_server_ydoc.app.YDocExtension | None,
    ):
        self.__manager = manager
        self.__ydoc = ydoc_extension
        # Store execution results per kernelID per execution request ID
        self.__execution_results: dict[str, dict[str, t.Any]] = {}
        # Cache kernel clients
        self.__kernel_clients: dict[str, jupyter_client.asynchronous.client.AsyncKernelClient] = {}
        # Store pending input per kernel ID
        self.__pending_inputs: dict[str, PendingInput] = {}
        # Store execution request parameters in order per kernel ID
        self.__tasks: dict[str, asyncio.Queue] = {}
        # Execution request queue worker per kernel ID
        self.__workers: dict[str, asyncio.Task] = {}


    def __del__(self):
        if (
            len(self.__workers)
            + len(self.__tasks)
            + len(self.__kernel_clients)
            + len(self.__pending_inputs)
        ):
            get_logger().warning(
                "Deleting active ExecutionStack. Be sure to call `await ExecutionStack.dispose()`."
            )
            self.dispose()


    def _get_client(self, kernel_id: str) -> jupyter_client.asynchronous.client.AsyncKernelClient:
        """Get the cached kernel client for ``kernel_id``.

        Args:
            kernel_id: The kernel ID
        Returns:
            The client for the given kernel.
        """
        if kernel_id not in self.__kernel_clients:
            km = self.__manager.get_kernel(kernel_id)
            self.__kernel_clients[kernel_id] = km.client()

        return self.__kernel_clients[kernel_id]


    async def dispose(self) -> None:
        get_logger().debug("Disposing ExecutionStack…")
        for worker in self.__workers.values():
            worker.cancel()

        for kernel_id, input_ in self.__pending_inputs.items():
            if input_.is_pending():
                await self.send_input(kernel_id, "")
        self.__pending_inputs.clear()
        await asyncio.wait_for(asyncio.gather(w for w in self.__workers.values()), timeout=3)
        self.__workers.clear()

        await asyncio.wait_for(asyncio.gather(q.join() for q in self.__tasks.values()), timeout=3)
        self.__tasks.clear()

        for client in self.__kernel_clients.values():
            client.stop_channels()
        self.__kernel_clients.clear()
        get_logger().debug("ExecutionStack has been disposed.")


    async def cancel(self, kernel_id: str, timeout: float | None = None) -> None:
        """Cancel execution for kernel ``kernel_id``.

        Args:
            kernel_id : Kernel identifier
            timeout: Timeout to await for completion in seconds

        Raises:
            TimeoutError: if a task is not cancelled in time
        """
        # FIXME connect this to kernel lifecycle
        get_logger().debug(f"Cancel execution for kernel {kernel_id}.")
        try:
            worker = self.__workers.pop(kernel_id, None)
            if worker is not None:
                worker.cancel()
                await asyncio.wait_for(worker, timeout=timeout)
        finally:
            try:
                queue = self.__tasks.pop(kernel_id, None)
                if queue is not None:
                    await asyncio.wait_for(queue.join(), timeout=timeout)
            finally:
                client = self.__kernel_clients.pop(kernel_id, None)
                if client is not None:
                    client.stop_channels()


    async def send_input(self, kernel_id: str, value: str) -> None:
        """Send input ``value`` to the kernel ``kernel_id``.

        Args:
            kernel_id : Kernel identifier
            value: Input value
        """
        try:
            client = self._get_client(kernel_id)
        except KeyError as e:
            raise ValueError(f"Unable to find kernel {kernel_id}") from e

        # only send stdin reply if there *was not* another request
        # or execution finished while we were reading.
        if not (await client.stdin_channel.msg_ready() or await client.shell_channel.msg_ready()):
            client.input(value)
            self.__pending_inputs[kernel_id].clear()


    def get(self, kernel_id: str, uid: str) -> t.Any:
        """Get the request ``uid`` results, its pending input or None.

        Args:
            kernel_id : Kernel identifier
            uid : Request identifier

        Returns:
            Any: None if the request is pending else its result or the kernel pending input.

        Raises:
            ValueError: If the request ``uid`` does not exists.
        """
        kernel_results = self.__execution_results.get(kernel_id, {})
        if uid not in kernel_results:
            raise ValueError(f"Execution request {uid} for kernel {kernel_id} does not exists.")

        if self.__pending_inputs[kernel_id].is_pending():
            get_logger().info(f"Kernel '{kernel_id}' has a pending input.")
            # Check the request id is the one matching the appearance of the input
            # Otherwise another cell still looking for its results may capture the
            # pending input
            input_ = self.__pending_inputs[kernel_id]
            if uid == input_.request_id:
                return asdict(input_.content)

        result = kernel_results[uid]
        if result == NO_RESULT:
            return None
        else:
            return kernel_results.pop(uid)


    def put(self, kernel_id: str, snippet: str, metadata: dict | None = None) -> str:
        """Add a asynchronous execution request.

        Args:
            kernel_id: Kernel ID
            snippet: Snippet to be executed
            metadata: [optional] Snippet metadata

        Returns:
            Request identifier
        """
        uid = str(uuid.uuid4())

        if kernel_id not in self.__execution_results:
            self.__execution_results[kernel_id] = {}
        # Make the stack aware a request `uid` exists.
        self.__execution_results[kernel_id][uid] = NO_RESULT
        if kernel_id not in self.__pending_inputs:
            self.__pending_inputs[kernel_id] = PendingInput()
        if kernel_id not in self.__tasks:
            self.__tasks[kernel_id] = asyncio.Queue()

        self.__tasks[kernel_id].put_nowait((uid, snippet, metadata))

        if kernel_id not in self.__workers:
            self.__workers[kernel_id] = asyncio.create_task(
                kernel_worker(
                    kernel_id,
                    self._get_client(kernel_id),
                    self.__ydoc,
                    self.__tasks[kernel_id],
                    self.__execution_results[kernel_id],
                    self.__pending_inputs[kernel_id],
                )
            )
        return uid
