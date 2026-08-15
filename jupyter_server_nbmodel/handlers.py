# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

import json
import os
import re
import typing as t
from http import HTTPStatus
from pathlib import Path

import tornado
from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin

from jupyter_server_nbmodel.execution_stack import ExecutionStack
from jupyter_server_nbmodel.log import get_logger


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
    def get(self, kernel_id: str) -> None:
        """Return active executions for the kernel without consuming them."""
        self.finish(
            json.dumps(
                {
                    "kernel_id": kernel_id,
                    "requests": self._execution_stack.pending(kernel_id),
                }
            )
        )

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
        remote_server = body.get("server")

        if remote_server is not None:
            if (
                not isinstance(remote_server, dict)
                or not isinstance(remote_server.get("url"), str)
                or not remote_server["url"].startswith(("http://", "https://"))
            ):
                raise tornado.web.HTTPError(
                    status_code=HTTPStatus.BAD_REQUEST,
                    reason="Invalid remote Jupyter server connection.",
                )
            token = remote_server.get("token")
            if token is not None and not isinstance(token, str):
                raise tornado.web.HTTPError(
                    status_code=HTTPStatus.BAD_REQUEST,
                    reason="Invalid remote Jupyter server token.",
                )
        elif kernel_id not in self.kernel_manager:
            msg = f"Unknown kernel with id: {kernel_id}"
            get_logger().error(msg)
            raise tornado.web.HTTPError(status_code=HTTPStatus.NOT_FOUND, reason=msg)
        uid = self._execution_stack.put(kernel_id, snippet, metadata, remote_server=remote_server)
        location = f"/api/kernels/{kernel_id}/requests/{uid}"
        self.set_status(HTTPStatus.ACCEPTED)
        self.set_header("Location", location)
        self.finish(
            json.dumps(
                {
                    "request_id": uid,
                    "kernel_id": kernel_id,
                    "cell_id": metadata.get("cell_id"),
                    "document_path": metadata.get("document_path"),
                    "pending": True,
                    "request_status": "queued",
                    "request_url": location,
                    "outputs": "[]",
                }
            )
        )


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
        if kernel_id not in self.kernel_manager and not self._stack.is_remote(kernel_id):
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
                elif r.get("pending") is True:
                    self.set_status(202)
                else:
                    self.set_status(200)
                self.finish(json.dumps(r))

# ---------------------------------------------------------------------------
# The "Recover the outputs over HTTP" switch.
#
# The option itself belongs to the frontend — it decides whether the lab
# extension polls the execution requests and writes the outputs back — and it
# lives where every JupyterLab setting lives: the user-settings file of the
# `notebook-cell-executor` plugin. These handlers are a bridge so any client
# of the *server* (the Datalayer home view among them) can read and flip the
# switch without being a JupyterLab page.
# ---------------------------------------------------------------------------

#: The plugin whose settings hold the switch.
SETTINGS_PLUGIN_PACKAGE = "@datalayer/jupyter-server-nbmodel"
SETTINGS_PLUGIN_SCHEMA = "notebook-cell-executor"

#: The key inside those settings, and its schema default.
OUTPUT_RECOVERY_KEY = "outputRecovery"
OUTPUT_RECOVERY_DEFAULT = False


def _user_settings_path() -> Path:
    """Where JupyterLab keeps the user overrides of the plugin.

    Honours ``JUPYTERLAB_SETTINGS_DIR`` the way JupyterLab does, so a server
    started with a custom settings directory reads and writes the same file
    the lab does.
    """
    from jupyter_core.paths import jupyter_config_dir

    settings_dir = os.environ.get("JUPYTERLAB_SETTINGS_DIR") or os.path.join(
        jupyter_config_dir(), "lab", "user-settings"
    )
    return (
        Path(settings_dir)
        / SETTINGS_PLUGIN_PACKAGE
        / f"{SETTINGS_PLUGIN_SCHEMA}.jupyterlab-settings"
    )


def _read_user_settings() -> dict:
    """The user overrides, or an empty dict when absent or unreadable.

    JupyterLab settings files may carry comments when edited by hand; they
    are stripped before parsing rather than failing the read — an unreadable
    file answers with the defaults, never with an error.
    """
    path = _user_settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        pass
    try:
        without_comments = re.sub(
            r"//[^\n]*|/\*.*?\*/", "", raw, flags=re.DOTALL
        )
        parsed = json.loads(without_comments)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


class OutputRecoverySettingHandler(ExtensionHandlerMixin, APIHandler):
    """Read and flip the *Recover the outputs over HTTP* option."""

    @tornado.web.authenticated
    def get(self) -> None:
        settings = _read_user_settings()
        value = settings.get(OUTPUT_RECOVERY_KEY, OUTPUT_RECOVERY_DEFAULT)
        self.finish(json.dumps({OUTPUT_RECOVERY_KEY: bool(value)}))

    @tornado.web.authenticated
    def put(self) -> None:
        try:
            body = json.loads(self.request.body or b"{}")
        except ValueError as error:
            raise tornado.web.HTTPError(
                HTTPStatus.BAD_REQUEST, reason="Body must be JSON."
            ) from error
        if not isinstance(body, dict) or not isinstance(
            body.get(OUTPUT_RECOVERY_KEY), bool
        ):
            raise tornado.web.HTTPError(
                HTTPStatus.BAD_REQUEST,
                reason=f"Body must carry a boolean {OUTPUT_RECOVERY_KEY!r}.",
            )
        settings = _read_user_settings()
        settings[OUTPUT_RECOVERY_KEY] = body[OUTPUT_RECOVERY_KEY]
        path = _user_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written as plain JSON: a hand-written comment in the file does not
        # survive a flip of the switch, which is the price of not owning a
        # JSON5 writer here.
        path.write_text(json.dumps(settings, indent=4), encoding="utf-8")
        get_logger().info(
            "%s set to %s in %s",
            OUTPUT_RECOVERY_KEY,
            body[OUTPUT_RECOVERY_KEY],
            path,
        )
        self.finish(json.dumps({OUTPUT_RECOVERY_KEY: body[OUTPUT_RECOVERY_KEY]}))
