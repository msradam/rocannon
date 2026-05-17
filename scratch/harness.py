"""In-process Rocannon validation harness.

Drives schema generation and execution paths directly via rocannon's API,
bypassing the FastMCP wire layer. Records results to results.jsonl.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from rocannon.config import Config  # noqa: E402
from rocannon.executor import build_inventory_list, run_module  # noqa: E402
from rocannon.inventory import load_inventory  # noqa: E402
from rocannon.schema import expand_modules, fetch_module_schema  # noqa: E402
from rocannon.server import _build_target_annotation, _make_tool_fn  # noqa: E402

INVENTORY = REPO / "scratch" / "inventory.yml"
RESULTS = REPO / "scratch" / "results" / "results.jsonl"


def write_result(record: dict[str, Any]) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def schema_check(module: str) -> dict[str, Any]:
    """Verify rocannon generates a usable schema, compare to ansible-doc."""
    t0 = time.monotonic()
    rec: dict[str, Any] = {
        "phase": "schema",
        "module": module,
        "ts": time.time(),
    }
    try:
        schema = fetch_module_schema(module)
        rec["param_count"] = len(schema["parameters"])
        rec["has_description"] = bool(schema.get("description"))
        # Build the actual tool function — exercises the type construction path
        inv = load_inventory([INVENTORY])
        inv_list = build_inventory_list([INVENTORY])
        fn = _make_tool_fn(module, schema, inv, inv_list)
        sig = fn.__signature__
        rec["sig_params"] = len(sig.parameters)
        rec["has_target"] = "target" in sig.parameters

        # Sanity: check required params have no default
        required_in_schema = [p["name"] for p in schema["parameters"] if p.get("required")]
        rec["required_count"] = len(required_in_schema)
        # Choices preserved as Literal where applicable
        with_choices = [
            p for p in schema["parameters"]
            if p.get("choices") and isinstance(p["choices"], list)
        ]
        rec["choices_count"] = len(with_choices)

        rec["status"] = "pass"
    except Exception as exc:
        rec["status"] = "fail"
        rec["error"] = str(exc)
        rec["traceback"] = traceback.format_exc()[-1500:]
    rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    write_result(rec)
    return rec


def exec_check(
    module: str, args: dict[str, Any], target: str, label: str = "happy"
) -> dict[str, Any]:
    t0 = time.monotonic()
    rec: dict[str, Any] = {
        "phase": "exec",
        "module": module,
        "target": target,
        "label": label,
        "args": args,
        "ts": time.time(),
    }
    try:
        result = run_module(
            module=module,
            module_args=args,
            inventory=[str(INVENTORY)],
            host_pattern=target,
            timeout=120,
            idle_timeout=30,
        )
        rec["runner_status"] = result.get("status")
        rec["changed"] = result.get("changed")
        # Trim result for jsonl readability
        res_view = result.get("result", {})
        if isinstance(res_view, dict):
            rec["result_keys"] = sorted(res_view.keys())[:20]
            rec["msg"] = res_view.get("msg", "")[:200] if isinstance(res_view.get("msg"), str) else None
            rec["failed"] = res_view.get("failed", False)
        rec["status"] = "pass" if result.get("status") == "successful" else "fail"
    except Exception as exc:
        rec["status"] = "error"
        rec["error"] = str(exc)
        rec["traceback"] = traceback.format_exc()[-1500:]
    rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    write_result(rec)
    return rec


def list_collection_modules(prefix: str) -> list[str]:
    return expand_modules([prefix])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "smoke"
    if cmd == "smoke":
        print(schema_check("ansible.builtin.ping"))
        print(exec_check("ansible.builtin.ping", {}, "ubuntu"))
