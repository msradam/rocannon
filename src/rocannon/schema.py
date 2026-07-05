import json
import logging
from typing import Any

import ansible_runner

logger = logging.getLogger("rocannon.schema")


class SchemaFetchError(RuntimeError):
    """Raised when ``ansible-doc`` cannot return a usable schema for a module."""


ANSIBLE_TYPE_MAP: dict[str, type] = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "bool": bool,
    "boolean": bool,
    "list": list,
    "dict": dict,
    "path": str,
    "raw": str,
    "jsonarg": str,
    "json": str,
    "bytes": str,
    "bits": str,
    "sid": str,
}


def _ee_kwargs(
    execution_environment: str | None,
    execution_environment_engine: str,
    execution_environment_container_options: list[str] | None,
) -> dict[str, Any]:
    """Build the process_isolation kwargs shared by every ``ansible_runner`` doc call.

    Mirrors ``executor.py``'s dispatch: when ``execution_environment`` is set, the
    ``ansible-doc`` invocation itself runs inside that image via ansible-runner's
    own container support, so discovery reflects the same collection set that
    execution runs against instead of whatever (if anything) is installed on
    Rocannon's own control-side environment.
    """
    if not execution_environment:
        return {}
    return {
        "process_isolation": True,
        "process_isolation_executable": execution_environment_engine,
        "container_image": execution_environment,
        "container_options": execution_environment_container_options or None,
    }


def expand_modules(
    specs: list[str],
    execution_environment: str | None = None,
    execution_environment_engine: str = "podman",
    execution_environment_container_options: list[str] | None = None,
) -> list[str]:
    """Expand module/collection/namespace specs into fully-qualified module names."""
    explicit: list[str] = []
    prefixes: list[str] = []

    for spec in specs:
        if spec.count(".") >= 2:
            explicit.append(spec)
        else:
            prefixes.append(spec)

    if not prefixes:
        return explicit

    ee_kwargs = _ee_kwargs(
        execution_environment, execution_environment_engine, execution_environment_container_options
    )
    try:
        all_modules, error = ansible_runner.get_plugin_list(
            response_format="json", plugin_type="module", quiet=True, **ee_kwargs
        )
    except json.JSONDecodeError as exc:
        logger.error(
            "ansible-doc --list returned unparsable JSON: %s, returning explicit modules only", exc
        )
        return explicit

    if not isinstance(all_modules, dict):
        logger.error(
            "ansible-doc --list failed: %s, returning explicit modules only",
            (error or "no output").strip(),
        )
        return explicit

    expanded: list[str] = explicit.copy()
    for prefix in prefixes:
        matched = [name for name in all_modules if name.startswith(prefix + ".")]
        expanded.extend(matched)

    return sorted(set(expanded))


def fetch_module_schema(
    module_name: str,
    execution_environment: str | None = None,
    execution_environment_engine: str = "podman",
    execution_environment_container_options: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch and parse ansible-doc JSON for a single module."""
    from rocannon.executor import ensure_ansible_on_path

    ensure_ansible_on_path()
    ee_kwargs = _ee_kwargs(
        execution_environment, execution_environment_engine, execution_environment_container_options
    )
    try:
        doc, error = ansible_runner.get_plugin_docs(
            [module_name], plugin_type="module", response_format="json", quiet=True, **ee_kwargs
        )
    except json.JSONDecodeError as exc:
        raise SchemaFetchError(
            f"Failed to parse ansible-doc JSON for {module_name}: {exc}"
        ) from exc

    if not isinstance(doc, dict) or not doc:
        raise SchemaFetchError(
            f"ansible-doc failed for {module_name}: {(error or 'no output').strip() or 'no stderr'}"
        )

    if module_name not in doc:
        raise SchemaFetchError(f"Module {module_name} not present in ansible-doc output")

    return _parse_module_doc(module_name, doc[module_name])


# ansible-doc pays ~0.2s of Python/Ansible import startup per invocation, which
# dominates schema loading. It accepts many module names per call and returns a
# JSON object keyed by name, so batching amortizes that startup across the whole
# set. Chunk to keep the argument list well under OS limits for huge collections.
_DOC_BATCH_SIZE = 256


def _fetch_individually(
    names: list[str],
    into: dict[str, dict[str, Any]],
    execution_environment: str | None,
    execution_environment_engine: str,
    execution_environment_container_options: list[str] | None,
) -> None:
    for name in names:
        try:
            into[name] = fetch_module_schema(
                name,
                execution_environment=execution_environment,
                execution_environment_engine=execution_environment_engine,
                execution_environment_container_options=execution_environment_container_options,
            )
        except SchemaFetchError as exc:
            logger.warning("Skipping %s: %s", name, exc)


def fetch_module_schemas(
    module_names: list[str],
    execution_environment: str | None = None,
    execution_environment_engine: str = "podman",
    execution_environment_container_options: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch and parse ansible-doc JSON for many modules in one pass per chunk.

    Returns a mapping of module name to parsed schema. Names absent from
    ansible-doc's output (renamed, not actually a module) are omitted; the
    caller decides what a missing schema means. Falls back to per-module fetches
    for any chunk whose batched call fails, so one unusable module never blocks
    the rest.
    """
    names = list(dict.fromkeys(module_names))  # de-dup, preserve order
    if not names:
        return {}

    from rocannon.executor import ensure_ansible_on_path

    ensure_ansible_on_path()
    ee_kwargs = _ee_kwargs(
        execution_environment, execution_environment_engine, execution_environment_container_options
    )
    schemas: dict[str, dict[str, Any]] = {}
    for i in range(0, len(names), _DOC_BATCH_SIZE):
        chunk = names[i : i + _DOC_BATCH_SIZE]
        try:
            doc, error = ansible_runner.get_plugin_docs(
                chunk, plugin_type="module", response_format="json", quiet=True, **ee_kwargs
            )
        except json.JSONDecodeError:
            logger.warning("Unparsable batched ansible-doc output; retrying %d singly", len(chunk))
            _fetch_individually(
                chunk,
                schemas,
                execution_environment,
                execution_environment_engine,
                execution_environment_container_options,
            )
            continue
        if not isinstance(doc, dict) or not doc:
            logger.warning(
                "Batched ansible-doc failed for %d modules (%s); retrying singly",
                len(chunk),
                (error or "no output").strip(),
            )
            _fetch_individually(
                chunk,
                schemas,
                execution_environment,
                execution_environment_engine,
                execution_environment_container_options,
            )
            continue
        for name in chunk:
            entry = doc.get(name)
            if entry is not None:
                schemas[name] = _parse_module_doc(name, entry)
    return schemas


def _parse_module_doc(module_name: str, module_doc: dict[str, Any]) -> dict[str, Any]:
    """Convert ansible-doc output into a structured schema dict."""
    doc_entry = module_doc.get("doc", {})
    description = doc_entry.get("short_description", module_name)
    options = doc_entry.get("options", {}) or {}

    parameters: list[dict[str, Any]] = []
    for param_name, param_info in options.items():
        if not isinstance(param_info, dict):
            continue
        param = _parse_parameter(param_name, param_info)
        parameters.append(param)

    return {
        "name": module_name,
        "description": _flatten_description(description),
        "parameters": parameters,
        "attributes": _parse_attributes(doc_entry.get("attributes") or {}),
        "meta": _build_meta(module_doc, doc_entry),
    }


def fetch_role_schemas(
    role_names: list[str],
    roles_path: str | None = None,
    execution_environment: str | None = None,
    execution_environment_engine: str = "podman",
    execution_environment_container_options: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch and parse ansible-doc JSON for roles, like modules but ``-t role``.

    A role with a ``meta/argument_specs.yml`` is documented the same way a
    module is: ``entry_points.<name>.options`` mirrors ``doc.options``. Only the
    ``main`` entry point is mapped. Roles without a documented argspec produce no
    schema and are skipped; the caller reports the gap.

    ``execution_environment`` only applies when ``roles_path`` is unset: a
    standalone role directory is host-local, so ansible-doc must see it
    directly rather than through a container that was never given that path.
    A collection-qualified role (FQCN, no ``roles_path``) is expected to
    already live in the image, same as a module.
    """
    names = list(dict.fromkeys(role_names))
    if not names:
        return {}

    from rocannon.executor import ensure_ansible_on_path

    ensure_ansible_on_path()
    if roles_path:
        kwargs: dict[str, Any] = {"envvars": {"ANSIBLE_ROLES_PATH": roles_path}}
    else:
        kwargs = _ee_kwargs(
            execution_environment,
            execution_environment_engine,
            execution_environment_container_options,
        )
    schemas: dict[str, dict[str, Any]] = {}
    for i in range(0, len(names), _DOC_BATCH_SIZE):
        chunk = names[i : i + _DOC_BATCH_SIZE]
        try:
            doc, error = ansible_runner.get_plugin_docs(
                chunk, plugin_type="role", response_format="json", quiet=True, **kwargs
            )
        except json.JSONDecodeError:
            logger.warning("Unparsable ansible-doc -t role output for %d role(s)", len(chunk))
            continue
        if not isinstance(doc, dict) or not doc:
            logger.warning(
                "ansible-doc -t role failed for %d role(s): %s",
                len(chunk),
                (error or "no output").strip(),
            )
            continue
        for name in chunk:
            parsed = _parse_role_doc(name, doc.get(name, {}))
            if parsed is not None:
                schemas[name] = parsed
    return schemas


def _parse_role_doc(
    role_name: str, role_doc: dict[str, Any], entry_point: str = "main"
) -> dict[str, Any] | None:
    """Parse a role's ``main`` entry point into the same schema shape as a module.

    Returns None when the role has no documented entry point (no
    argument_specs), which is how a role becomes untyped and is skipped.
    """
    entry = (role_doc.get("entry_points") or {}).get(entry_point)
    if not isinstance(entry, dict):
        return None
    options = entry.get("options") or {}
    parameters = [
        _parse_parameter(name, info) for name, info in options.items() if isinstance(info, dict)
    ]
    description = entry.get("short_description") or f"Ansible role {role_name}"
    return {
        "name": role_name,
        "description": _flatten_description(description),
        "parameters": parameters,
        "attributes": {},
        "meta": {"kind": "role", "entry_point": entry_point},
        "is_role": True,
        "entry_point": entry_point,
    }


def _build_meta(module_doc: dict[str, Any], doc_entry: dict[str, Any]) -> dict[str, Any]:
    """Pass through the descriptive metadata ansible-doc already provides.

    None of this drives execution; it travels in the tool's MCP ``meta`` so a
    client or model can see a module's Python requirements, when it was added,
    whether it is deprecated, related modules, and the documented return keys.
    """
    meta: dict[str, Any] = {}
    requirements = doc_entry.get("requirements")
    if requirements:
        meta["requirements"] = requirements
    version_added = doc_entry.get("version_added")
    if version_added and version_added != "historical":
        meta["version_added"] = str(version_added)
    if doc_entry.get("deprecated"):
        meta["deprecated"] = True
    seealso = doc_entry.get("seealso") or []
    related = [s["module"] for s in seealso if isinstance(s, dict) and s.get("module")]
    if related:
        meta["seealso"] = related
    return_block = module_doc.get("return")
    if isinstance(return_block, dict) and return_block:
        meta["returns"] = sorted(return_block)
    return meta


def _parse_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Pull the execution-relevant flags out of ansible-doc's attributes block.

    ``check_mode``/``diff_mode``/``idempotent`` carry a ``support`` level
    (full/partial/none/N/A); ``facts`` and ``raw`` are presence flags. These drive
    the MCP tool hints and the dry-run parameters built in ``rocannon.ansible``.
    """

    def support(key: str) -> str | None:
        entry = attributes.get(key)
        return entry.get("support") if isinstance(entry, dict) else None

    return {
        "check_mode": support("check_mode"),
        "diff_mode": support("diff_mode"),
        "idempotent": support("idempotent"),
        "facts": "facts" in attributes,
        "raw": "raw" in attributes,
    }


def _parse_parameter(param_name: str, param_info: dict[str, Any]) -> dict[str, Any]:
    """Parse a single parameter from ansible-doc options."""
    desc = _flatten_description(param_info.get("description", ""))

    if param_info.get("aliases"):
        desc += f" (aliases: {', '.join(param_info['aliases'])})"

    if param_info.get("deprecated"):
        dep = param_info["deprecated"]
        dep_msg = dep.get("why", "deprecated") if isinstance(dep, dict) else "deprecated"
        desc += f" [DEPRECATED: {dep_msg}]"

    if "suboptions" in param_info and isinstance(param_info["suboptions"], dict):
        sub_desc = _describe_suboptions(param_info["suboptions"])
        desc += f" Suboptions: {sub_desc}"

    param: dict[str, Any] = {
        "name": param_name,
        "description": desc,
        "required": param_info.get("required", False),
    }

    if "default" in param_info:
        param["default"] = param_info["default"]
    if "choices" in param_info:
        param["choices"] = param_info["choices"]
    if "type" in param_info:
        param["type"] = param_info["type"]
    if param_info.get("elements"):
        param["elements"] = param_info["elements"]

    return param


def _describe_suboptions(suboptions: dict[str, Any]) -> str:
    """Flatten suboptions into a human-readable string for tool descriptions."""
    parts = []
    for name, info in suboptions.items():
        if not isinstance(info, dict):
            continue
        part = name
        if info.get("required"):
            part += " (required)"
        if info.get("type"):
            part += f": {info['type']}"
        sub_desc = _flatten_description(info.get("description", ""))
        if sub_desc:
            part += f", {sub_desc[:80]}"
        parts.append(part)
    return "{" + ", ".join(parts) + "}"


def _flatten_description(desc: Any) -> str:
    """Normalize description field to a single string."""
    if isinstance(desc, list):
        return " ".join(str(item) for item in desc)
    return str(desc)
