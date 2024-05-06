import json
import typing as t
from http import HTTPStatus

import nbformat
import tornado
from jupyter_core.utils import ensure_async
from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin

from .log import get_logger


class ExecuteHandler(ExtensionHandlerMixin, APIHandler):
    def initialize(self, name: str, *args: t.Any, **kwargs: t.Any) -> None:
        super().initialize(name, *args, **kwargs)
        self._outputs = []

    @tornado.web.authenticated
    async def post(self, kernel_id: str) -> None:
        """
        Execute a code snippet within the kernel

        Args:
            kernel_id: Kernel ID

        Json Body Required:
            code (str): code to execute

            OR

            document_id (str): Realtime collaboration document unique identifier
            cell_id (str):  to-execute cell identifier
        """
        body = self.get_json_body()
        # FIXME support loading from RTC
        snippet = body["code"]
        try:
            km = self.kernel_manager.get_kernel(kernel_id)
        except KeyError as e:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg, exc_info=e)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg) from e

        client = km.client()

        # FIXME set the username of client.session to server user
        try:
            reply = await ensure_async(
                client.execute_interactive(
                    snippet,
                    output_hook=self._output_hook,
                    stdin_hook=self._stdin_hook if client.allow_stdin else None,
                )
            )

            reply_content = reply["content"]

            self.finish({
                "status": reply_content["status"],
                "execution_count": reply_content["execution_count"],
                # FIXME quid for buffers
                "outputs": json.dumps(self._outputs)
            })
        finally:
            self._outputs.clear()
            del client

    def _output_hook(self, msg) -> None:
        msg_type = msg["header"]["msg_type"]
        if msg_type in ("display_data", "stream", "execute_result", "error"):
            output = nbformat.v4.output_from_msg(msg)
            get_logger().info("Got an output. %s", output)
            self._outputs.append(output)

    def _stdin_hook(self, msg) -> None:
        get_logger().info("Code snippet execution is waiting for an input.")
