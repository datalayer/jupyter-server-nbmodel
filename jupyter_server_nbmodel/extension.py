from __future__ import annotations

import typing as t

import tornado
from jupyter_server.extension.application import ExtensionApp
from jupyter_server.services.kernels.handlers import _kernel_id_regex

from .handlers import ExecuteHandler


class Extension(ExtensionApp):
    name = "jupyter_server_nbmodel"
    handlers: t.ClassVar[list[tuple[str, tornado.web.RequestHandler]]] = [
        (f"/api/kernels/{_kernel_id_regex}/execute", ExecuteHandler)
    ]
