from pathlib import Path

import yaml


def load_inventories(paths: list[Path]) -> dict[str, dict]:
    """Load and merge multiple Ansible inventory files into a host-vars dict."""
    merged_hosts: dict[str, dict] = {}
    for path in paths:
        raw = yaml.safe_load(path.read_text())
        if not raw:
            continue
        _extract_hosts(raw, merged_hosts)
    return merged_hosts


def _extract_hosts(data: dict, hosts: dict[str, dict]) -> None:
    """Recursively extract hosts from inventory group structure."""
    if not isinstance(data, dict):
        return

    if "hosts" in data and isinstance(data["hosts"], dict):
        for hostname, hostvars in data["hosts"].items():
            hosts[hostname] = hostvars or {}

    if "children" in data and isinstance(data["children"], dict):
        for _group_name, group_data in data["children"].items():
            _extract_hosts(group_data, hosts)

    for key, value in data.items():
        if key in ("hosts", "children"):
            continue
        if isinstance(value, dict):
            _extract_hosts(value, hosts)


def get_valid_hosts(hosts: dict[str, dict]) -> set[str]:
    """Return the set of valid host names from merged inventory."""
    return set(hosts.keys())
