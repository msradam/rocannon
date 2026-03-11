import asyncio
import logging
from typing import Any

from fastmcp import FastMCP

from rocannon.config import Config
from rocannon.executor import build_inventory_list, run_module
from rocannon.inventory import get_valid_hosts, load_inventories
from rocannon.schema import expand_modules, fetch_module_schema

logger = logging.getLogger("rocannon")


def create_server(config: Config) -> FastMCP:
    """Build a FastMCP server with one tool per Ansible module."""
    mcp = FastMCP("rocannon")

    merged_hosts = load_inventories(config.inventories)
    valid_hosts = get_valid_hosts(merged_hosts)
    inventory_list = build_inventory_list(config.inventories)

    logger.info("Loaded %d hosts from %d inventories", len(valid_hosts), len(config.inventories))
    logger.info("Valid hosts: %s", ", ".join(sorted(valid_hosts)))

    modules = expand_modules(config.modules)
    logger.info("Expanded %d module specs to %d modules", len(config.modules), len(modules))

    registered = 0
    failed = 0
    for module_name in modules:
        try:
            schema = fetch_module_schema(module_name)
            _register_tool(mcp, module_name, schema, valid_hosts, inventory_list)
            registered += 1
        except Exception:
            failed += 1
            logger.warning("Failed to register %s, skipping", module_name, exc_info=True)

    logger.info(
        "Rocannon startup complete: %d requested → %d registered, %d failed",
        len(modules),
        registered,
        failed,
    )

    return mcp


def _register_tool(
    mcp: FastMCP,
    module_name: str,
    schema: dict[str, Any],
    valid_hosts: set[str],
    inventory_list: list[str],
) -> None:
    """Register a single Ansible module as an MCP tool."""
    description = schema["description"]
    params_doc = []
    for p in schema["parameters"]:
        line = f"  - {p['name']}"
        if p.get("required"):
            line += " (required)"
        if p.get("description"):
            line += f": {p['description']}"
        params_doc.append(line)

    full_description = description
    if params_doc:
        full_description += "\n\nParameters (pass via module_args dict):\n" + "\n".join(params_doc)

    fn = _make_tool_fn(module_name, valid_hosts, inventory_list)
    mcp.tool(name=module_name, description=full_description)(fn)


def _make_tool_fn(module_name: str, valid_hosts: set[str], inventory_list: list[str]):
    """Create an async tool function bound to a specific module."""

    async def tool_fn(host: str, module_args: dict[str, Any] | None = None) -> dict[str, Any]:
        if host not in valid_hosts:
            return {
                "status": "rejected",
                "reason": f"Host '{host}' not found in any loaded inventory. "
                f"Valid hosts: {', '.join(sorted(valid_hosts))}",
            }

        return await asyncio.to_thread(
            run_module,
            module=module_name,
            module_args=module_args or {},
            inventory=inventory_list,
            host_pattern=host,
        )

    return tool_fn


def main():
    """Legacy entrypoint — loads from rocannon.yml."""
    from pathlib import Path

    from rocannon.config import load_profile

    config = load_profile(Path("rocannon.yml"))
    server = create_server(config)
    server.run(transport=config.transport)


if __name__ == "__main__":
    main()
