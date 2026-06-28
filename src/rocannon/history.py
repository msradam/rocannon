"""Bounded in-memory history of recent tool calls.

Backs the ``rocannon://runs/{request_id}`` resource and the
``commit_session`` save tool. Audit middleware writes a ``HistoryEntry`` after
every call; readers look up by request_id or iterate recent.
"""

import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class HistoryEntry:
    request_id: str
    tool: str
    target: str
    status: str
    latency_ms: int
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunHistory:
    """Bounded request_id → HistoryEntry mapping with insertion order.

    Thread-safe (asyncio.to_thread workers may write concurrently with the
    event loop reading). Eviction is FIFO once ``max_entries`` is exceeded.
    """

    def __init__(self, max_entries: int = 500) -> None:
        self._max = max_entries
        self._entries: OrderedDict[str, HistoryEntry] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, entry: HistoryEntry) -> None:
        with self._lock:
            self._entries[entry.request_id] = entry
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def get(self, request_id: str) -> HistoryEntry | None:
        with self._lock:
            return self._entries.get(request_id)

    def recent(self, limit: int | None = None) -> list[HistoryEntry]:
        with self._lock:
            items = list(self._entries.values())
        return items[-limit:] if limit else items
