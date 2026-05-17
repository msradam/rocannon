"""Request-scoped correlation IDs for tracing a single tool call across logs and spans."""

import contextvars
import logging
import secrets
from typing import Any

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "rocannon_request_id", default=None
)
_call_metadata: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "rocannon_call_metadata", default=None
)


def new_request_id() -> str:
    """Return a short hex ID suitable for one tool call."""
    return secrets.token_hex(4)


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str) -> contextvars.Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    _request_id.reset(token)


def init_call_metadata() -> dict[str, Any]:
    """Initialize a per-call metadata dict in this context. Returns the dict for mutation."""
    d: dict[str, Any] = {}
    _call_metadata.set(d)
    return d


def get_call_metadata() -> dict[str, Any] | None:
    """Return the current per-call metadata dict, if one is set."""
    return _call_metadata.get()


class CorrelationFormatter(logging.Formatter):
    """Logging formatter that injects the current request_id into every record."""

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = get_request_id() or "-"
        return super().format(record)
