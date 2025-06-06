# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

try:
    from ._version import __version__
except ImportError:
    # Fallback when using the package in dev mode without installing
    # in editable mode with pip. It is highly recommended to install
    # the package from a stable release or in editable mode: https://pip.pypa.io/en/stable/topics/local-project-installs/#editable-installs
    import warnings
    warnings.warn("Importing 'jupyter_server_nbmodel' outside a proper installation.", stacklevel=1)
    __version__ = "dev"

from .extension import Extension


def _jupyter_labextension_paths():
    return [{"src": "labextension", "dest": "jupyter-server-nbmodel"}]


def _jupyter_server_extension_points():
    return [{"module": "jupyter_server_nbmodel", "app": Extension}]
