import json
from pathlib import Path
from typing import Any

import ansible_runner


def run_module(
    module: str,
    module_args: dict[str, Any],
    inventory: list[str],
    host_pattern: str,
) -> dict[str, Any]:
    """Execute an Ansible module via ansible-runner and return structured results."""
    args = dict(module_args)
    free_form = args.pop("_raw_params", None) or args.pop("cmd", None)

    if free_form and not args:
        args_str = str(free_form)
    else:
        if free_form:
            args["_raw_params"] = free_form
        args_str = json.dumps(args) if args else ""

    try:
        runner = ansible_runner.run(
            module=module,
            module_args=args_str,
            inventory=inventory,
            host_pattern=host_pattern,
            quiet=True,
        )
    except Exception as exc:
        return {
            "status": "error",
            "changed": False,
            "result": {},
            "stdout": "",
            "stderr": str(exc),
        }

    return _parse_runner_result(runner)


def build_inventory_list(inventory_paths: list[Path]) -> list[str]:
    """Convert inventory Path objects to strings for ansible-runner."""
    return [str(p) for p in inventory_paths]


def _parse_runner_result(runner: ansible_runner.Runner) -> dict[str, Any]:
    """Extract structured result from ansible-runner events."""
    for event in runner.events:
        event_data = event.get("event_data", {})
        res = event_data.get("res")
        if res is not None:
            return {
                "status": runner.status,
                "changed": res.get("changed", False),
                "result": res,
                "stdout": res.get("stdout", ""),
                "stderr": res.get("stderr", ""),
            }

    return {
        "status": runner.status,
        "changed": False,
        "result": {},
        "stdout": runner.stdout.read() if runner.stdout else "",
        "stderr": runner.stderr.read() if runner.stderr else "",
    }
