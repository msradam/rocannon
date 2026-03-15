import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("rocannon.inventory")


def load_inventory(paths: list[Path]) -> dict[str, list[str]]:
    """Query ansible-inventory for hosts and groups.

    Returns a dict with 'hosts' (list of hostnames) and 'groups' (list of group names).
    Delegates all inventory parsing, group_vars, Jinja2 resolution, etc. to Ansible.

    Uses subprocess rather than Python API because Ansible's internal import machinery
    (FileFinder hook check in _collection_finder.py) is incompatible with FastMCP
    in the same process.
    """
    cmd = ["ansible-inventory", "--list"]
    for p in paths:
        cmd.extend(["-i", str(p)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("ansible-inventory failed: %s", result.stderr.strip())
        return {"hosts": [], "groups": []}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse ansible-inventory output: %s", exc)
        return {"hosts": [], "groups": []}

    hosts = sorted(data.get("_meta", {}).get("hostvars", {}).keys())
    groups = sorted(
        k for k in data if k not in ("_meta", "all", "ungrouped") and data[k].get("hosts")
    )

    return {"hosts": hosts, "groups": groups}
