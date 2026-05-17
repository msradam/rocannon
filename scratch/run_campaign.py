"""Full validation campaign — schema + execution across collections, OSes."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scratch.harness import (  # noqa: E402
    exec_check,
    list_collection_modules,
    schema_check,
)

OSES = ["ubuntu", "rhel", "sles"]

# (collection prefix, modules to spot-check schemas for)
SCHEMA_TARGETS = {
    "ansible.builtin": [
        "ansible.builtin.ping",
        "ansible.builtin.setup",
        "ansible.builtin.command",
        "ansible.builtin.shell",
        "ansible.builtin.copy",
        "ansible.builtin.file",
        "ansible.builtin.service",
        "ansible.builtin.systemd",
        "ansible.builtin.package",
        "ansible.builtin.user",
        "ansible.builtin.group",
        "ansible.builtin.template",
    ],
    "community.general": [
        "community.general.timezone",
        "community.general.hostname",
        "community.general.cron",
        "community.general.archive",
        "community.general.unarchive",
        "community.general.ufw",
    ],
    "community.docker": [
        "community.docker.docker_container",
        "community.docker.docker_image",
        "community.docker.docker_network",
        "community.docker.docker_volume",
    ],
    "containers.podman": [
        "containers.podman.podman_container",
        "containers.podman.podman_image",
        "containers.podman.podman_network",
        "containers.podman.podman_pod",
    ],
    "community.mongodb": [
        "community.mongodb.mongodb_user",
        "community.mongodb.mongodb_shard",
        "community.mongodb.mongodb_replicaset",
    ],
    "community.postgresql": [
        "community.postgresql.postgresql_db",
        "community.postgresql.postgresql_user",
        "community.postgresql.postgresql_privs",
        "community.postgresql.postgresql_query",
    ],
}


def phase_schema() -> None:
    print("=== SCHEMA PHASE ===")
    # Full collection counts
    counts: dict[str, int] = {}
    for col in list(SCHEMA_TARGETS) + ["kubernetes.core"]:
        modules = list_collection_modules(col)
        counts[col] = len(modules)
        print(f"  {col}: {len(modules)} modules discoverable via ansible-doc")
    Path(REPO / "scratch" / "results" / "collection_counts.json").write_text(
        json.dumps(counts, indent=2)
    )

    # Schema generation per curated module
    for col, modules in SCHEMA_TARGETS.items():
        for m in modules:
            r = schema_check(m)
            print(f"  schema {m}: {r['status']} params={r.get('param_count','?')} elapsed={r.get('elapsed_ms')}ms")


def phase_exec_builtin() -> None:
    print("=== EXEC: ansible.builtin across 3 OSes ===")
    cells = [
        ("ansible.builtin.ping", {}, "happy"),
        ("ansible.builtin.command", {"_raw_params": "echo hello"}, "freeform"),
        ("ansible.builtin.shell", {"_raw_params": "echo $HOME"}, "freeform"),
        ("ansible.builtin.file", {"path": "/tmp/rocannon-test", "state": "directory"}, "happy"),
        ("ansible.builtin.file", {"path": "/tmp/rocannon-test", "state": "directory"}, "idempotent"),
        ("ansible.builtin.copy", {"content": "hello\n", "dest": "/tmp/rocannon-test/file.txt"}, "happy"),
        ("ansible.builtin.copy", {"content": "hello\n", "dest": "/tmp/rocannon-test/file.txt"}, "idempotent"),
    ]
    for os_target in OSES:
        for module, args, label in cells:
            r = exec_check(module, args, os_target, label=label)
            print(f"  {module} on {os_target} [{label}]: {r['status']} changed={r.get('changed')} {r.get('elapsed_ms')}ms")


def phase_exec_general() -> None:
    print("=== EXEC: community.general across 3 OSes ===")
    cells = [
        ("community.general.timezone", {"name": "UTC"}, "happy"),
        ("community.general.cron", {"name": "rocannon-test", "minute": "0", "job": "echo test"}, "happy"),
        ("community.general.cron", {"name": "rocannon-test", "minute": "0", "job": "echo test"}, "idempotent"),
        ("community.general.archive", {"path": "/etc/hostname", "dest": "/tmp/rocannon.tar.gz", "format": "gz"}, "happy"),
    ]
    for os_target in OSES:
        for module, args, label in cells:
            r = exec_check(module, args, os_target, label=label)
            print(f"  {module} on {os_target} [{label}]: {r['status']} {r.get('elapsed_ms')}ms")


def phase_exec_failures() -> None:
    print("=== EXEC: failure modes ===")
    # bad-param: required missing
    r = exec_check("ansible.builtin.copy", {"dest": "/tmp/x"}, "ubuntu", label="missing_required")
    print(f"  missing required: {r['status']} msg={r.get('msg')}")
    # bad enum
    r = exec_check("ansible.builtin.file", {"path": "/tmp/x", "state": "BOGUS"}, "ubuntu", label="bad_enum")
    print(f"  bad enum: {r['status']} msg={r.get('msg')}")
    # unreachable target — not in inventory; ansible-runner will treat as host pattern
    r = exec_check("ansible.builtin.ping", {}, "nonexistent-host", label="unreachable")
    print(f"  unreachable: {r['status']} msg={r.get('msg')}")
    # Module-internal error
    r = exec_check("ansible.builtin.command", {"_raw_params": "false"}, "ubuntu", label="module_error")
    print(f"  module error (false): {r['status']} msg={r.get('msg')}")


def phase_exec_postgres() -> None:
    print("=== EXEC: community.postgresql against pg-host (local) ===")
    cells = [
        ("community.postgresql.postgresql_db",
         {"name": "rocannon_test", "login_host": "127.0.0.1", "login_password": "rocannon", "login_user": "postgres"},
         "happy"),
        ("community.postgresql.postgresql_db",
         {"name": "rocannon_test", "login_host": "127.0.0.1", "login_password": "rocannon", "login_user": "postgres"},
         "idempotent"),
        ("community.postgresql.postgresql_query",
         {"db": "postgres", "query": "SELECT 1", "login_host": "127.0.0.1", "login_password": "rocannon", "login_user": "postgres"},
         "happy"),
        ("community.postgresql.postgresql_user",
         {"name": "rocannon", "password": "secret", "db": "postgres",
          "login_host": "127.0.0.1", "login_password": "rocannon", "login_user": "postgres"},
         "happy"),
    ]
    for module, args, label in cells:
        r = exec_check(module, args, "pg-host", label=label)
        print(f"  {module} [{label}]: {r['status']} changed={r.get('changed')} msg={r.get('msg')}")


def phase_exec_mongodb() -> None:
    print("=== EXEC: community.mongodb against mongo-host (local) ===")
    cells = [
        ("community.mongodb.mongodb_user",
         {"login_host": "127.0.0.1", "login_port": 27017,
          "database": "test", "name": "rocannon", "password": "secret",
          "roles": "readWrite", "state": "present"},
         "happy"),
    ]
    for module, args, label in cells:
        r = exec_check(module, args, "mongo-host", label=label)
        print(f"  {module} [{label}]: {r['status']} msg={r.get('msg')}")


def phase_exec_podman() -> None:
    print("=== EXEC: containers.podman (local; needs podman binary) ===")
    cells = [
        ("containers.podman.podman_image",
         {"name": "docker.io/library/alpine:3", "state": "present"}, "happy"),
        ("containers.podman.podman_image",
         {"name": "docker.io/library/alpine:3", "state": "present"}, "idempotent"),
    ]
    for module, args, label in cells:
        r = exec_check(module, args, "pg-host", label=label)
        print(f"  {module} [{label}]: {r['status']} msg={r.get('msg')}")


def phase_exec_docker() -> None:
    print("=== EXEC: community.docker — using docker SDK against podman socket ===")
    # Likely to fail without docker SDK / sock; record outcome
    cells = [
        ("community.docker.docker_image",
         {"name": "docker.io/library/alpine:3", "source": "pull"}, "happy"),
    ]
    for module, args, label in cells:
        r = exec_check(module, args, "pg-host", label=label)
        print(f"  {module} [{label}]: {r['status']} msg={r.get('msg')}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or "schema" in args:
        phase_schema()
    if not args or "builtin" in args:
        phase_exec_builtin()
    if not args or "general" in args:
        phase_exec_general()
    if not args or "fail" in args:
        phase_exec_failures()
    if not args or "pg" in args:
        phase_exec_postgres()
    if not args or "mongo" in args:
        phase_exec_mongodb()
    if not args or "podman" in args:
        phase_exec_podman()
    if not args or "docker" in args:
        phase_exec_docker()
