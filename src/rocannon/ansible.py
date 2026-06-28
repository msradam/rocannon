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
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from rocannon.correlation import get_call_metadata
from rocannon.executor import build_envvars, build_inventory_list, run_module, run_role
from rocannon.inventory import load_inventory
from rocannon.redaction import redact
from rocannon.schema import (
    ANSIBLE_TYPE_MAP,
    expand_modules,
    fetch_module_schemas,
    fetch_role_schemas,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from rocannon.history import RunHistory
    from rocannon.profiles import RuntimeContext

logger = logging.getLogger("rocannon")

# Tag carried by every reflected module tool. Progressive discovery hides the
# whole set with mcp.disable(tags={_MODULE_TAG}) and reveals matches per session.
_MODULE_TAG = "ansible.module"


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
        runtime.expanded_roles[name] = set(lp.config.roles)
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
    ordered = sorted(union_modules)

    schemas = fetch_module_schemas(ordered)
    for module_name in ordered:
        schema = schemas.get(module_name)
        if schema is None:
            report.tools_failed.append(module_name)
            logger.warning("Skipping %s: not present in ansible-doc output", module_name)
            continue
        try:
            _register_tool(mcp, module_name, schema, union_inv, runtime)
            schema_cache[module_name] = schema
            report.tools_registered += 1
            report.tool_names.append(module_name)
        except Exception as exc:
            report.tools_failed.append(module_name)
            logger.warning("Failed to register %s: %s", module_name, exc)

    # Progressive discovery hides the module tools behind a search/reveal pair so a
    # large surface doesn't flood the client, while a revealed tool is still the real
    # typed module tool (params, choices, safety hints, approval gate) rather than a
    # generic wrapper. ctx.enable_components reveals matches per session.
    if runtime.active_config().discovery == "progressive":
        mcp.disable(tags={_MODULE_TAG})
        catalog = {n: schema_cache[n]["description"] for n in schema_cache}
        meta = _register_progressive_meta(mcp, runtime, catalog)
        report.tools_registered += len(meta)
        report.tool_names.extend(meta)
        logger.info(
            "Progressive discovery: %d module tools hidden behind %d search/reveal tools",
            len(schema_cache),
            len(meta),
        )

    # Role tools: a role with an argument_specs.yml is documented by ansible-doc
    # like a module and executed via run_role. Fetch per profile so each
    # profile's roles_path applies; register the union once.
    role_schemas: dict[str, dict[str, Any]] = {}
    for lp in registry.profiles.values():
        if not lp.config.roles:
            continue
        rp = str(lp.config.roles_path) if lp.config.roles_path else None
        role_schemas.update(fetch_role_schemas(list(lp.config.roles), roles_path=rp))
    union_roles = (
        sorted(set().union(*runtime.expanded_roles.values())) if runtime.expanded_roles else []
    )
    for role_name in union_roles:
        schema = role_schemas.get(role_name)
        if schema is None:
            report.tools_failed.append(role_name)
            logger.warning(
                "Skipping role %s: no argument_specs documented (ansible-doc -t role)", role_name
            )
            continue
        try:
            _register_role_tool(mcp, role_name, schema, union_inv, runtime)
            schema_cache[role_name] = schema
            report.tools_registered += 1
            report.tool_names.append(role_name)
        except Exception as exc:
            report.tools_failed.append(role_name)
            logger.warning("Failed to register role %s: %s", role_name, exc)

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


def _idempotent_hint(attributes: dict[str, Any]) -> bool | None:
    """Derive MCP ``idempotentHint`` from ansible-doc attributes.

    ansible-doc exposes an ``idempotent`` attribute on some modules (support
    full/partial/none/N/A): ``full`` is idempotent, ``none`` is not, the rest are
    ambiguous. For the modules that omit it, full check-mode support is a reliable
    proxy: a module that can fully predict its change in check mode is declarative,
    so applying it twice is a no-op.
    """
    idem = attributes.get("idempotent")
    if idem == "full":
        return True
    if idem == "none":
        return False
    if idem in ("partial", "N/A"):
        return None
    if attributes.get("check_mode") == "full" and not attributes.get("raw"):
        return True
    return None


def _build_annotations(module_name: str, attributes: dict[str, Any]) -> ToolAnnotations | None:
    """Map ansible-doc module attributes to MCP tool hints.

    Read-only: fact-gathering modules (the ``facts`` attribute), the
    ``_info``/``_facts`` naming convention, and a few builtins that only query
    state. Destructive and open-world: the free-form execution family (command,
    shell, script, raw), which ansible-doc flags with the ``raw`` attribute, and
    which is never idempotent. Otherwise carry ``idempotentHint`` when the source
    supports it (see ``_idempotent_hint``).
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
        return ToolAnnotations(destructiveHint=True, openWorldHint=True, idempotentHint=False)
    hint = _idempotent_hint(attributes)
    if hint is not None:
        return ToolAnnotations(idempotentHint=hint)
    return None


def _needs_approval(read_only: bool, destructive: bool) -> bool:
    """Decide whether a call must be human-approved before it runs.

    Gated by ``ROCANNON_APPROVAL``: ``off`` (default) never gates, ``destructive``
    gates only the free-form execution family (command/shell/script/raw, the
    modules carrying ``destructiveHint``), ``writes`` gates everything that isn't
    a read-only/fact module. An unknown value is treated as ``off``.
    """
    mode = os.environ.get("ROCANNON_APPROVAL", "off").strip().lower()
    if mode == "destructive":
        return destructive
    if mode == "writes":
        return not read_only
    return False


async def _request_approval(
    ctx: Context | None,
    module_name: str,
    target: str,
    module_args: dict[str, Any],
) -> bool | None:
    """Ask the human (via the MCP client) to approve one module call.

    Returns ``True`` if approved, ``False`` if declined/cancelled, and ``None``
    if approval could not be requested at all (no context, or the client does
    not support elicitation). A ``None`` is fail-closed by the caller.
    """
    if ctx is None or not ctx.request_context:
        return None
    summary = ", ".join(f"{k}={redact({k: v})[k]}" for k, v in module_args.items()) or "(no args)"
    message = f"Approve running '{module_name}' on target '{target}'?\nArguments: {summary}"
    try:
        from fastmcp.server.elicitation import AcceptedElicitation

        # fastmcp 3.4.2's elicit() overloads are broken under mypy (a stray
        # docstring between @overload stubs hides the non-None overloads), so the
        # bool response_type resolves to the wrong return type. Bind via Any.
        elicit: Any = ctx.elicit
        result = await elicit(
            message,
            response_type=bool,
            response_title="Approve",
            response_description="Set true to run this on the target, false to abort.",
        )
    except Exception:
        return None
    return isinstance(result, AcceptedElicitation) and result.data is True


def _approval_denied(
    module_name: str,
    target: str,
    *,
    unsupported: bool,
) -> dict[str, Any]:
    """Result returned when a gated call is refused before execution."""
    if unsupported:
        stderr = (
            f"Approval required (ROCANNON_APPROVAL) for {module_name!r} on {target!r}, "
            "but the MCP client does not support elicitation. Refused to run. Unset "
            "ROCANNON_APPROVAL or connect with an elicitation-capable client."
        )
    else:
        stderr = f"Operator declined approval for {module_name!r} on {target!r}; not executed."
    return {"status": "denied", "changed": False, "result": {}, "stdout": "", "stderr": stderr}


def _collection_tag(module_name: str) -> str:
    """Extract collection name as a tag: 'ansible.builtin.copy' → 'ansible.builtin'."""
    parts = module_name.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else module_name


def _tags_for(module_name: str) -> set[str]:
    """Tag a tool by its collection and namespace, both derived from the FQCN.

    'ansible.builtin.copy' -> {'ansible.builtin', 'ansible'}. Lets a client
    filter the surface by collection or whole namespace.
    """
    tags = {_collection_tag(module_name)}
    namespace = module_name.split(".", 1)[0]
    tags.add(namespace)
    return tags


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


async def _execute_gated(
    runtime: RuntimeContext,
    module_name: str,
    target: str,
    module_args: dict[str, Any],
    *,
    check: bool,
    diff: bool,
    read_only: bool,
    destructive: bool,
    ctx: Context | None,
    meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Approval gate then module execution, shared by static tools and ansible_run_module.

    Keeping both paths on one implementation means the human-in-the-loop gate and
    the execution semantics can't drift between static and progressive discovery.
    Dry-runs change nothing, so they are never gated.
    """
    if not check and _needs_approval(read_only, destructive):
        approved = await _request_approval(ctx, module_name, target, module_args)
        if approved is not True:
            denied = _approval_denied(module_name, target, unsupported=approved is None)
            if meta is not None:
                meta["result"] = denied
                meta["status"] = "denied"
            return denied

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


def _optional_param(
    name: str, py_type: Any, desc: str, doc_default: Any
) -> tuple[Any, inspect.Parameter]:
    """Build an optional tool parameter that never forwards the ansible-doc default.

    The Python default is always ``None`` so an omitted argument is dropped by the
    ``if v is not None`` filter before the module runs, and Ansible applies its own
    default. Baking the doc default into the signature would forward it as an
    explicit value, which breaks modules whose defaults are Jinja/shell
    metacharacters (e.g. ``template``'s ``block_start_string="{%"``). The documented
    default is surfaced in the description instead.
    """
    described = (
        f"{desc} (Ansible default: {doc_default})".strip() if doc_default is not None else desc
    )
    ann = Annotated[py_type | None, Field(description=described)]
    param = inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=ann, default=None)
    return ann, param


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
    annotations = _build_annotations(module_name, schema.get("attributes", {}))
    read_only = bool(annotations and annotations.readOnlyHint)
    destructive = bool(annotations and annotations.destructiveHint)
    fn = _make_tool_fn(
        module_name, schema, inv, runtime, read_only=read_only, destructive=destructive
    )

    module_meta = schema.get("meta") or {}
    mcp.tool(
        name=module_name,
        description=schema["description"],
        tags=_tags_for(module_name) | {_MODULE_TAG},
        annotations=annotations,
        output_schema=_RESULT_SCHEMA,
        meta={"ansible": module_meta} if module_meta else None,
    )(fn)


def _make_tool_fn(
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    runtime: RuntimeContext,
    *,
    read_only: bool = False,
    destructive: bool = False,
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
            ann, param = _optional_param(python_name, py_type, desc, default)
            annotations[python_name] = ann
            sig_params.append(param)

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

        return await _execute_gated(
            runtime,
            module_name,
            target,
            module_args,
            check=check,
            diff=diff,
            read_only=read_only,
            destructive=destructive,
            ctx=ctx,
            meta=meta,
        )

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = module_name.replace(".", "_")

    return tool_fn


def _register_progressive_meta(
    mcp: FastMCP,
    runtime: RuntimeContext,
    catalog: dict[str, str],
) -> list[str]:
    """Register the search/reveal pair for progressive discovery.

    The module tools are already registered (fully typed) but hidden by
    ``mcp.disable(tags={_MODULE_TAG})``. ``ansible_search_modules`` ranks the
    catalog; ``ansible_use_module`` reveals a match for the session via
    ``ctx.enable_components``, after which the model calls the real typed tool
    directly. ``catalog`` maps fqcn to short description.
    """

    @mcp.tool(
        name="ansible_search_modules",
        description=(
            "Search this server's Ansible module catalog by capability (e.g. 'copy "
            "file', 'manage service', 'gather facts'). Returns matching module FQCNs "
            "with one-line descriptions. Then call ansible_use_module to make one "
            "callable as a typed tool."
        ),
        tags={"rocannon.discovery"},
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def search_modules(query: str = "", limit: int = 25) -> dict[str, Any]:
        tokens = [t for t in query.lower().split() if t]
        scored: list[tuple[int, str]] = []
        for name in catalog:
            if not runtime.is_module_active(name):
                continue
            if not tokens:
                scored.append((0, name))
                continue
            haystack = f"{name} {catalog[name]}".lower()
            hits = sum(1 for t in tokens if t in haystack)
            if hits:
                # Name matches rank above description-only matches.
                name_hits = sum(1 for t in tokens if t in name.lower())
                scored.append((hits * 2 + name_hits, name))
        scored.sort(key=lambda s: (-s[0], s[1]))
        modules = [{"module": n, "description": catalog[n]} for _, n in scored[:limit]]
        return {"count": len(modules), "matched": len(scored), "modules": modules}

    @mcp.tool(
        name="ansible_use_module",
        description=(
            "Reveal an Ansible module as a typed tool for this session. After calling, "
            "`module` (an FQCN from ansible_search_modules, e.g. ansible.builtin.copy) "
            "becomes directly callable with its real typed parameters, choices, safety "
            "hints, and dry-run flags. Find modules with ansible_search_modules first."
        ),
        tags={"rocannon.discovery"},
    )
    async def use_module(module: str, ctx: Context) -> dict[str, Any]:
        if module not in catalog:
            return {
                "ok": False,
                "error": f"{module!r} is not in this server's catalog",
                "hint": "find modules with ansible_search_modules",
            }
        await ctx.enable_components(names={module}, components={"tool"})
        return {
            "ok": True,
            "tool": module,
            "note": f"{module} is now callable as a typed tool in this session.",
        }

    return ["ansible_search_modules", "ansible_use_module"]


def _register_role_tool(
    mcp: FastMCP,
    role_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    runtime: RuntimeContext,
) -> None:
    """Register a role (with an argument_specs interface) as an MCP tool."""
    fn = _make_role_tool_fn(role_name, schema, inv, runtime)
    role_meta = schema.get("meta") or {}
    mcp.tool(
        name=role_name,
        description=schema["description"],
        tags=_tags_for(role_name) | {"role"},
        output_schema=_RESULT_SCHEMA,
        meta={"ansible": role_meta} if role_meta else None,
    )(fn)


def _make_role_tool_fn(
    role_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    runtime: RuntimeContext,
) -> Any:
    """Async tool function for a role: typed `target` plus the role's argspec.

    No check/diff (those are per-task, not role-level). Executes via run_role,
    which passes the arguments as extravars for ansible to validate.
    """
    target_annotation = _build_target_annotation(inv)
    annotations: dict[str, Any] = {"target": target_annotation}
    sig_params: list[inspect.Parameter] = [
        inspect.Parameter("target", inspect.Parameter.KEYWORD_ONLY, annotation=target_annotation),
    ]
    reserved = {"target", "ctx"}
    name_map: dict[str, str] = {}
    seen_names: set[str] = reserved.copy()
    for p in schema["parameters"]:
        ansible_name = p["name"]
        python_name = _sanitize_param_name(ansible_name, reserved)
        while python_name in seen_names:
            python_name = f"{python_name}_"
        seen_names.add(python_name)
        name_map[python_name] = ansible_name
        py_type = _ansible_type_to_python(p)
        desc = p.get("description", "")
        if p.get("required", False):
            ann = Annotated[py_type, Field(description=desc)]  # type: ignore[valid-type]
            annotations[python_name] = ann
            sig_params.append(
                inspect.Parameter(python_name, inspect.Parameter.KEYWORD_ONLY, annotation=ann)
            )
        else:
            ann, param = _optional_param(python_name, py_type, desc, p.get("default"))
            annotations[python_name] = ann
            sig_params.append(param)

    annotations["ctx"] = Context
    sig_params.append(
        inspect.Parameter(
            "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context, default=CurrentContext()
        )
    )

    async def tool_fn(**kwargs: Any) -> dict[str, Any]:
        ctx: Context = kwargs.pop("ctx", None)
        target: str = kwargs.pop("target")
        role_args = {name_map.get(k, k): v for k, v in kwargs.items() if v is not None}

        meta = get_call_metadata()
        if meta is not None:
            meta["args"] = redact(role_args | {"target": target})

        if not runtime.is_role_active(role_name):
            err = {
                "status": "error",
                "changed": False,
                "result": {},
                "stdout": "",
                "stderr": (
                    f"Role {role_name!r} is not declared in the active profile "
                    f"{runtime.active_name!r}. Switch with rocannon_use_profile."
                ),
            }
            if meta is not None:
                meta["result"] = err
                meta["status"] = "error"
            return err

        # A role is an opaque bundle of state-changing tasks with no check mode,
        # so it is gated under any active approval mode (treated as destructive).
        if _needs_approval(read_only=False, destructive=True):
            approved = await _request_approval(ctx, role_name, target, role_args)
            if approved is not True:
                denied = _approval_denied(role_name, target, unsupported=approved is None)
                if meta is not None:
                    meta["result"] = denied
                    meta["status"] = "denied"
                return denied

        cfg = runtime.active_config()
        inventory_list = build_inventory_list(cfg.inventories)
        envvars = build_envvars(
            extra_envvars=cfg.extra_envvars,
            ansible_cfg=cfg.ansible_cfg,
            vault_password_file=cfg.vault_password_file,
        )
        roles_path = str(cfg.roles_path) if cfg.roles_path else None

        if ctx and ctx.request_context:
            await ctx.info(f"Running role {role_name} on {target} [profile={runtime.active_name}]")
        else:
            logger.info(
                "Running role %s on %s [profile=%s]", role_name, target, runtime.active_name
            )

        result = await asyncio.to_thread(
            run_role,
            role=role_name,
            role_args=role_args,
            inventory=inventory_list,
            host_pattern=target,
            roles_path=roles_path,
            timeout=cfg.timeouts.get(role_name),
            envvars=envvars,
        )
        if meta is not None:
            meta["result"] = result
            meta["status"] = result.get("status", "ok")
        return result

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = role_name.replace(".", "_")

    return tool_fn
