from __future__ import annotations

import logging

from traitlets.config import Application


class _ExtensionLogger:
    _LOGGER: logging.Logger | None = None

    @classmethod
    def get_logger(cls) -> logging.Logger:
        if cls._LOGGER is None:
            app = Application.instance()
            cls._LOGGER = logging.getLogger(f"{app.log.name!s}.jupyter_server_nb_model")
            Application.clear_instance()

        return cls._LOGGER


get_logger = _ExtensionLogger.get_logger
