import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import ansible_runner  # type: ignore[import-untyped]
import yaml

DEFAULT_TIMEOUT = 300
DEFAULT_IDLE_TIMEOUT = 60


def run_module(
    module: str,
    module_args: dict[str, Any],
    inventory: list[str],
    host_pattern: str,
    timeout: int = DEFAULT_TIMEOUT,
    idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
) -> dict[str, Any]:
    """Execute an Ansible module via ansible-runner and return structured results."""
    abs_inventory = []
    for inv_path in inventory:
        p = Path(inv_path)
        if not p.is_absolute():
            p = Path.cwd() / p
        abs_inventory.append(str(p))

    # environment_vars is resolved from inventory by Ansible at runtime
    play: dict[str, Any] = {
        "hosts": host_pattern,
        "gather_facts": False,
        "environment": "{{ environment_vars | default({}) }}",
        "tasks": [
            {
                "name": f"Execute {module}",
                module: module_args or {},
            }
        ],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.dump([play], f)
        playbook_path = f.name

    try:
        runner = ansible_runner.run(
            playbook=playbook_path,
            inventory=abs_inventory,
            quiet=True,
            timeout=timeout,
            settings={"idle_timeout": idle_timeout},
        )
    except Exception as exc:
        return {
            "status": "error",
            "changed": False,
            "result": {},
            "stdout": "",
            "stderr": str(exc),
        }
    finally:
        with contextlib.suppress(Exception):
            os.unlink(playbook_path)

    return _parse_runner_result(runner)


def build_inventory_list(inventory_paths: list[Path]) -> list[str]:
    """Convert inventory Path objects to strings for ansible-runner."""
    return [str(p) for p in inventory_paths]


def _parse_runner_result(runner: Any) -> dict[str, Any]:
    """Extract structured results from ansible-runner events.

    Collects results from all hosts. Returns a single-host format when only
    one host responded, and a per-host format when multiple hosts responded.
    """
    host_results: dict[str, dict[str, Any]] = {}

    for event in runner.events:
        event_data = event.get("event_data", {})
        res = event_data.get("res")
        host = event_data.get("host")
        if res is not None and host is not None:
            host_results[host] = {
                "changed": res.get("changed", False),
                "result": res,
                "stdout": res.get("stdout", ""),
                "stderr": res.get("stderr", ""),
            }

    if not host_results:
        return {
            "status": runner.status,
            "changed": False,
            "result": {},
            "stdout": runner.stdout.read() if runner.stdout else "",
            "stderr": runner.stderr.read() if runner.stderr else "",
        }

    if len(host_results) == 1:
        host_data = next(iter(host_results.values()))
        return {"status": runner.status, **host_data}

    return {
        "status": runner.status,
        "changed": any(h["changed"] for h in host_results.values()),
        "hosts": host_results,
    }
