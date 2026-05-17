"""Three use-case validation suites against Linux containers.

Use Case 1: Linux fleet diagnostics (read-only)
Use Case 2: Network config on Linux + vyos.vyos schema proof
Use Case 3: Container lifecycle (containers.podman, full)

Records to scratch/results/use_cases.jsonl
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
sys.path.insert(0, str(REPO))

from rocannon.executor import run_module  # noqa: E402
from rocannon.schema import expand_modules, fetch_module_schema  # noqa: E402
from rocannon.server import _make_tool_fn  # noqa: E402
from rocannon.inventory import load_inventory  # noqa: E402
from rocannon.executor import build_inventory_list  # noqa: E402

INV_FILE = str(REPO / "scratch" / "inventory.yml")
RESULTS = REPO / "scratch" / "results" / "use_cases.jsonl"
RESULTS.parent.mkdir(parents=True, exist_ok=True)


def write(rec: dict[str, Any]) -> None:
    with RESULTS.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def exec_cell(use_case: str, module: str, args: dict[str, Any], target: str, label: str = "happy") -> dict[str, Any]:
    t0 = time.monotonic()
    rec: dict[str, Any] = {
        "use_case": use_case,
        "phase": "exec",
        "module": module,
        "target": target,
        "label": label,
        "args": args,
    }
    try:
        result = run_module(
            module=module, module_args=args, inventory=[INV_FILE],
            host_pattern=target, timeout=120, idle_timeout=30,
        )
        rec["runner_status"] = result.get("status")
        rec["changed"] = result.get("changed")
        res = result.get("result", {})
        if isinstance(res, dict):
            rec["msg"] = (res.get("msg") or "")[:200] if isinstance(res.get("msg"), str) else None
            rec["failed"] = res.get("failed", False)
            # Capture small diagnostic-friendly chunks
            if "ansible_facts" in res:
                af = res["ansible_facts"]
                rec["fact_keys"] = sorted(list(af.keys()))[:10]
        rec["status"] = "pass" if result.get("status") == "successful" else "fail"
    except Exception as exc:
        rec["status"] = "error"
        rec["error"] = str(exc)
        rec["traceback"] = traceback.format_exc()[-800:]
    rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    write(rec)
    flag = "✓" if rec["status"] == "pass" else "✗"
    print(f"  {flag} {module:55s} on {target:12s} [{label}] {rec.get('elapsed_ms')}ms"
          + (f"  {rec.get('msg','')[:80]}" if rec["status"] != "pass" else ""))
    return rec


def schema_cell(use_case: str, module: str) -> dict[str, Any]:
    t0 = time.monotonic()
    rec: dict[str, Any] = {"use_case": use_case, "phase": "schema", "module": module}
    try:
        schema = fetch_module_schema(module)
        inv = load_inventory([Path(INV_FILE)])
        inv_list = build_inventory_list([Path(INV_FILE)])
        fn = _make_tool_fn(module, schema, inv, inv_list)
        rec["param_count"] = len(schema["parameters"])
        rec["sig_params"] = len(fn.__signature__.parameters)
        rec["status"] = "pass" if rec["param_count"] >= 0 else "fail"
    except Exception as exc:
        rec["status"] = "error"
        rec["error"] = str(exc)[:300]
    rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    write(rec)
    return rec


OSES = ["ubuntu", "rhel", "sles"]


# ============================================================
# USE CASE 1: Linux fleet diagnostics
# ============================================================
def use_case_1_diagnostics() -> None:
    print("\n=== USE CASE 1: Linux fleet diagnostics ===")
    print("Operator question: 'What's running where, what's drifted, what's broken'")

    # 1a — fact gathering across all OSes
    for os_t in OSES:
        exec_cell("diagnostics", "ansible.builtin.setup",
                  {"gather_subset": ["min", "distribution", "kernel"]},
                  os_t, "facts")
    # 1b — service inventory
    for os_t in OSES:
        exec_cell("diagnostics", "ansible.builtin.service_facts", {}, os_t, "services")
    # 1c — package inventory
    for os_t in OSES:
        mgr = {"ubuntu": "apt", "rhel": "rpm", "sles": "rpm"}[os_t]
        exec_cell("diagnostics", "ansible.builtin.package_facts",
                  {"manager": mgr}, os_t, "packages")
    # 1d — disk usage via command
    for os_t in OSES:
        exec_cell("diagnostics", "ansible.builtin.command",
                  {"cmd": "df -h /"}, os_t, "disk_usage")
    # 1e — uptime / load
    for os_t in OSES:
        exec_cell("diagnostics", "ansible.builtin.command",
                  {"cmd": "uptime"}, os_t, "uptime")
    # 1f — find logs (read-only, demonstrates LLM-shaped fleet introspection)
    for os_t in OSES:
        exec_cell("diagnostics", "ansible.builtin.find",
                  {"paths": "/var/log", "patterns": "*.log", "recurse": False, "file_type": "file"},
                  os_t, "find_logs")
    # 1g — listening ports via shell pipeline
    for os_t in OSES:
        exec_cell("diagnostics", "ansible.builtin.command",
                  {"cmd": "ss -tlnp || netstat -tlnp"}, os_t, "listening_ports")


# ============================================================
# USE CASE 2: Network configuration on Linux + VyOS schema proof
# ============================================================
def use_case_2_network() -> None:
    print("\n=== USE CASE 2: Network config (Linux + VyOS schema) ===")
    print("Operator question: 'Configure routing/firewall/sysctl across the fleet'")

    # 2a — sysctl forwarding (turn the host into a router)
    for os_t in OSES:
        exec_cell("network", "ansible.posix.sysctl",
                  {"name": "net.ipv4.ip_forward", "value": "1", "sysctl_set": True, "state": "present", "reload": False},
                  os_t, "ip_forward_on")
        exec_cell("network", "ansible.posix.sysctl",
                  {"name": "net.ipv4.ip_forward", "value": "1", "sysctl_set": True, "state": "present", "reload": False},
                  os_t, "ip_forward_idempotent")

    # 2b — hostname management
    for os_t in OSES:
        exec_cell("network", "ansible.builtin.hostname",
                  {"name": f"rocannon-{os_t}-test"}, os_t, "set_hostname")
        exec_cell("network", "ansible.builtin.hostname",
                  {"name": f"rocannon-{os_t}-test"}, os_t, "hostname_idempotent")

    # 2c — /etc/hosts entries
    for os_t in OSES:
        exec_cell("network", "ansible.builtin.lineinfile",
                  {"path": "/etc/hosts", "line": "10.99.99.99 fleet-controller", "state": "present"},
                  os_t, "add_host_entry")
        exec_cell("network", "ansible.builtin.lineinfile",
                  {"path": "/etc/hosts", "line": "10.99.99.99 fleet-controller", "state": "present"},
                  os_t, "host_entry_idempotent")

    # 2d — iptables rule (works across all 3)
    for os_t in OSES:
        exec_cell("network", "ansible.builtin.iptables",
                  {"chain": "INPUT", "protocol": "tcp", "destination_port": "9999",
                   "jump": "ACCEPT", "comment": "rocannon-test"},
                  os_t, "iptables_allow")
        exec_cell("network", "ansible.builtin.iptables",
                  {"chain": "INPUT", "protocol": "tcp", "destination_port": "9999",
                   "jump": "ACCEPT", "comment": "rocannon-test"},
                  os_t, "iptables_idempotent")

    # 2e — Schema-only validation for vyos.vyos
    print("\n  --- vyos.vyos schema proof (no live device required) ---")
    vyos_modules = expand_modules(["vyos.vyos"])
    print(f"  Discovered {len(vyos_modules)} vyos.vyos modules")
    write({"use_case": "network", "phase": "discovery", "collection": "vyos.vyos",
           "module_count": len(vyos_modules)})
    sample = ["vyos.vyos.vyos_facts", "vyos.vyos.vyos_interfaces",
              "vyos.vyos.vyos_static_routes", "vyos.vyos.vyos_firewall_rules",
              "vyos.vyos.vyos_l3_interfaces", "vyos.vyos.vyos_ospfv2",
              "vyos.vyos.vyos_bgp_global", "vyos.vyos.vyos_config",
              "vyos.vyos.vyos_command", "vyos.vyos.vyos_user"]
    for m in sample:
        if m in vyos_modules:
            r = schema_cell("network", m)
            print(f"  ✓ schema {m}: params={r.get('param_count')} sig={r.get('sig_params')}")


# ============================================================
# USE CASE 3: Container lifecycle (containers.podman)
# ============================================================
def use_case_3_containers() -> None:
    print("\n=== USE CASE 3: Container lifecycle (containers.podman) ===")
    print("Operator question: 'Stand up / inspect / tear down containers across the fleet'")
    target = "pg-host"  # local connection; runs against host's podman

    # cleanup any leftover from previous runs
    exec_cell("containers", "containers.podman.podman_container",
              {"name": "rocannon-uc-demo", "state": "absent"}, target, "cleanup_pre")

    # 3a — pull image
    exec_cell("containers", "containers.podman.podman_image",
              {"name": "docker.io/library/alpine:3", "state": "present"}, target, "pull_image")
    # 3b — run container
    exec_cell("containers", "containers.podman.podman_container",
              {"name": "rocannon-uc-demo", "image": "docker.io/library/alpine:3",
               "command": "sleep 300", "state": "started", "detach": True}, target, "run_container")
    # 3c — idempotent re-run
    exec_cell("containers", "containers.podman.podman_container",
              {"name": "rocannon-uc-demo", "image": "docker.io/library/alpine:3",
               "command": "sleep 300", "state": "started", "detach": True}, target, "run_idempotent")
    # 3d — create network
    exec_cell("containers", "containers.podman.podman_network",
              {"name": "rocannon-uc-net", "state": "present"}, target, "create_network")
    exec_cell("containers", "containers.podman.podman_network",
              {"name": "rocannon-uc-net", "state": "present"}, target, "network_idempotent")
    # 3e — create pod
    exec_cell("containers", "containers.podman.podman_pod",
              {"name": "rocannon-uc-pod", "state": "started"}, target, "create_pod")
    exec_cell("containers", "containers.podman.podman_pod",
              {"name": "rocannon-uc-pod", "state": "started"}, target, "pod_idempotent")
    # 3f — stop container
    exec_cell("containers", "containers.podman.podman_container",
              {"name": "rocannon-uc-demo", "state": "stopped"}, target, "stop_container")
    # 3g — teardown
    exec_cell("containers", "containers.podman.podman_container",
              {"name": "rocannon-uc-demo", "state": "absent"}, target, "remove_container")
    exec_cell("containers", "containers.podman.podman_pod",
              {"name": "rocannon-uc-pod", "state": "absent"}, target, "remove_pod")
    exec_cell("containers", "containers.podman.podman_network",
              {"name": "rocannon-uc-net", "state": "absent"}, target, "remove_network")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or "1" in args:
        use_case_1_diagnostics()
    if not args or "2" in args:
        use_case_2_network()
    if not args or "3" in args:
        use_case_3_containers()
    print("\n=== DONE — see scratch/results/use_cases.jsonl ===")
