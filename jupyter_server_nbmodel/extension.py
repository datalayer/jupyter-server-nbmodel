from __future__ import annotations

from jupyter_server.extension.application import ExtensionApp
from jupyter_server.services.kernels.handlers import _kernel_id_regex

from .handlers import ExecuteHandler
from .log import get_logger

RTC_EXTENSIONAPP_NAME = "jupyter_server_ydoc"


class Extension(ExtensionApp):
    name = "jupyter_server_nbmodel"

    def initialize_handlers(self):
        rtc_extension = None
        rtc_extensions = self.serverapp.extension_manager.extension_apps.get(RTC_EXTENSIONAPP_NAME, set())
        n_extensions = len(rtc_extensions)
        if n_extensions:
            if n_extensions > 1:
                get_logger().warning("%i collaboration extensions found.", n_extensions)
            rtc_extension = next(iter(rtc_extensions))
        self.handlers.extend([
            (f"/api/kernels/{_kernel_id_regex}/execute", ExecuteHandler, { "ydoc_extension": rtc_extension })
        ])
