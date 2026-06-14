import json
import logging
import subprocess
from typing import Any

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


def expand_modules(specs: list[str]) -> list[str]:
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

    try:
        result = subprocess.run(
            ["ansible-doc", "--list", "--type", "module", "-j"],
            capture_output=True,
            text=True,
            check=True,
        )
        all_modules = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        logger.error("ansible-doc --list failed: %s, returning explicit modules only", exc)
        return explicit

    expanded: list[str] = explicit.copy()
    for prefix in prefixes:
        matched = [name for name in all_modules if name.startswith(prefix + ".")]
        expanded.extend(matched)

    return sorted(set(expanded))


def fetch_module_schema(module_name: str) -> dict[str, Any]:
    """Fetch and parse ansible-doc JSON for a single module."""
    from rocannon.executor import ensure_ansible_on_path

    ensure_ansible_on_path()
    result = subprocess.run(
        ["ansible-doc", "-t", "module", "-j", module_name],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise SchemaFetchError(
            f"ansible-doc failed for {module_name}: {result.stderr.strip() or 'no stderr'}"
        )

    if not result.stdout.strip():
        raise SchemaFetchError(f"ansible-doc returned empty output for {module_name}")

    try:
        doc = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SchemaFetchError(
            f"Failed to parse ansible-doc JSON for {module_name}: {exc}"
        ) from exc

    if module_name not in doc:
        raise SchemaFetchError(f"Module {module_name} not present in ansible-doc output")

    return _parse_module_doc(module_name, doc[module_name])


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
    }


def _parse_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Pull the execution-relevant flags out of ansible-doc's attributes block.

    ``check_mode``/``diff_mode`` carry a ``support`` level (full/partial/none);
    ``facts`` and ``raw`` are presence flags. These drive the MCP tool hints and
    the dry-run parameters built in ``rocannon.ansible``.
    """

    def support(key: str) -> str | None:
        entry = attributes.get(key)
        return entry.get("support") if isinstance(entry, dict) else None

    return {
        "check_mode": support("check_mode"),
        "diff_mode": support("diff_mode"),
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
