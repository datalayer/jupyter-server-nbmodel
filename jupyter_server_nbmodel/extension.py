from __future__ import annotations

import asyncio

from jupyter_server.extension.application import ExtensionApp
from jupyter_server.services.kernels.handlers import _kernel_id_regex

from .handlers import ExecuteHandler, ExecutionStack, InputHandler, RequestHandler
from .log import get_logger

RTC_EXTENSIONAPP_NAME = "jupyter_server_ydoc"

STOP_TIMEOUT = 3

_request_id_regex = r"(?P<request_id>\w+-\w+-\w+-\w+-\w+)"


class Extension(ExtensionApp):
    name = "jupyter_server_nbmodel"

    def initialize_handlers(self):
        rtc_extension = None
        rtc_extensions = self.serverapp.extension_manager.extension_apps.get(
            RTC_EXTENSIONAPP_NAME, set()
        )
        n_extensions = len(rtc_extensions)
        if n_extensions:
            if n_extensions > 1:
                get_logger().warning("%i collaboration extensions found.", n_extensions)
            rtc_extension = next(iter(rtc_extensions))

        self.__execution_stack = ExecutionStack(
            manager=self.settings["kernel_manager"], ydoc_extension=rtc_extension
        )

        self.handlers.extend(
            [
                (
                    f"/api/kernels/{_kernel_id_regex}/execute",
                    ExecuteHandler,
                    {"execution_stack": self.__execution_stack},
                ),
                (
                    f"/api/kernels/{_kernel_id_regex}/input",
                    InputHandler,
                    {"execution_stack": self.__execution_stack},
                ),
                (
                    f"/api/kernels/{_kernel_id_regex}/requests/{_request_id_regex}",
                    RequestHandler,
                    {"execution_stack": self.__execution_stack},
                ),
            ]
        )

    async def stop_extension(self):
        if hasattr(self, "__execution_stack"):
            get_logger().info("Disposing the execution stack…")
            await asyncio.wait_for(self.__execution_stack.dispose(), timeout=3)
