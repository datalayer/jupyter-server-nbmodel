# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import json
import typing as t

from http import HTTPStatus

import tornado

from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin

from jupyter_server_nbmodel.log import get_logger
from jupyter_server_nbmodel.execution_stack import ExecutionStack


class ExecuteHandler(ExtensionHandlerMixin, APIHandler):
    """Handle request for snippet execution."""

    def initialize(
        self,
        name: str,
        execution_stack: ExecutionStack,
        *args: t.Any,
        **kwargs: t.Any,
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._execution_stack = execution_stack


    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        """
        Execute a code snippet within the kernel

        Args:
            kernel_id: Kernel ID

        Json Body Required:
            code (str): code to execute
            metadata (dict): [optional]
                document_id (str): Realtime collaboration document unique identifier
                cell_id (str): to-execute cell identifier
        """
        body = self.get_json_body()

        snippet = body.get("code")
        metadata = body.get("metadata", {})

        if kernel_id not in self.kernel_manager:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)
        uid = self._execution_stack.put(kernel_id, snippet, metadata)
        self.set_status(HTTPStatus.ACCEPTED)
        self.set_header("Location", f"/api/kernels/{kernel_id}/requests/{uid}")
        self.finish("{}")


class InputHandler(ExtensionHandlerMixin, APIHandler):
    """Handle request for input reply."""

    def initialize(
        self, name: str, execution_stack: ExecutionStack, *args: t.Any, **kwargs: t.Any
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._stack = execution_stack

    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        """
        Send an input value to kernel ``kernel_id``.

        Args:
            kernel_id: Kernel identifier

        Json Body Required:
            input (str): Input value
        """
        if kernel_id not in self.kernel_manager:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)

        body = self.get_json_body()

        await self._stack.send_input(kernel_id, body["input"])

        self.set_status(HTTPStatus.CREATED)


class RequestHandler(ExtensionHandlerMixin, APIHandler):
    """Handler for /api/kernels/<kernel_id>/requests/<request_id>"""

    def initialize(
        self, name: str, execution_stack: ExecutionStack, *args: t.Any, **kwargs: t.Any
    ) -> None:
        super().initialize(name, *args, **kwargs)
        self._stack = execution_stack

    @tornado.web.authenticated
    def get(self, kernel_id: str, request_id: str) -> None:
        """`GET /api/kernels/<kernel_id>/requests/<request_id>` Returns the request ``uid`` status.

        Status are:

        * 200: Request result is returned
        * 202: Request is pending
        * 300: Request has a pending input
        * 500: Request ends with errors

        Args:
            kernel_id: Kernel identifier
            request_id: Request identifier

        Raises:
            404 if request ``request_id`` for ``kernel_id`` does not exist
        """
        try:
            r = self._stack.get(kernel_id, request_id)
        except ValueError as err:
            raise tornado.web.HTTPError(404, reason=str(err)) from err
        else:
            if r is None:
                self.set_status(202)
                self.finish("{}")
            else:
                if "error" in r:
                    self.set_status(500)
                    self.log.debug(f"{r}")
                elif "input_request" in r:
                    self.set_status(300)
                    self.set_header("Location", f"/api/kernels/{kernel_id}/input")
                else:
                    self.set_status(200)
                self.finish(json.dumps(r))
