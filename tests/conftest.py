"""Pytest fixtures for Rocannon integration tests.

Manages test containers (RHEL 10, SLES 16, Ubuntu 24.04) with SSH + Python,
matching the enterprise Linux distros supported on IBM LinuxONE (s390x).

Container lifecycle:
  session start → build images → start containers → generate inventory
  session end   → stop and remove containers
"""

import shutil
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import yaml

CONTAINERS_DIR = Path(__file__).parent / "containers"

CONTAINER_DEFS = {
    "rocannon-rhel": ("Containerfile.rhel", "rocannon-test:rhel", 2222),
    "rocannon-sles": ("Containerfile.sles", "rocannon-test:sles", 2223),
    "rocannon-ubuntu": ("Containerfile.ubuntu", "rocannon-test:ubuntu", 2224),
}

SSH_USER = "rocannon"
SSH_PASSWORD = "rocannon"


def _runtime() -> str:
    """Detect container runtime: podman preferred, fallback to docker."""
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    raise RuntimeError("Neither podman nor docker found on PATH")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a container runtime command."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _container_running(runtime: str, name: str) -> bool:
    """Check if a container with the given name is running."""
    result = _run(
        [runtime, "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
        check=False,
    )
    return name in result.stdout


def _wait_for_ssh(port: int, timeout: int = 30) -> None:
    """Wait until SSH is accepting connections on the given port."""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    raise TimeoutError(f"SSH not ready on port {port} after {timeout}s")


def _build_image(runtime: str, containerfile: str, tag: str) -> None:
    """Build a container image if it doesn't already exist."""
    result = _run([runtime, "image", "exists", tag], check=False)
    if result.returncode == 0:
        return

    _run(
        [
            runtime,
            "build",
            "-f",
            str(CONTAINERS_DIR / containerfile),
            "-t",
            tag,
            str(CONTAINERS_DIR),
        ]
    )


def _start_container(runtime: str, name: str, tag: str, port: int) -> None:
    """Start a container, removing any existing one with the same name."""
    _run([runtime, "rm", "-f", name], check=False)

    _run(
        [
            runtime,
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"{port}:22",
            tag,
        ],
    )

    _wait_for_ssh(port)


def _stop_container(runtime: str, name: str) -> None:
    """Stop and remove a container."""
    _run([runtime, "rm", "-f", name], check=False)


def _generate_inventory(inv_path: Path) -> None:
    """Generate Ansible inventory YAML for the running test containers."""
    hosts = {}
    for name, (_, _, port) in CONTAINER_DEFS.items():
        inv_name = name.replace("rocannon-", "linuxone-")
        hosts[inv_name] = {
            "ansible_host": "127.0.0.1",
            "ansible_port": port,
            "ansible_user": SSH_USER,
            "ansible_password": SSH_PASSWORD,
            "ansible_become": True,
            "ansible_become_password": SSH_PASSWORD,
            "ansible_ssh_extra_args": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        }

    inventory = {
        "all": {
            "hosts": hosts,
            "children": {
                "linuxone": {
                    "hosts": dict.fromkeys(hosts),
                },
            },
        },
    }

    inv_path.parent.mkdir(parents=True, exist_ok=True)
    inv_path.write_text(yaml.dump(inventory, default_flow_style=False))


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def container_runtime() -> str:
    """Detect and return the container runtime (podman or docker)."""
    return _runtime()


@pytest.fixture(scope="session")
def podman_containers(container_runtime: str) -> Generator[dict[str, Any], None, None]:
    """Build, start, and yield test containers. Tear down at session end.

    Skips all tests if no container runtime is available.
    """
    runtime = container_runtime

    for _name, (containerfile, tag, _) in CONTAINER_DEFS.items():
        _build_image(runtime, containerfile, tag)

    for name, (_, tag, port) in CONTAINER_DEFS.items():
        _start_container(runtime, name, tag, port)

    yield CONTAINER_DEFS

    for name in CONTAINER_DEFS:
        _stop_container(runtime, name)


@pytest.fixture(scope="session")
def podman_inventory(
    podman_containers: dict[str, Any], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Generate and return path to a dynamic Ansible inventory for test containers."""
    _ = podman_containers  # ensure containers are running before generating inventory
    inv_dir = tmp_path_factory.mktemp("rocannon-inv")
    inv_path = inv_dir / "podman.yml"
    _generate_inventory(inv_path)
    return inv_path
