"""End-to-end integration tests against real infrastructure.

These tests spin up a real docker container and run real Ansible modules
against it via SSH. They are **opt-in** via ``pytest -m integration``; the
default ``pytest`` run skips them.

Prereqs (any missing → auto-skip):
  - docker daemon reachable (Colima socket or /var/run/docker.sock)
  - ``ansible-doc`` on PATH (for schema-fidelity tests)

Each test owns its disposable artifacts and cleans up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastmcp.client import Client

from rocannon.config import Config
from rocannon.server import create_server

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# environment probes (cheap; tests use them to auto-skip)
# ---------------------------------------------------------------------------


def _docker_alive() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


_skip_no_docker = pytest.mark.skipif(
    not _docker_alive(),
    reason="docker daemon not reachable",
)


# ---------------------------------------------------------------------------
# shared session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ssh_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate an ed25519 key once per session for the UBI9 SSH target."""
    keydir = tmp_path_factory.mktemp("ssh")
    key = keydir / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "rocannon-integ"],
        capture_output=True,
        check=True,
    )
    return key


@pytest.fixture(scope="session")
def ubi_container(ssh_key: Path) -> Generator[tuple[str, int], None, None]:
    """Build + run a UBI9 SSH container; yield (host, port). Cleaned at session end."""
    if not _docker_alive():
        pytest.skip("docker not reachable")

    name = f"rocannon-integ-ubi-{uuid.uuid4().hex[:8]}"
    image = f"{name}:latest"
    port = 22000 + (hash(name) % 1000)

    build_ctx = ssh_key.parent
    dockerfile = build_ctx / "Dockerfile"
    dockerfile.write_text(
        "FROM redhat/ubi9-minimal\n"
        "RUN microdnf install -y openssh-server openssh-clients python3 "
        "iproute procps-ng iputils && microdnf clean all && ssh-keygen -A "
        "&& mkdir -p /root/.ssh && chmod 700 /root/.ssh\n"
        f"COPY {ssh_key.name}.pub /root/.ssh/authorized_keys\n"
        "RUN chmod 600 /root/.ssh/authorized_keys && "
        "sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' "
        "/etc/ssh/sshd_config\n"
        'EXPOSE 22\nCMD ["/usr/sbin/sshd", "-D", "-e"]\n'
    )
    subprocess.run(
        ["docker", "build", "-t", image, str(build_ctx)],
        capture_output=True,
        check=True,
        timeout=180,
    )
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:22", image],
        capture_output=True,
        check=True,
        timeout=30,
    )

    # Wait for sshd
    for _ in range(20):
        check = subprocess.run(
            [
                "ssh",
                "-i",
                str(ssh_key),
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-p",
                str(port),
                "-o",
                "ConnectTimeout=2",
                "root@127.0.0.1",
                "echo ok",
            ],
            capture_output=True,
            timeout=5,
        )
        if check.returncode == 0:
            break
        time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(f"ssh did not come up on {name}")

    try:
        yield ("127.0.0.1", port)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def ansible_inventory(tmp_path: Path, ssh_key: Path, ubi_container: tuple[str, int]) -> Path:
    """Per-test inventory pointing at the session-shared UBI container."""
    host, port = ubi_container
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[ubi]\n"
        f"ubi9 ansible_host={host} ansible_port={port} ansible_user=root "
        f"ansible_ssh_private_key_file={ssh_key} "
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null'\n"
    )
    return inv


# ---------------------------------------------------------------------------
# Ansible module execution
# ---------------------------------------------------------------------------


class TestAnsibleExecution:
    @_skip_no_docker
    async def test_ping_real_ubi9_target(self, ansible_inventory: Path) -> None:
        cfg = Config(
            inventories=[ansible_inventory],
            modules=["ansible.builtin.ping"],
        )
        server = create_server(cfg)
        async with Client(server) as c:
            r = await c.call_tool("ansible.builtin.ping", {"target": "ubi9"})
            payload = json.loads(r.content[0].text)
            assert payload["status"] == "successful"
            assert payload["result"]["ping"] == "pong"

    @_skip_no_docker
    async def test_command_redacts_passwords(self, ansible_inventory: Path) -> None:
        cfg = Config(
            inventories=[ansible_inventory],
            modules=["ansible.builtin.command"],
        )
        server = create_server(cfg)
        async with Client(server) as c:
            # Echo a fake secret; verify the result has been scrubbed.
            r = await c.call_tool(
                "ansible.builtin.command",
                {
                    "target": "ubi9",
                    "cmd": "echo password=hunter2 trailing",
                },
            )
            text = r.content[0].text
            assert "hunter2" not in text, "secret leaked into tool result"
            assert "REDACTED" in text or "password=" not in text


# ---------------------------------------------------------------------------
# Schema fidelity, what we register matches the upstream catalog
# ---------------------------------------------------------------------------


class TestSchemaFidelity:
    """Catches drift between our registered tools and ansible-doc.

    Marked integration because they shell out to ansible-doc, but they don't
    touch docker or any real targets.
    """

    @pytest.mark.skipif(not _have("ansible-doc"), reason="ansible-doc not on PATH")
    async def test_ansible_ping_schema_matches_ansible_doc(self) -> None:
        """ansible.builtin.ping → tool inputSchema should reflect ansible-doc truth."""
        cfg = Config(
            inventories=[self._dummy_inventory()],
            modules=["ansible.builtin.ping"],
        )
        server = create_server(cfg)
        async with Client(server) as c:
            tool = next(t for t in await c.list_tools() if t.name == "ansible.builtin.ping")
            props = tool.inputSchema.get("properties", {})
            required = set(tool.inputSchema.get("required", []))
            # ansible.builtin.ping has one optional param: data
            assert "data" in props
            assert "data" not in required
            assert "target" in props
            assert "target" in required

    @staticmethod
    def _dummy_inventory() -> Path:
        """Minimal in-memory inventory for cases where we just need schema, not execution."""
        f = Path(tempfile.mkstemp(suffix=".ini")[1])
        f.write_text("[local]\nlocalhost ansible_connection=local\n")
        return f
