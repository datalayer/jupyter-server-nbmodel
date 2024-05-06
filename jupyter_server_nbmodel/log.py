from __future__ import annotations

import logging
from typing import Optional

from traitlets.config import Application


class _ExtensionLogger:
    _LOGGER: Optional[logging.Logger] = None

    @classmethod
    def get_logger(cls) -> logging.Logger:
        if cls._LOGGER is None:
            app = Application.instance()
            cls._LOGGER = logging.getLogger(
                "{!s}.jupyter_server_nb_model".format(app.log.name)
            )
            Application.clear_instance()

        return cls._LOGGER


get_logger = _ExtensionLogger.get_logger
