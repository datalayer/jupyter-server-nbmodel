# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import asyncio

from jupyter_server.extension.application import ExtensionApp
from jupyter_server.services.kernels.handlers import _kernel_id_regex
from traitlets import Float

from jupyter_server_nbmodel.execution_stack import ExecutionStack
from jupyter_server_nbmodel.handlers import (
    ExecuteHandler,
    InputHandler,
    OutputRecoverySettingHandler,
    RequestHandler,
)
from jupyter_server_nbmodel.log import get_logger

RTC_EXTENSIONAPP_NAME = "jupyter_server_ydoc"

STOP_TIMEOUT = 3

REQUEST_ID_REGEX = r"(?P<request_id>\w+-\w+-\w+-\w+-\w+)"


class Extension(ExtensionApp):

    name = "jupyter_server_nbmodel"

    execute_reply_timeout = Float(
        60,
        config=True,
        help=(
            "Seconds to wait for a cell's execute reply once the kernel has gone idle. "
            "The kernel sends the reply before going idle, so a reply still missing is "
            "lost: the request fails and the kernel's queue carries on with a new client. "
            "The clock starts at idle, so long-running cells are unaffected. 0 waits forever."
        ),
    )

    cancel_grace_period = Float(
        5,
        config=True,
        help=(
            "Seconds a cancelled running cell has to end after its kernel is interrupted. "
            "A cell still running then is abandoned: it fails along with the cells queued "
            "behind it, and the next cell starts over with a new kernel client."
        ),
    )

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
            manager=self.settings["kernel_manager"],
            ydoc_extension=rtc_extension,
            execute_reply_timeout=self.execute_reply_timeout,
            cancel_grace_period=self.cancel_grace_period,
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
                    f"/api/kernels/{_kernel_id_regex}/requests/{REQUEST_ID_REGEX}",
                    RequestHandler,
                    {"execution_stack": self.__execution_stack},
                ),
                (
                    "/api/nbmodel/settings/output-recovery",
                    OutputRecoverySettingHandler,
                    {},
                ),
            ]
        )


    async def stop_extension(self):
        if hasattr(self, "__execution_stack"):
            get_logger().info("Disposing the execution stack…")
            await asyncio.wait_for(self.__execution_stack.dispose(), timeout=3)
