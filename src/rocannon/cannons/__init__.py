"""Cannons: pluggable upstream-catalog reflectors for the MCP server.

Each cannon reads schemas from one upstream catalog (Ansible Galaxy
collections, Terraform Registry providers and modules, Helm charts) and
registers each operation as a typed FastMCP tool.

To add a new cannon, subclass ``Cannon`` and implement ``register``.
Middleware, transports, and FastMCP construction stay in ``server.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from rocannon.history import RunHistory


@dataclass
class CannonServices:
    """Cross-cutting services every cannon can use."""

    history: RunHistory


@dataclass
class CannonMetrics:
    """What a cannon registered, for the startup summary."""

    cannon: str
    tools_registered: int = 0
    tools_failed: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    resources_registered: int = 0
    prompts_registered: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class Cannon(ABC):
    """Base class for execution cannons."""

    name: str  # subclasses set this

    @abstractmethod
    def register(self, mcp: FastMCP, services: CannonServices) -> CannonMetrics:
        """Register all of this cannon's tools/resources/prompts on ``mcp``.

        Called once during server construction. Should be idempotent within a
        process but does not need to handle concurrent re-registration.
        """
