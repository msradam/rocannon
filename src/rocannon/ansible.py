"""Ansible module reflection and MCP tool registration.

``register_ansible_modules`` reads schemas via ``ansible-doc -j`` and
registers one typed FastMCP tool per module. The Ansible-specific resources
(``rocannon://inventory``, ``rocannon://module/<fqcn>``) live here too;
the cross-cutting tools (``save_playbook``, ``commit_session``,
``rocannon_*_profile``) live in ``server.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import keyword
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from rocannon.correlation import get_call_metadata
from rocannon.executor import build_envvars, build_inventory_list, run_module
from rocannon.inventory import load_inventory
from rocannon.redaction import redact
from rocannon.schema import ANSIBLE_TYPE_MAP, expand_modules, fetch_module_schema

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from rocannon.history import RunHistory
    from rocannon.profiles import RuntimeContext

logger = logging.getLogger("rocannon")


@dataclass
class AnsibleRegistration:
    """What got registered, for the startup summary and the /health route."""

    tools_registered: int = 0
    tool_names: list[str] = field(default_factory=list)
    tools_failed: list[str] = field(default_factory=list)
    resources_registered: int = 0
    hosts: int = 0
    groups: int = 0
    profiles: list[str] = field(default_factory=list)
    active_profile: str = ""


def register_ansible_modules(
    mcp: FastMCP,
    runtime: RuntimeContext,
    history: RunHistory,
) -> AnsibleRegistration:
    """Reflect every module in every loaded profile, register each as a tool.

    The union of all profiles' modules is registered exactly once. The active
    profile is consulted at call time, not registration time, so
    ``rocannon_use_profile`` takes effect without re-registering tools.
    """
    registry = runtime.registry

    union_hosts: set[str] = set()
    union_groups: set[str] = set()
    union_modules: set[str] = set()

    for name, lp in registry.profiles.items():
        inv = load_inventory(lp.config.inventories)
        union_hosts.update(inv["hosts"])
        union_groups.update(inv["groups"])
        expanded = set(expand_modules(lp.config.modules))
        runtime.expanded_modules[name] = expanded
        union_modules.update(expanded)
        logger.info(
            "Profile %r: %d hosts, %d groups, %d modules",
            name,
            len(inv["hosts"]),
            len(inv["groups"]),
            len(expanded),
        )

    if not union_hosts and not union_groups:
        raise ValueError(
            "Inventory resolved to zero hosts and groups across all profiles. "
            "Check that inventory files are readable and contain valid hosts."
        )

    union_inv = {"hosts": sorted(union_hosts), "groups": sorted(union_groups)}

    report = AnsibleRegistration()
    schema_cache: dict[str, dict[str, Any]] = {}
    for module_name in sorted(union_modules):
        try:
            schema = fetch_module_schema(module_name)
            _register_tool(mcp, module_name, schema, union_inv, runtime)
            schema_cache[module_name] = schema
            report.tools_registered += 1
            report.tool_names.append(module_name)
        except Exception as exc:
            report.tools_failed.append(module_name)
            logger.warning("Failed to register %s: %s", module_name, exc)

    if report.tools_registered == 0:
        raise ValueError(
            "No tools registered. Check that the specified modules are installed "
            "and accessible via ansible-doc."
        )

    _add_ansible_resources(mcp, runtime, schema_cache)
    report.resources_registered = 3  # inventory + module + collections
    report.hosts = len(union_hosts)
    report.groups = len(union_groups)
    report.profiles = registry.names()
    report.active_profile = runtime.active_name
    return report


def _add_ansible_resources(
    mcp: FastMCP,
    runtime: RuntimeContext,
    schema_cache: dict[str, dict[str, Any]],
) -> None:
    """Register the inventory and per-module-schema resources.

    Run-history resources (``rocannon://runs`` and ``runs/{id}``) are cross-
    cutting and live in ``server.py``.
    """

    @mcp.resource(
        "rocannon://inventory",
        name="inventory",
        description="Hosts and groups for the active profile.",
        mime_type="application/json",
    )
    def _inventory_resource() -> dict[str, Any]:
        cfg = runtime.active_config()
        inv = load_inventory(cfg.inventories)
        return {
            "active_profile": runtime.active_name,
            "hosts": inv["hosts"],
            "groups": inv["groups"],
        }

    @mcp.resource(
        "rocannon://module/{fqcn}",
        name="module_schema",
        description="Parsed schema (name, description, parameters) for a registered module.",
        mime_type="application/json",
    )
    def _module_resource(fqcn: str) -> dict[str, Any]:
        schema = schema_cache.get(fqcn)
        if schema is None:
            return {"error": f"module not registered: {fqcn}", "available": sorted(schema_cache)}
        return schema

    @mcp.resource(
        "rocannon://collections",
        name="collections",
        description="Collections exposed as tools, each with its registered module names.",
        mime_type="application/json",
    )
    def _collections_resource() -> dict[str, Any]:
        by_collection: dict[str, list[str]] = {}
        for fqcn in sorted(schema_cache):
            by_collection.setdefault(_collection_tag(fqcn), []).append(fqcn)
        return {
            "collections": [
                {"name": coll, "modules": mods, "module_count": len(mods)}
                for coll, mods in sorted(by_collection.items())
            ]
        }


# ---------------------------------------------------------------------------
# Tool registration internals
# ---------------------------------------------------------------------------

# Shared output schema for every module tool. The shape is stable: single-host
# runs carry result/stdout/stderr; multi-host runs carry a `hosts` map; both
# always carry `status`. additionalProperties keeps module-specific result keys.
_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "description": "successful, failed, or error"},
        "changed": {"type": "boolean"},
        "result": {"type": "object", "additionalProperties": True},
        "stdout": {"type": "string"},
        "stderr": {"type": "string"},
        "hosts": {
            "type": "object",
            "additionalProperties": True,
            "description": "Per-host results when the target matched more than one host.",
        },
        "check_mode": {"type": "boolean", "description": "True when the call ran as a dry-run."},
    },
    "required": ["status"],
    "additionalProperties": True,
}

# Builtins that only read state but carry no `facts` attribute or `_info`/`_facts`
# suffix for the naming heuristic to catch.
_READ_ONLY_BUILTINS: frozenset[str] = frozenset(
    {
        "ansible.builtin.ping",
        "ansible.builtin.debug",
        "ansible.builtin.assert",
        "ansible.builtin.stat",
        "ansible.builtin.slurp",
        "ansible.builtin.find",
        "ansible.builtin.getent",
    }
)

_SUPPORTED_LEVELS = frozenset({"full", "partial"})


def _build_annotations(module_name: str, attributes: dict[str, Any]) -> ToolAnnotations | None:
    """Map ansible-doc module attributes to MCP tool hints.

    Read-only: fact-gathering modules (the ``facts`` attribute), the
    ``_info``/``_facts`` naming convention, and a few builtins that only query
    state. Destructive and open-world: the free-form execution family (command,
    shell, script, raw), which ansible-doc flags with the ``raw`` attribute.
    Everything else is left unannotated; ansible-doc carries no idempotency
    signal that would justify a stronger claim.
    """
    short = module_name.rsplit(".", 1)[-1]
    read_only = (
        attributes.get("facts")
        or short.endswith(("_info", "_facts"))
        or module_name in _READ_ONLY_BUILTINS
    )
    if read_only:
        return ToolAnnotations(readOnlyHint=True)
    if attributes.get("raw"):
        return ToolAnnotations(destructiveHint=True, openWorldHint=True)
    return None


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


def _sanitize_param_name(name: str, reserved: set[str]) -> str:
    """Convert an Ansible parameter name to a valid Python identifier, avoiding collisions."""
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if keyword.iskeyword(safe) or keyword.issoftkeyword(safe):
        safe = f"param_{safe}"
    if safe in reserved:
        safe = f"module_{safe}"
    return safe


def _register_tool(
    mcp: FastMCP,
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    runtime: RuntimeContext,
) -> None:
    """Register a single Ansible module as an MCP tool with typed parameters.

    The tool function reads inventory/envvars/timeouts from ``runtime`` at call
    time, so profile switches via ``rocannon_use_profile`` take effect without
    re-registering tools. ``inv`` is the union of hosts+groups across every
    profile, used only to build the target parameter's type annotation.
    """
    fn = _make_tool_fn(module_name, schema, inv, runtime)

    mcp.tool(
        name=module_name,
        description=schema["description"],
        tags={_collection_tag(module_name)},
        annotations=_build_annotations(module_name, schema.get("attributes", {})),
        output_schema=_RESULT_SCHEMA,
    )(fn)


def _make_tool_fn(
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    runtime: RuntimeContext,
) -> Any:
    """Create an async tool function with a dynamic typed signature matching the Ansible module."""
    target_annotation = _build_target_annotation(inv)
    params = schema["parameters"]
    attributes = schema.get("attributes", {})
    inject_check = attributes.get("check_mode") in _SUPPORTED_LEVELS
    inject_diff = attributes.get("diff_mode") in _SUPPORTED_LEVELS

    annotations: dict[str, Any] = {"target": target_annotation}
    sig_params: list[inspect.Parameter] = [
        inspect.Parameter(
            "target",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=target_annotation,
        ),
    ]

    reserved = {"target", "ctx"}
    if inject_check:
        reserved.add("check")
    if inject_diff:
        reserved.add("diff")
    name_map: dict[str, str] = {}  # python_name → ansible_name
    seen_names: set[str] = reserved.copy()

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

    if inject_check:
        check_desc = (
            "Dry-run: report what would change without applying it (Ansible check mode)."
            if attributes["check_mode"] == "full"
            else (
                "Dry-run without applying changes (Ansible check mode). This module's "
                "check-mode support is partial, so some results may be incomplete."
            )
        )
        check_ann = Annotated[bool, Field(description=check_desc)]
        annotations["check"] = check_ann
        sig_params.append(
            inspect.Parameter(
                "check",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=check_ann,
                default=False,
            )
        )
    if inject_diff:
        diff_desc = "Return a diff of what this would change (Ansible diff mode)."
        if attributes["diff_mode"] == "partial":
            diff_desc += " Diff support for this module is partial."
        diff_ann = Annotated[bool, Field(description=diff_desc)]
        annotations["diff"] = diff_ann
        sig_params.append(
            inspect.Parameter(
                "diff",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=diff_ann,
                default=False,
            )
        )

    # Context injection, invisible to the model
    annotations["ctx"] = Context
    sig_params.append(
        inspect.Parameter(
            "ctx",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Context,
            default=CurrentContext(),
        )
    )

    async def tool_fn(**kwargs: Any) -> dict[str, Any]:
        ctx: Context = kwargs.pop("ctx", None)
        target: str = kwargs.pop("target")
        check: bool = kwargs.pop("check", False)
        diff: bool = kwargs.pop("diff", False)

        module_args = {name_map.get(k, k): v for k, v in kwargs.items() if v is not None}

        meta = get_call_metadata()
        if meta is not None:
            meta["args"] = redact(module_args | {"target": target})

        if not runtime.is_module_active(module_name):
            err = {
                "status": "error",
                "changed": False,
                "result": {},
                "stdout": "",
                "stderr": (
                    f"Module {module_name!r} is not declared in the active "
                    f"profile {runtime.active_name!r}. Switch with "
                    f"rocannon_use_profile, or list available profiles with "
                    f"rocannon_list_profiles."
                ),
            }
            if meta is not None:
                meta["result"] = err
                meta["status"] = "error"
            return err

        cfg = runtime.active_config()
        inventory_list = build_inventory_list(cfg.inventories)
        envvars = build_envvars(
            extra_envvars=cfg.extra_envvars,
            ansible_cfg=cfg.ansible_cfg,
            vault_password_file=cfg.vault_password_file,
        )
        module_timeout = cfg.timeouts.get(module_name)

        if ctx and ctx.request_context:
            await ctx.info(f"Executing {module_name} on {target} [profile={runtime.active_name}]")
        else:
            logger.info("Executing %s on %s [profile=%s]", module_name, target, runtime.active_name)

        result = await asyncio.to_thread(
            run_module,
            module=module_name,
            module_args=module_args,
            inventory=inventory_list,
            host_pattern=target,
            timeout=module_timeout,
            envvars=envvars,
            check=check,
            diff=diff,
        )

        if meta is not None:
            meta["result"] = result
            meta["status"] = result.get("status", "ok")

        return result

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = module_name.replace(".", "_")

    return tool_fn
