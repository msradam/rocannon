import asyncio
import inspect
import json
import keyword
import logging
import re as _re
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from pydantic import Field

from rocannon.config import Config
from rocannon.executor import build_inventory_list, run_module
from rocannon.inventory import load_inventory
from rocannon.schema import ANSIBLE_TYPE_MAP, expand_modules, fetch_module_schema

logger = logging.getLogger("rocannon")

SERVER_INSTRUCTIONS = """\
Rocannon exposes Ansible modules as MCP tools for remote host automation.

Every tool requires a `target` parameter — a host, group, or pattern from the loaded inventory.
Module parameters are typed tool arguments derived from `ansible-doc`.
Required parameters must be provided; optional parameters have defaults shown in the schema.

Tools execute Ansible modules via SSH. Results include:
- status: "successful" or "failed"
- changed: whether the host state was modified
- result: full Ansible module output
- stdout/stderr: command output when applicable

Use `ansible_builtin_setup` or `ansible_builtin_gather_facts` to discover host details.
Use `ansible_builtin_command` or `ansible_builtin_shell` for ad-hoc commands.
Prefer specific modules (e.g. `ansible_builtin_copy`, `ansible_builtin_file`) over shell commands.\
"""


def create_server(config: Config) -> FastMCP:
    """Build a FastMCP server with one tool per Ansible module."""
    mcp = FastMCP(
        "rocannon",
        instructions=SERVER_INSTRUCTIONS,
    )

    inv = load_inventory(config.inventories)
    inventory_list = build_inventory_list(config.inventories)

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

    registered = 0
    failed = 0
    for module_name in modules:
        try:
            schema = fetch_module_schema(module_name)
            _register_tool(mcp, module_name, schema, inv, inventory_list)
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


def _collection_tag(module_name: str) -> str:
    """Extract collection name as a tag: 'ansible.builtin.copy' → 'ansible.builtin'."""
    parts = module_name.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else module_name


def _build_target_annotation(inv: dict[str, list[str]]) -> Any:
    """Build a typed annotation for the target parameter.

    Uses Literal for small inventories so the model sees exact valid values.
    Falls back to a described str for larger inventories.
    """
    valid_targets = inv["hosts"] + inv["groups"]
    if len(valid_targets) <= 30:
        return Annotated[
            Literal[tuple(valid_targets)],
            Field(description="Target host or group from inventory"),
        ]
    return Annotated[
        str,
        Field(description=f"Target host or group. Valid: {', '.join(valid_targets)}"),
    ]


def _ansible_type_to_python(param: dict[str, Any]) -> Any:
    """Map an Ansible parameter schema to a Python type for MCP schema generation."""
    atype = param.get("type", "str")
    choices = param.get("choices")

    if choices:
        if isinstance(choices, dict):
            choices = list(choices.keys())
        if isinstance(choices, list) and all(isinstance(c, str) for c in choices):
            return Literal[tuple(choices)]

    base = ANSIBLE_TYPE_MAP.get(atype, str)

    if base is list:
        elem_type = ANSIBLE_TYPE_MAP.get(param.get("elements", "str"), str)
        return list[elem_type]  # type: ignore[valid-type]

    return base


def _register_tool(
    mcp: FastMCP,
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    inventory_list: list[str],
) -> None:
    """Register a single Ansible module as an MCP tool with typed parameters."""
    fn = _make_tool_fn(module_name, schema, inv, inventory_list)

    mcp.tool(
        name=module_name,
        description=schema["description"],
        tags={_collection_tag(module_name)},
    )(fn)


def _sanitize_param_name(name: str, reserved: set[str]) -> str:
    """Convert an Ansible parameter name to a valid Python identifier, avoiding collisions."""
    safe = _re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if keyword.iskeyword(safe) or keyword.issoftkeyword(safe):
        safe = f"param_{safe}"
    if safe in reserved:
        safe = f"module_{safe}"
    return safe


def _make_tool_fn(
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    inventory_list: list[str],
) -> Any:
    """Create an async tool function with a dynamic typed signature matching the Ansible module."""
    target_annotation = _build_target_annotation(inv)
    params = schema["parameters"]

    annotations: dict[str, Any] = {"target": target_annotation}
    sig_params: list[inspect.Parameter] = [
        inspect.Parameter(
            "target",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=target_annotation,
        ),
    ]

    reserved = {"target", "ctx"}
    name_map: dict[str, str] = {}  # python_name → ansible_name
    seen_names: set[str] = set(reserved)

    for p in params:
        ansible_name = p["name"]
        python_name = _sanitize_param_name(ansible_name, reserved)
        while python_name in seen_names:
            python_name = f"{python_name}_"
        seen_names.add(python_name)
        name_map[python_name] = ansible_name

        py_type = _ansible_type_to_python(p)
        is_required = p.get("required", False)
        desc = p.get("description", "")
        default = p.get("default")

        if is_required:
            ann = Annotated[py_type, Field(description=desc)]  # type: ignore[valid-type]
            annotations[python_name] = ann
            sig_params.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                )
            )
        else:
            optional_type = py_type | None if default is None else py_type
            ann = Annotated[optional_type, Field(description=desc)]  # type: ignore[misc]
            annotations[python_name] = ann
            sig_params.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                    default=default,
                )
            )

    # Context injection — invisible to the model
    annotations["ctx"] = Context
    sig_params.append(
        inspect.Parameter(
            "ctx",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Context,
            default=CurrentContext(),
        )
    )

    async def tool_fn(**kwargs: Any) -> str:
        ctx: Context = kwargs.pop("ctx", None)
        target: str = kwargs.pop("target")

        module_args = {name_map.get(k, k): v for k, v in kwargs.items() if v is not None}

        if ctx and ctx.request_context:
            await ctx.info(f"Executing {module_name} on {target}")
        else:
            logger.info("Executing %s on %s", module_name, target)

        result = await asyncio.to_thread(
            run_module,
            module=module_name,
            module_args=module_args,
            inventory=inventory_list,
            host_pattern=target,
        )

        return json.dumps(result, indent=2, default=str)

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = module_name.replace(".", "_")

    return tool_fn
