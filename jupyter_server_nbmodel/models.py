# Copyright (c) 2024-2025 Datalayer, Inc.
#
# Distributed under the terms of the Modified BSD License.

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InputRequest:
    """input_request data"""

    prompt: str
    password: bool


@dataclass(frozen=True)
class InputDescription:
    """Pending input request data"""

    parent_header: dict
    input_request: InputRequest


@dataclass
class PendingInput:
    """Pending input."""

    request_id: str | None = None
    content: InputDescription | None = None

    def is_pending(self) -> bool:
        """Whether a pending input is ongoing or not."""
        return self.request_id is not None

    def clear(self) -> None:
        """Clear pending input."""
        self.request_id = None
        self.content = None

