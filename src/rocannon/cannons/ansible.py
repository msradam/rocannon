"""Ansible cannon.

Reflects modules via ``ansible-doc``, registers one typed MCP tool per module.
Also registers the inventory and per-module-schema resources.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from rocannon.executor import build_envvars, build_inventory_list
from rocannon.inventory import load_inventory
from rocannon.schema import expand_modules, fetch_module_schema

from . import Cannon, CannonMetrics, CannonServices

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from rocannon.config import Config

logger = logging.getLogger("rocannon")


class AnsibleCannon(Cannon):
    name = "ansible"

    def __init__(self, config: Config) -> None:
        self.config = config

    def register(self, mcp: FastMCP, services: CannonServices) -> CannonMetrics:
        # Lazy imports keep the cannon sibling-loadable (no cycle through server.py).
        from rocannon.server import (
            _add_resources,
            _register_tool,
        )

        config = self.config

        inv = load_inventory(config.inventories)
        inventory_list = build_inventory_list(config.inventories)

        if not inv["hosts"] and not inv["groups"]:
            raise ValueError(
                "Inventory resolved to zero hosts and groups. "
                "Check that inventory files are readable and contain valid hosts."
            )

        logger.info(
            "Inventory: %d hosts, %d groups from %d files",
            len(inv["hosts"]),
            len(inv["groups"]),
            len(config.inventories),
        )
        if inv["hosts"]:
            logger.info("Hosts: %s", ", ".join(inv["hosts"]))
        if inv["groups"]:
            logger.info("Groups: %s", ", ".join(inv["groups"]))

        modules = expand_modules(config.modules)
        logger.info("Expanded %d module specs to %d modules", len(config.modules), len(modules))

        envvars = build_envvars(
            extra_envvars=config.extra_envvars,
            ansible_cfg=config.ansible_cfg,
            vault_password_file=config.vault_password_file,
        )

        metrics = CannonMetrics(cannon=self.name)
        schema_cache: dict[str, dict[str, Any]] = {}
        for module_name in modules:
            try:
                schema = fetch_module_schema(module_name)
                module_timeout = config.timeouts.get(module_name)
                _register_tool(
                    mcp, module_name, schema, inv, inventory_list,
                    module_timeout, envvars,
                )
                schema_cache[module_name] = schema
                metrics.tools_registered += 1
                metrics.tool_names.append(module_name)
            except Exception as exc:
                metrics.tools_failed.append(module_name)
                logger.warning("Failed to register %s: %s", module_name, exc)

        if metrics.tools_registered == 0:
            raise ValueError(
                "No tools registered. Check that the specified modules are installed "
                "and accessible via ansible-doc."
            )

        # Ansible-specific resources (inventory + per-module schema).
        # Cross-cannon save/replay machinery lives in server.py now.
        _add_resources(mcp, inv, schema_cache, services.history)
        metrics.resources_registered = 4  # inventory + module + runs + runs/{id}

        # Stash inventory counts so the server's /health route can read them.
        metrics.extra["hosts"] = len(inv["hosts"])
        metrics.extra["groups"] = len(inv["groups"])
        return metrics
