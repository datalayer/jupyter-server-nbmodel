# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import typing as t
import uuid
from dataclasses import asdict

import jupyter_server
import jupyter_server.services
import jupyter_server.services.kernels
import jupyter_server.services.kernels.kernelmanager

from jupyter_server_nbmodel.actions import kernel_worker
from jupyter_server_nbmodel.log import get_logger
from jupyter_server_nbmodel.models import PendingInput

if t.TYPE_CHECKING:
    try:
        import jupyter_server_ydoc
    except ImportError:
        # optional dependencies
        ...


NO_RESULT = object()


async def _run_in_thread(callback: t.Callable[[], t.Any]) -> t.Any:
    """Run a blocking callback on a dedicated daemon thread."""
    result: list[t.Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(callback())
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.01)
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


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
        # Keep request metadata until its result is consumed so every response
        # can describe the request restored from notebook cell metadata.
        self.__execution_metadata: dict[str, dict[str, dict | None]] = {}
        # Cache kernel clients
        self.__kernel_clients: dict[str, t.Any] = {}
        # Connection information for kernels hosted by a remote Jupyter server.
        self.__remote_servers: dict[str, dict[str, str | None]] = {}
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

    def _get_local_client(self, kernel_id: str) -> t.Any:
        """Get or create a client managed by the local Jupyter server."""
        if kernel_id not in self.__kernel_clients:
            km = self.__manager.get_kernel(kernel_id)
            self.__kernel_clients[kernel_id] = km.client()
        return self.__kernel_clients[kernel_id]

    async def _get_client(self, kernel_id: str) -> t.Any:
        """Get the cached kernel client for ``kernel_id``.

        Args:
            kernel_id: The kernel ID
        Returns:
            The client for the given kernel.
        """
        if kernel_id not in self.__kernel_clients:
            remote_server = self.__remote_servers.get(kernel_id)
            if remote_server is None:
                client = self._get_local_client(kernel_id)
                # A newly connected IOPub SUB socket can otherwise miss the
                # first execution messages while its subscription propagates.
                # The readiness handshake also drains startup messages before
                # the worker submits its first execution request.
                await client.wait_for_ready()
            else:
                from jupyter_kernel_client.manager import KernelHttpManager

                def create_remote_client() -> t.Any:
                    manager = KernelHttpManager(
                        server_url=remote_server["url"],
                        token=remote_server.get("token"),
                        kernel_id=kernel_id,
                    )
                    client = manager.client
                    client._server_nbmodel_remote = True
                    client.start_channels()
                    return client

                self.__kernel_clients[kernel_id] = await _run_in_thread(create_remote_client)

        return self.__kernel_clients[kernel_id]

    async def _run_worker(self, kernel_id: str) -> None:
        """Create the kernel client and process its execution queue."""
        try:
            client = await self._get_client(kernel_id)
        except BaseException as error:
            get_logger().error("Failed to connect to kernel %s.", kernel_id, exc_info=error)
            queue = self.__tasks[kernel_id]
            while not queue.empty():
                uid, _, _ = queue.get_nowait()
                self.__execution_results[kernel_id][uid] = {"error": str(error)}
                queue.task_done()
            return
        await kernel_worker(
            kernel_id,
            client,
            self.__ydoc,
            self.__tasks[kernel_id],
            self.__execution_results[kernel_id],
            self.__pending_inputs[kernel_id],
        )

    async def dispose(self) -> None:
        get_logger().debug("Disposing ExecutionStack…")
        for worker in self.__workers.values():
            worker.cancel()

        for kernel_id, input_ in self.__pending_inputs.items():
            if input_.is_pending():
                await self.send_input(kernel_id, "")
        self.__pending_inputs.clear()
        await asyncio.wait_for(asyncio.gather(*self.__workers.values()), timeout=3)
        self.__workers.clear()

        await asyncio.wait_for(
            asyncio.gather(*(q.join() for q in self.__tasks.values())), timeout=3
        )
        self.__tasks.clear()

        for client in self.__kernel_clients.values():
            client.stop_channels()
            # Destroy the ZMQ context to release internal signaling FDs
            # (eventfd/pipe pair). stop_channels() only closes the sockets.
            if getattr(client, "_created_context", False) and client.context:
                client.context.destroy(linger=0)
        self.__kernel_clients.clear()
        self.__remote_servers.clear()
        self.__execution_metadata.clear()
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
                    # Destroy the ZMQ context to release internal signaling
                    # FDs. Without this, each cancel leaks 2 FDs (the
                    # eventfd/pipe pair used by the context's internal
                    # signaling mechanism) until GC collects the client.
                    if getattr(client, "_created_context", False) and client.context:
                        client.context.destroy(linger=0)
                self.__remote_servers.pop(kernel_id, None)

    async def send_input(self, kernel_id: str, value: str) -> None:
        """Send input ``value`` to the kernel ``kernel_id``.

        Args:
            kernel_id : Kernel identifier
            value: Input value
        """
        try:
            client = await self._get_client(kernel_id)
        except KeyError as e:
            raise ValueError(f"Unable to find kernel {kernel_id}") from e

        # only send stdin reply if there *was not* another request
        # or execution finished while we were reading.
        stdin_ready = client.stdin_channel.msg_ready()
        shell_ready = client.shell_channel.msg_ready()
        if inspect.isawaitable(stdin_ready):
            stdin_ready = await stdin_ready
        if inspect.isawaitable(shell_ready):
            shell_ready = await shell_ready
        if not (stdin_ready or shell_ready):
            client.input(value)
        # Cleared whichever branch was taken. The request is answered in one
        # and overtaken in the other — the kernel asked again, or finished —
        # so in neither is it still pending. Clearing only after sending left
        # the stack advertising an input nobody would ever answer again: the
        # caller had been told CREATED, and every later poll kept returning
        # the same prompt.
        self.__pending_inputs[kernel_id].clear()

    def is_remote(self, kernel_id: str) -> bool:
        """Whether ``kernel_id`` is connected through a remote Jupyter server."""
        return kernel_id in self.__remote_servers

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

        metadata = self.__execution_metadata.get(kernel_id, {}).get(uid)
        request_context = {
            "request_id": uid,
            "kernel_id": kernel_id,
            "request_url": f"/api/kernels/{kernel_id}/requests/{uid}",
            "cell_id": metadata.get("cell_id") if isinstance(metadata, dict) else None,
            "document_path": (
                metadata.get("document_path") if isinstance(metadata, dict) else None
            ),
        }

        if self.__pending_inputs[kernel_id].is_pending():
            get_logger().info(f"Kernel '{kernel_id}' has a pending input.")
            # Check the request id is the one matching the appearance of the input
            # Otherwise another cell still looking for its results may capture the
            # pending input
            input_ = self.__pending_inputs[kernel_id]
            if uid == input_.request_id:
                return {
                    **asdict(input_.content),
                    **request_context,
                    "pending": True,
                    "request_status": "input",
                }

        result = kernel_results[uid]
        if result == NO_RESULT:
            return {
                **request_context,
                "pending": True,
                "request_status": "queued",
                "outputs": "[]",
            }
        elif isinstance(result, dict) and result.get("pending") is True:
            # Return a serialized snapshot while retaining the mutable progress
            # object until the worker replaces it with the final result.
            return {
                **request_context,
                "pending": True,
                "request_status": "running",
                "outputs": json.dumps(result.get("outputs", [])),
            }
        else:
            self.__execution_metadata.get(kernel_id, {}).pop(uid, None)
            return {
                **kernel_results.pop(uid),
                **request_context,
                "pending": False,
                "request_status": "complete",
            }

    def pending(self, kernel_id: str) -> list[dict[str, t.Any]]:
        """Describe active requests without consuming their final results."""
        requests: list[dict[str, t.Any]] = []
        for uid, result in self.__execution_results.get(kernel_id, {}).items():
            metadata = self.__execution_metadata.get(kernel_id, {}).get(uid)
            context = {
                "request_id": uid,
                "kernel_id": kernel_id,
                "request_url": f"/api/kernels/{kernel_id}/requests/{uid}",
                "cell_id": metadata.get("cell_id") if isinstance(metadata, dict) else None,
                "document_path": (
                    metadata.get("document_path") if isinstance(metadata, dict) else None
                ),
                "pending": True,
            }
            if result == NO_RESULT:
                requests.append({**context, "request_status": "queued", "outputs": "[]"})
            elif isinstance(result, dict) and result.get("pending") is True:
                requests.append(
                    {
                        **context,
                        "request_status": "running",
                        "outputs": json.dumps(result.get("outputs", [])),
                    }
                )
        return requests

    def put(
        self,
        kernel_id: str,
        snippet: str,
        metadata: dict | None = None,
        remote_server: dict[str, str | None] | None = None,
    ) -> str:
        """Add a asynchronous execution request.

        Args:
            kernel_id: Kernel ID
            snippet: Snippet to be executed
            metadata: [optional] Snippet metadata
            remote_server: [optional] Remote Jupyter server connection

        Returns:
            Request identifier
        """
        uid = str(uuid.uuid4())

        if remote_server is not None:
            self.__remote_servers[kernel_id] = remote_server

        if kernel_id not in self.__execution_results:
            self.__execution_results[kernel_id] = {}
        if kernel_id not in self.__execution_metadata:
            self.__execution_metadata[kernel_id] = {}
        # Make the stack aware a request `uid` exists.
        self.__execution_results[kernel_id][uid] = NO_RESULT
        self.__execution_metadata[kernel_id][uid] = metadata
        if kernel_id not in self.__pending_inputs:
            self.__pending_inputs[kernel_id] = PendingInput()
        if kernel_id not in self.__tasks:
            self.__tasks[kernel_id] = asyncio.Queue()

        self.__tasks[kernel_id].put_nowait((uid, snippet, metadata))

        if kernel_id not in self.__workers:
            self.__workers[kernel_id] = asyncio.create_task(self._run_worker(kernel_id))
        return uid
