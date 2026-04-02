"""Pytest fixtures for Rocannon integration tests.

Manages a single Ubuntu 24.04 test container with SSH + Python.
The container is built on first run, started at session start, and removed at
session end. Tests that require it declare the `ubuntu_container` fixture;
tests that don't (e.g. test_unit.py) are unaffected.
"""

import shutil
import socket
import subprocess
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml

CONTAINER_NAME = "rocannon-test"
CONTAINER_TAG = "rocannon-test:ubuntu"
CONTAINER_PORT = 2222
CONTAINERS_DIR = Path(__file__).parent / "containers"

SSH_USER = "rocannon"
SSH_PASSWORD = "rocannon"


def _runtime() -> str:
    if shutil.which("podman"):
        return "podman"
    if shutil.which("docker"):
        return "docker"
    raise RuntimeError("Neither podman nor docker found on PATH")


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _wait_for_ssh(port: int, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.5)
    raise TimeoutError(f"SSH not ready on port {port} after {timeout}s")


def _build_image(runtime: str) -> None:
    result = _run([runtime, "image", "exists", CONTAINER_TAG], check=False)
    if result.returncode == 0:
        return
    _run(
        [
            runtime,
            "build",
            "-f",
            str(CONTAINERS_DIR / "Containerfile"),
            "-t",
            CONTAINER_TAG,
            str(CONTAINERS_DIR),
        ]
    )


def _start_container(runtime: str) -> None:
    _run([runtime, "rm", "-f", CONTAINER_NAME], check=False)
    _run(
        [
            runtime,
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "-p",
            f"{CONTAINER_PORT}:22",
            CONTAINER_TAG,
        ]
    )
    _wait_for_ssh(CONTAINER_PORT)


def _stop_container(runtime: str) -> None:
    _run([runtime, "rm", "-f", CONTAINER_NAME], check=False)


def _write_inventory(path: Path) -> None:
    inventory = {
        "all": {
            "hosts": {
                "ubuntu": {
                    "ansible_host": "127.0.0.1",
                    "ansible_port": CONTAINER_PORT,
                    "ansible_user": SSH_USER,
                    "ansible_password": SSH_PASSWORD,
                    "ansible_become": True,
                    "ansible_become_password": SSH_PASSWORD,
                    "ansible_ssh_extra_args": (
                        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
                    ),
                }
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(inventory, default_flow_style=False))


@pytest.fixture(scope="session")
def ubuntu_container(tmp_path_factory: pytest.TempPathFactory) -> Generator[Path, None, None]:
    """Build, start, and yield path to the Ubuntu test container inventory.

    Skips all dependent tests if no container runtime is available or
    if the build/start fails (e.g. podman machine not running).
    """
    try:
        runtime = _runtime()
    except RuntimeError as exc:
        pytest.skip(str(exc))

    try:
        _build_image(runtime)
        _start_container(runtime)
    except (subprocess.CalledProcessError, TimeoutError) as exc:
        pytest.skip(f"Container runtime not usable: {exc}")

    inv_path = tmp_path_factory.mktemp("rocannon-inv") / "inventory.yml"
    _write_inventory(inv_path)

    yield inv_path

    _stop_container(runtime)
