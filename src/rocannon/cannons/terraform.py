"""Terraform/OpenTofu cannon.

Reflects provider resources via ``tofu providers schema -json`` and registry
modules via parsed ``variables.tf``. Registers one typed MCP tool per resource
and per module. Each call writes HCL JSON to the workspace, runs plan + apply,
returns structured result.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from . import Cannon, CannonMetrics, CannonServices

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from rocannon.config import TerraformConfig

logger = logging.getLogger("rocannon.terraform")

# ---------------------------------------------------------------------------
# schema reflection
# ---------------------------------------------------------------------------


@dataclass
class ResourceSchema:
    provider: str  # e.g. "docker"
    name: str  # e.g. "docker_container"
    attributes: dict[str, dict[str, Any]]
    block_types: dict[str, dict[str, Any]]  # nested blocks; opaque pass-through

    @property
    def required(self) -> list[str]:
        return [a for a, info in self.attributes.items() if info.get("required")]


def _provider_short_name(qualified: str) -> str:
    """Reduce ``registry.opentofu.org/kreuzwerker/docker`` to ``docker``."""
    return qualified.rsplit("/", 1)[-1]


def reflect_schemas(workspace: Path) -> dict[str, ResourceSchema]:
    """Return ``{resource_name: ResourceSchema}`` for every resource in any
    provider currently initialized in ``workspace``.

    The workspace must already have a ``main.tf`` declaring the providers and
    have had ``tofu init`` run. ``init_workspace`` does both.
    """
    proc = subprocess.run(
        ["tofu", f"-chdir={workspace}", "providers", "schema", "-json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"tofu providers schema failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    data = json.loads(proc.stdout)
    out: dict[str, ResourceSchema] = {}
    for provider_qualified, schema in data.get("provider_schemas", {}).items():
        provider = _provider_short_name(provider_qualified)
        for resource_name, resource in schema.get("resource_schemas", {}).items():
            block = resource.get("block", {})
            out[resource_name] = ResourceSchema(
                provider=provider,
                name=resource_name,
                attributes=block.get("attributes", {}) or {},
                block_types=block.get("block_types", {}) or {},
            )
    return out


# ---------------------------------------------------------------------------
# workspace + execution
# ---------------------------------------------------------------------------


_HCL_RESOURCES_FILE = "rocannon.tf.json"
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


_HCL_REFLECTION_FILE = "rocannon_reflect.tf.json"


def init_workspace(
    workspace: Path,
    providers: dict[str, dict[str, str]],
    provider_config: dict[str, dict[str, Any]] | None = None,
    modules: list[Any] | None = None,
) -> None:
    """Write provider declarations and (optionally) module stubs, then ``tofu init``.

    ``providers`` maps a local alias (``docker``) to ``{"source": "...",
    "version": "..."}``. ``provider_config`` (optional) maps the same alias
    to provider-level kwargs (e.g. ``{"host": "unix:///path/to/docker.sock"}``).

    ``modules`` (optional) is a list of ``TerraformModuleSpec``, each gets a
    reflection-only stub block written so ``tofu init`` downloads the module's
    source to ``.terraform/modules/<key>/``. The stub is deleted after init,
    so subsequent plans do not include the stub instances. The downloaded
    module sources remain on disk and are reused by later ``apply_module``.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {"terraform": {}}
    if providers:
        config["terraform"]["required_providers"] = {
            alias: {"source": p["source"], "version": p["version"]}
            for alias, p in providers.items()
        }
    if provider_config:
        config["provider"] = dict(provider_config)
    (workspace / "providers.tf.json").write_text(json.dumps(config, indent=2))

    if modules:
        reflection_doc: dict[str, Any] = {"module": {}}
        for spec in modules:
            stub_block: dict[str, Any] = {"source": spec.source}
            if spec.version:
                stub_block["version"] = spec.version
            key = _module_key(spec)
            reflection_doc["module"][key] = stub_block
        (workspace / _HCL_REFLECTION_FILE).write_text(json.dumps(reflection_doc, indent=2))

    proc = subprocess.run(
        ["tofu", f"-chdir={workspace}", "init", "-no-color", "-input=false"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tofu init failed: {proc.stderr.strip() or proc.stdout.strip()}")

    # Strip the reflection stub now that modules are on disk. Subsequent
    # plans should not see the empty-arg module instances.
    (workspace / _HCL_REFLECTION_FILE).unlink(missing_ok=True)
    logger.info("tofu init complete in %s", workspace)


def _module_key(spec: Any) -> str:
    """Per-module directory key used by tofu under ``.terraform/modules/``."""
    return (spec.tool_name or _safe_module_name(spec.source)) + "_reflect"


def _safe_module_name(source: str) -> str:
    """``terraform-aws-modules/vpc/aws`` → ``aws_vpc``."""
    parts = source.split("/")
    # last two are typically <name>/<provider>; flip to <provider>_<name>
    if len(parts) >= 2:
        return f"{parts[-1]}_{parts[-2]}".replace("-", "_").lower()
    return source.replace("/", "_").replace("-", "_").lower()


def _load_resources(workspace: Path) -> dict[str, Any]:
    """Read accumulated resource + module blocks; return ``{}`` if the file doesn't exist.

    HCL JSON rejects empty ``"resource": {}`` blocks, so we never write them.
    """
    path = workspace / _HCL_RESOURCES_FILE
    if not path.exists():
        return {}
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _prune_empty(doc: dict[str, Any]) -> dict[str, Any]:
    """Remove empty top-level ``resource``/``module`` keys (HCL JSON rejects them)."""
    return {k: v for k, v in doc.items() if v}


def _write_resources(workspace: Path, doc: dict[str, Any]) -> None:
    pruned = _prune_empty(doc)
    if not pruned:
        (workspace / _HCL_RESOURCES_FILE).unlink(missing_ok=True)
        return
    (workspace / _HCL_RESOURCES_FILE).write_text(json.dumps(pruned, indent=2))


def apply_resource(
    workspace: Path,
    resource_type: str,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Add or update a resource and run plan + apply.

    Returns ``{ok, address, plan_summary, after, raw}``.

    On failure, the resource block is **reverted** so the workspace is left in
    the state it was before the call. (Same as if the call never happened.)
    """
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid resource name {name!r}: must match {_NAME_RE.pattern}")

    doc = _load_resources(workspace)
    prior = doc.get("resource", {}).get(resource_type, {}).get(name)
    doc.setdefault("resource", {}).setdefault(resource_type, {})[name] = args
    _write_resources(workspace, doc)
    had_prior_file = (workspace / _HCL_RESOURCES_FILE).exists()
    _ = had_prior_file  # reserved; revert logic below handles file presence
    address = f"{resource_type}.{name}"

    try:
        plan_proc = subprocess.run(
            [
                "tofu",
                f"-chdir={workspace}",
                "plan",
                "-no-color",
                "-input=false",
                "-out",
                "rocannon.plan",
                "-detailed-exitcode",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 0=no changes, 1=error, 2=changes pending
        if plan_proc.returncode == 1:
            raise RuntimeError(
                f"tofu plan failed: {plan_proc.stderr.strip() or plan_proc.stdout.strip()}"
            )
        no_changes = plan_proc.returncode == 0

        if not no_changes:
            apply_proc = subprocess.run(
                [
                    "tofu",
                    f"-chdir={workspace}",
                    "apply",
                    "-no-color",
                    "-input=false",
                    "-auto-approve",
                    "rocannon.plan",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if apply_proc.returncode != 0:
                raise RuntimeError(
                    f"tofu apply failed: {apply_proc.stderr.strip() or apply_proc.stdout.strip()}"
                )
            plan_summary = _extract_plan_summary(plan_proc.stdout)
        else:
            plan_summary = "no changes"

        after = _read_state_resource(workspace, address)
        return {
            "ok": True,
            "address": address,
            "plan_summary": plan_summary,
            "after": after,
        }
    except Exception:
        # Revert on any failure so the workspace is left in its pre-call state.
        if prior is None:
            del doc["resource"][resource_type][name]
            if not doc["resource"][resource_type]:
                del doc["resource"][resource_type]
        else:
            doc["resource"][resource_type][name] = prior
        # If the resource block is now empty, remove the file entirely,
        # ``"resource": {}`` is rejected by the HCL JSON parser.
        if not doc.get("resource"):
            (workspace / _HCL_RESOURCES_FILE).unlink(missing_ok=True)
        else:
            _write_resources(workspace, doc)
        raise


def destroy_resource(workspace: Path, address: str) -> dict[str, Any]:
    """Destroy ``address`` and any resources that depend on it, then remove
    from the workspace config.

    Uses ``tofu destroy -target=<addr>``, which walks the dependency graph and
    destroys dependents first. The config file is updated AFTER the destroy
    succeeds, so a failed destroy leaves the workspace consistent.
    """
    if "." not in address:
        raise ValueError(f"address must be 'type.name', got {address!r}")
    resource_type, name = address.split(".", 1)

    doc = _load_resources(workspace)
    block = doc.get("resource", {}).get(resource_type, {})
    if name not in block:
        return {"ok": False, "error": f"{address!r} not in workspace config"}

    # Destroy first (with dependency-ordered cascade), then update config.
    proc = subprocess.run(
        [
            "tofu",
            f"-chdir={workspace}",
            "destroy",
            "-no-color",
            "-input=false",
            "-auto-approve",
            f"-target={address}",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"destroy failed: {proc.stderr.strip() or proc.stdout.strip()}")

    # Strip the resource (and any dependents tofu also destroyed) from config.
    after_addrs = set(state_list(workspace))
    new_resource: dict[str, dict[str, Any]] = {}
    for rt, instances in doc.get("resource", {}).items():
        for inst_name, args in instances.items():
            if f"{rt}.{inst_name}" in after_addrs:
                new_resource.setdefault(rt, {})[inst_name] = args
    if new_resource:
        doc["resource"] = new_resource
        _write_resources(workspace, doc)
    else:
        (workspace / _HCL_RESOURCES_FILE).unlink(missing_ok=True)

    return {"ok": True, "address": address, "destroyed": True}


def state_list(workspace: Path) -> list[str]:
    proc = subprocess.run(
        ["tofu", f"-chdir={workspace}", "state", "list"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _read_state_resource(workspace: Path, address: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["tofu", f"-chdir={workspace}", "state", "show", "-no-color", address],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return {"_unavailable": proc.stderr.strip() or "state show failed"}
    return {"_raw": proc.stdout}


def _extract_plan_summary(plan_stdout: str) -> str:
    """Pull the ``Plan: X to add, Y to change, Z to destroy.`` line."""
    for line in plan_stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Plan:"):
            return stripped
    return plan_stdout.splitlines()[-1] if plan_stdout.strip() else ""


# ---------------------------------------------------------------------------
# module reflection (community Terraform Registry modules)
# ---------------------------------------------------------------------------


@dataclass
class ModuleVariable:
    name: str
    type_spec: str | None  # raw HCL type, e.g. "string", "${list(string)}"
    description: str
    required: bool
    default: Any = None


@dataclass
class ModuleSchema:
    source: str
    version: str | None
    variables: dict[str, ModuleVariable]
    outputs: list[str]  # output names, values fetched from state


def reflect_modules(workspace: Path, modules: list[Any]) -> dict[str, ModuleSchema]:
    """Parse ``variables.tf`` and ``outputs.tf`` for each downloaded module.

    Assumes ``init_workspace`` already ran with these modules, i.e. their
    sources live under ``.terraform/modules/<key>/``. Requires ``python-hcl2``
    (declared by the ``[terraform]`` extra).
    """
    import hcl2  # type: ignore[import-untyped]

    out: dict[str, ModuleSchema] = {}
    for spec in modules:
        key = _module_key(spec)
        mod_dir = workspace / ".terraform" / "modules" / key
        if not mod_dir.is_dir():
            logger.warning("module %s: no downloaded source at %s", spec.source, mod_dir)
            continue

        variables: dict[str, ModuleVariable] = {}
        for var_file in sorted(mod_dir.glob("variables*.tf")):
            try:
                with var_file.open() as f:
                    parsed = hcl2.load(f)
            except Exception as exc:
                logger.warning("module %s: failed to parse %s: %s", spec.source, var_file, exc)
                continue
            for var_block in parsed.get("variable", []) or []:
                for raw_var_name, var_spec in var_block.items():
                    var_name = _normalize_str(raw_var_name)  # strip HCL quotes
                    if var_name in variables:
                        continue
                    has_default = "default" in var_spec
                    variables[var_name] = ModuleVariable(
                        name=var_name,
                        type_spec=_normalize_type(var_spec.get("type")),
                        description=_normalize_str(var_spec.get("description", "")),
                        required=not has_default,
                        default=var_spec.get("default") if has_default else None,
                    )

        outputs: list[str] = []
        for out_file in sorted(mod_dir.glob("outputs*.tf")):
            try:
                with out_file.open() as f:
                    parsed_out = hcl2.load(f)
            except Exception as exc:  # noqa: BLE001
                logger.warning("module %s: failed to parse %s: %s", spec.source, out_file, exc)
                continue
            for out_block in parsed_out.get("output", []) or []:
                outputs.extend(_normalize_str(k) for k in out_block)

        out[spec.source] = ModuleSchema(
            source=spec.source,
            version=spec.version,
            variables=variables,
            outputs=outputs,
        )
    return out


def _normalize_type(raw: Any) -> str | None:
    """python-hcl2 returns HCL types as bare strings or ``${...}`` templates."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s.startswith("${") and s.endswith("}"):
        s = s[2:-1]
    return s


def _normalize_str(raw: Any) -> str:
    """python-hcl2 sometimes returns strings with embedded quote literals."""
    if isinstance(raw, list) and raw:
        raw = raw[0]
    s = str(raw)
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        s = s[1:-1]
    return s


def _hcl_type_to_python(type_spec: str | None) -> Any:
    """Map an HCL type expression to a Python type for the MCP tool signature."""
    if not type_spec:
        return str
    s = type_spec.strip()
    base_scalars: dict[str, Any] = {
        "string": str,
        "number": float,
        "bool": bool,
        "any": str,
        "dynamic": str,
    }
    if s in base_scalars:
        return base_scalars[s]
    if s.startswith(("list(", "set(", "tuple(")):
        inner = s[s.index("(") + 1 : s.rindex(")")]
        return list[_hcl_type_to_python(inner)]  # type: ignore[misc]
    if s.startswith("map("):
        inner = s[s.index("(") + 1 : s.rindex(")")]
        return dict[str, _hcl_type_to_python(inner)]  # type: ignore[misc]
    if s.startswith("object("):
        return dict
    return str


def apply_module(
    workspace: Path,
    source: str,
    version: str | None,
    instance: str,
    args: dict[str, Any],
    output_names: list[str] | None = None,
) -> dict[str, Any]:
    """Add a ``module "<instance>" {...}`` block and run plan + apply.

    Captures the module's outputs and returns them. Because pure-computation
    modules (no resources) don't store outputs in state, we synthesize root-
    level ``output`` blocks that re-expose each declared module output,
    making them readable via ``tofu output -json``.
    """
    if not _NAME_RE.fullmatch(instance):
        raise ValueError(f"invalid module instance name {instance!r}")

    doc = _load_resources(workspace)
    prior_module = doc.get("module", {}).get(instance)
    prior_outputs = {
        k: v for k, v in doc.get("output", {}).items() if k.startswith(f"{instance}__")
    }
    block: dict[str, Any] = {"source": source}
    if version:
        block["version"] = version
    block.update(args)
    doc.setdefault("module", {})[instance] = block

    # Synthesize root outputs so module outputs land in state.
    if output_names:
        doc.setdefault("output", {})
        for out_name in output_names:
            doc["output"][f"{instance}__{out_name}"] = {
                "value": f"${{module.{instance}.{out_name}}}",
            }

    _write_resources(workspace, doc)
    address = f"module.{instance}"

    try:
        # tofu records each module instance label in modules.json; a new
        # instance needs `init` to register before plan. Cheap when sources
        # are already cached.
        init_proc = subprocess.run(
            ["tofu", f"-chdir={workspace}", "init", "-no-color", "-input=false"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if init_proc.returncode != 0:
            raise RuntimeError(
                f"tofu init (re-register) failed: "
                f"{init_proc.stderr.strip() or init_proc.stdout.strip()}"
            )

        plan_proc = subprocess.run(
            [
                "tofu",
                f"-chdir={workspace}",
                "plan",
                "-no-color",
                "-input=false",
                "-out",
                "rocannon.plan",
                "-detailed-exitcode",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if plan_proc.returncode == 1:
            raise RuntimeError(
                f"tofu plan failed: {plan_proc.stderr.strip() or plan_proc.stdout.strip()}"
            )
        if plan_proc.returncode == 2:
            apply_proc = subprocess.run(
                [
                    "tofu",
                    f"-chdir={workspace}",
                    "apply",
                    "-no-color",
                    "-input=false",
                    "-auto-approve",
                    "rocannon.plan",
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            if apply_proc.returncode != 0:
                raise RuntimeError(
                    f"tofu apply failed: {apply_proc.stderr.strip() or apply_proc.stdout.strip()}"
                )
            plan_summary = _extract_plan_summary(plan_proc.stdout)
        else:
            plan_summary = "no changes"

        outputs = _read_module_outputs(workspace, instance)
        return {
            "ok": True,
            "address": address,
            "plan_summary": plan_summary,
            "outputs": outputs,
        }
    except Exception:
        # Revert: restore both the module block and the root outputs to
        # their pre-call state so the workspace stays consistent.
        if prior_module is None:
            del doc["module"][instance]
            if not doc["module"]:
                del doc["module"]
        else:
            doc["module"][instance] = prior_module
        if "output" in doc:
            for k in list(doc["output"]):
                if k.startswith(f"{instance}__"):
                    del doc["output"][k]
            for k, v in prior_outputs.items():
                doc["output"][k] = v
            if not doc["output"]:
                del doc["output"]
        _write_resources(workspace, doc)
        raise


def _read_module_outputs(workspace: Path, instance: str) -> dict[str, Any]:
    """Pull a module's outputs from ``tofu output -json``.

    Returns the values of root-level outputs named ``<instance>__<output>``
    (which we synthesize at apply time) with the prefix stripped.
    """
    proc = subprocess.run(
        ["tofu", f"-chdir={workspace}", "output", "-json", "-no-color"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        return {"_unavailable": proc.stderr.strip()}
    try:
        all_outputs = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"_unavailable": f"bad json: {exc}"}
    if not isinstance(all_outputs, dict):
        return {}
    prefix = f"{instance}__"
    return {
        k.removeprefix(prefix): v.get("value") if isinstance(v, dict) else v
        for k, v in all_outputs.items()
        if k.startswith(prefix)
    }


# ---------------------------------------------------------------------------
# TerraformCannon, type-map, dynamic tool registration
# ---------------------------------------------------------------------------


# Terraform's type system maps to Python as follows. List/set/map/object types
# arrive as nested lists in the JSON schema, e.g. ``["list", "string"]``.
def _tf_type_to_python(type_spec: Any) -> type:
    if isinstance(type_spec, str):
        return {
            "string": str,
            "number": float,
            "bool": bool,
            "any": str,
            "dynamic": str,
        }.get(type_spec, str)
    if isinstance(type_spec, list) and type_spec:
        head = type_spec[0]
        elem = type_spec[1] if len(type_spec) > 1 else "string"
        elem_py = _tf_type_to_python(elem)
        if head in ("list", "set", "tuple"):
            return list[elem_py]  # type: ignore[valid-type]
        if head == "map":
            return dict[str, elem_py]  # type: ignore[valid-type]
        if head == "object":
            return dict
    return str


def _make_tf_tool_fn(
    workspace: Path,
    resource_type: str,
    schema: ResourceSchema,
) -> Any:
    """Build a typed async tool function for one Terraform resource.

    Signature: ``(name: str, **attributes) -> str``. ``name`` is the
    Terraform resource instance label (e.g. ``"my_container"``). All schema
    attributes become keyword-only args; required attrs are required, optional
    have ``None`` defaults. Computed-only attrs are excluded, they're outputs.
    Nested blocks come through as opaque ``list[dict]`` for now.
    """
    annotations: dict[str, Any] = {}
    sig_params: list[inspect.Parameter] = []

    # ``instance`` is the resource instance label (Terraform's local block name).
    # We reserve this slot, if a resource attribute happens to be named
    # ``instance`` we'll rename it on the way in. Same trick as the Ansible
    # cannon's _sanitize_param_name + name_map.
    instance_ann = Annotated[
        str,
        Field(
            description=(
                "Resource instance label, the Terraform local name for this "
                "block (e.g. 'my_container'). Must be a valid HCL identifier."
            )
        ),
    ]
    annotations["instance"] = instance_ann
    sig_params.append(
        inspect.Parameter(
            "instance",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=instance_ann,
        )
    )

    reserved = {"instance"}
    name_map: dict[str, str] = {}  # python_name → terraform_attr_name

    for attr_name, info in schema.attributes.items():
        if info.get("computed") and not info.get("optional"):
            # Computed-only (output), skip from inputs.
            continue
        safe_name = attr_name
        while safe_name in reserved or safe_name in name_map:
            safe_name = f"{safe_name}_"
        name_map[safe_name] = attr_name
        py_type = _tf_type_to_python(info.get("type", "string"))
        description = info.get("description", "")
        required = bool(info.get("required"))

        ann: Any
        if required:
            ann = Annotated[py_type, Field(description=description)]
            annotations[safe_name] = ann
            sig_params.append(
                inspect.Parameter(
                    safe_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                )
            )
        else:
            optional_type = py_type | None
            ann = Annotated[optional_type, Field(description=description)]
            annotations[safe_name] = ann
            sig_params.append(
                inspect.Parameter(
                    safe_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                    default=None,
                )
            )

    # Nested blocks as opaque list-of-dict / dict for now.
    for block_name, block_info in schema.block_types.items():
        safe_name = block_name
        while safe_name in reserved or safe_name in name_map:
            safe_name = f"{safe_name}_"
        name_map[safe_name] = block_name
        nesting = block_info.get("nesting_mode", "list")
        py_type = list[dict[str, Any]] if nesting in ("list", "set") else dict[str, Any]
        ann = Annotated[
            py_type | None,
            Field(description=f"Nested block '{block_name}' (opaque pass-through)."),
        ]
        annotations[safe_name] = ann
        sig_params.append(
            inspect.Parameter(
                safe_name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=ann,
                default=None,
            )
        )

    async def tool_fn(**kwargs: Any) -> str:
        instance_label = kwargs.pop("instance")
        # Drop None-valued attrs and de-mangle param names back to TF attrs.
        args = {name_map.get(k, k): v for k, v in kwargs.items() if v is not None}
        import asyncio

        result = await asyncio.to_thread(
            apply_resource,
            workspace,
            resource_type,
            instance_label,
            args,
        )
        return json.dumps(result, indent=2, default=str)

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = f"tf_{resource_type}"
    return tool_fn


def _make_tf_module_tool_fn(
    workspace: Path,
    schema: ModuleSchema,
) -> Any:
    """Build a typed async tool function for one Terraform module.

    Signature: ``(instance, **variables) -> str``. Required module variables
    are required tool args; optional vars have ``None`` defaults. The module's
    declared outputs become the return payload (alongside plan summary).
    """
    annotations: dict[str, Any] = {}
    sig_params: list[inspect.Parameter] = []

    instance_ann = Annotated[
        str,
        Field(description="Module instance label (HCL local name)."),
    ]
    annotations["instance"] = instance_ann
    sig_params.append(
        inspect.Parameter(
            "instance",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=instance_ann,
        )
    )

    reserved = {"instance"}
    name_map: dict[str, str] = {}

    for var_name, var in schema.variables.items():
        safe = var_name
        while safe in reserved or safe in name_map:
            safe = f"{safe}_"
        name_map[safe] = var_name
        py_type = _hcl_type_to_python(var.type_spec)
        ann: Any
        if var.required:
            ann = Annotated[py_type, Field(description=var.description)]
            annotations[safe] = ann
            sig_params.append(
                inspect.Parameter(
                    safe,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                )
            )
        else:
            optional_type = py_type | None
            ann = Annotated[optional_type, Field(description=var.description)]
            annotations[safe] = ann
            sig_params.append(
                inspect.Parameter(
                    safe,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                    default=None,
                )
            )

    async def tool_fn(**kwargs: Any) -> str:
        instance_label = kwargs.pop("instance")
        args = {name_map.get(k, k): v for k, v in kwargs.items() if v is not None}
        import asyncio

        result = await asyncio.to_thread(
            apply_module,
            workspace,
            schema.source,
            schema.version,
            instance_label,
            args,
            schema.outputs,
        )
        return json.dumps(result, indent=2, default=str)

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = f"tf_module_{_safe_module_name(schema.source)}"
    return tool_fn


class TerraformCannon(Cannon):
    """Reflect Terraform provider resources as typed MCP tools.

    Constructor takes a ``TerraformConfig``. ``register`` initializes the
    workspace (writes provider declarations, runs ``tofu init``), reflects all
    resource schemas, and registers one typed tool per resource plus utility
    tools for destroy / state inspection.
    """

    name = "terraform"

    def __init__(self, config: TerraformConfig) -> None:
        self.config = config

    def register(self, mcp: FastMCP, services: CannonServices) -> CannonMetrics:
        import shutil

        if not (shutil.which("tofu") or shutil.which("terraform")):
            raise RuntimeError(
                "Terraform cannon needs the `tofu` (OpenTofu) or `terraform` "
                "binary on PATH. Install with:\n"
                "  macOS:  brew install opentofu\n"
                "  Linux:  https://opentofu.org/docs/intro/install/\n"
                "  RHEL:   dnf install opentofu"
            )

        cfg = self.config
        init_workspace(
            cfg.workspace,
            cfg.providers,
            cfg.provider_config,
            modules=cfg.modules,
        )
        metrics = CannonMetrics(cannon=self.name)

        # --- Provider resources ---
        schemas = reflect_schemas(cfg.workspace) if cfg.providers else {}
        wanted = set(cfg.expose_resources) if cfg.expose_resources else set(schemas)
        for resource_type in sorted(schemas):
            if resource_type not in wanted:
                continue
            try:
                fn = _make_tf_tool_fn(cfg.workspace, resource_type, schemas[resource_type])
                tool_name = f"tf_{resource_type}"
                mcp.tool(
                    name=tool_name,
                    description=(
                        f"Create or update Terraform resource '{resource_type}' "
                        f"in the workspace. Required: {schemas[resource_type].required}."
                    ),
                    tags={"terraform", schemas[resource_type].provider},
                )(fn)
                metrics.tools_registered += 1
                metrics.tool_names.append(tool_name)
            except Exception as exc:
                metrics.tools_failed.append(resource_type)
                logger.warning("Failed to register tf resource %s: %s", resource_type, exc)

        # --- Community modules ---
        module_schemas = reflect_modules(cfg.workspace, cfg.modules) if cfg.modules else {}
        for spec in cfg.modules:
            schema = module_schemas.get(spec.source)
            if schema is None:
                metrics.tools_failed.append(f"module:{spec.source}")
                continue
            tool_name = f"tf_module_{spec.tool_name or _safe_module_name(spec.source)}"
            try:
                fn = _make_tf_module_tool_fn(cfg.workspace, schema)
                required_vars = [v.name for v in schema.variables.values() if v.required]
                mcp.tool(
                    name=tool_name,
                    description=(
                        f"Instantiate the '{spec.source}' module"
                        + (f" (version {spec.version})" if spec.version else "")
                        + f". Required vars: {required_vars or '(none)'}."
                    ),
                    tags={"terraform", "terraform.module"},
                )(fn)
                metrics.tools_registered += 1
                metrics.tool_names.append(tool_name)
            except Exception as exc:
                metrics.tools_failed.append(f"module:{spec.source}")
                logger.warning("Failed to register tf module %s: %s", spec.source, exc)

        self._register_utility_tools(mcp, cfg.workspace, metrics)

        metrics.extra["workspace"] = str(cfg.workspace)
        metrics.extra["providers"] = sorted(cfg.providers)
        metrics.extra["resources_total"] = len(schemas)
        metrics.extra["modules_total"] = len(module_schemas)
        return metrics

    def _register_utility_tools(
        self, mcp: FastMCP, workspace: Path, metrics: CannonMetrics
    ) -> None:
        """Register tf_destroy, tf_state_list utility tools."""

        @mcp.tool(
            name="tf_destroy",
            description=(
                "Destroy a Terraform-managed resource by address (e.g. "
                "'docker_container.my_web'). Dependents are destroyed first."
            ),
            tags={"terraform", "rocannon.meta"},
        )
        def _destroy(address: str) -> str:
            # Module addresses (module.<name>) use destroy on the whole module;
            # resource addresses (type.name) use destroy_resource.
            if address.startswith("module."):
                # tofu destroy -target=module.<name> handles dependencies
                proc = subprocess.run(
                    [
                        "tofu",
                        f"-chdir={workspace}",
                        "destroy",
                        "-no-color",
                        "-input=false",
                        "-auto-approve",
                        f"-target={address}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if proc.returncode != 0:
                    return json.dumps(
                        {"ok": False, "error": proc.stderr.strip()},
                        default=str,
                    )
                return json.dumps(
                    {"ok": True, "address": address, "destroyed": True},
                    default=str,
                )
            return json.dumps(destroy_resource(workspace, address), default=str)

        @mcp.tool(
            name="tf_state_list",
            description="List all Terraform-managed resource addresses currently in state.",
            tags={"terraform", "rocannon.meta"},
        )
        def _state_list() -> list[str]:
            return state_list(workspace)

        metrics.tools_registered += 2
        metrics.tool_names.extend(["tf_destroy", "tf_state_list"])
