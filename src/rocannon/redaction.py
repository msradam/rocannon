"""Redact secrets from Ansible result payloads before they are logged or returned.

Ansible inventories and module args routinely contain ``ansible_password``,
``ansible_become_password``, API tokens, and private keys. These leak into
``stdout``/``stderr`` and the ``invocation.module_args`` echo that ansible-runner
puts in every result. Scrub before the data leaves the executor.
"""

import re
from typing import Any

REDACTED = "***REDACTED***"

_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|become[_-]?pass)"
)

_TEXT_PATTERNS = [
    re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|become[_-]?pass)"
        r"(\s*[=:]\s*)(\S+)"
    ),
    re.compile(
        r"(?i)(--(?:password|passwd|secret|token|api[_-]?key|become[_-]?pass))"
        r"(\s+)(\S+)"
    ),
]


def redact(obj: Any) -> Any:
    """Recursively scrub sensitive values from a dict/list/str payload.

    Keys matching ``_SENSITIVE_KEY`` have their values replaced wholesale.
    Strings are passed through ``redact_text`` to catch key=value substrings.
    Returns a new structure; the input is not mutated.
    """
    if isinstance(obj, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def redact_text(text: str) -> str:
    """Mask ``key=value`` and ``--flag value`` secret substrings in free-form text."""
    if not text:
        return text
    result = text
    for pat in _TEXT_PATTERNS:
        result = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
    return result
