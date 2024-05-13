"""A Jupyter Server extension to execute code cell from the server."""

from .extension import Extension

__version__ = "0.1.0"


def _jupyter_server_extension_points():
    return [{"module": "jupyter_server_nbmodel", "app": Extension}]
