"""Integration tests for community Ansible collection tools.

Each test class covers one collection. Tests verify both that tools are
registered with correct schemas AND that they execute successfully against
a live Ubuntu container.

All tests in this file require the `ubuntu_container` fixture, which builds
and starts the test container. The suite skips gracefully when no container
runtime is available.

Usage:
    uv run pytest tests/test_collections.py -v              # all collection tests
    uv run pytest tests/test_collections.py -v -k crypto    # one collection
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from rocannon.config import Config
from rocannon.server import create_server

# These exercise real third-party collections against a live container, so they
# are integration tests (opt-in via `pytest -m integration`), like
# test_ansible_integration.py. Without the marker they error in any environment
# that lacks the collections (e.g. CI, which installs ansible-core only).
pytestmark = pytest.mark.integration


def _server(inventory: Path, modules: list[str]) -> Any:
    return create_server(Config(inventories=[inventory], modules=modules))


def _tool_names(tools: list[Any]) -> set[str]:
    return {t.name for t in tools}


def _result(raw: Any) -> dict[str, Any]:
    content = raw.content[0].text if hasattr(raw.content[0], "text") else str(raw.content[0])
    return json.loads(content)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# ansible.posix
# ---------------------------------------------------------------------------


class TestAnsiblePosix:
    @pytest.fixture(scope="class")
    @staticmethod
    def server(ubuntu_container: Path) -> Any:
        return _server(
            ubuntu_container,
            [
                "ansible.posix.authorized_key",
                "ansible.posix.sysctl",
                "ansible.posix.acl",
                "ansible.posix.synchronize",
            ],
        )

    @pytest.mark.asyncio
    async def test_tools_registered(self, server: Any) -> None:
        async with Client(server) as client:
            names = _tool_names(await client.list_tools())
        assert "ansible.posix.authorized_key" in names
        assert "ansible.posix.sysctl" in names
        assert "ansible.posix.acl" in names

    @pytest.mark.asyncio
    async def test_sysctl_read(self, server: Any) -> None:
        async with Client(server) as client:
            raw = await client.call_tool(
                "ansible.posix.sysctl",
                {"target": "ubuntu", "name": "vm.swappiness", "value": "60", "state": "present"},
            )
        result = _result(raw)
        assert result["status"] == "successful"

    @pytest.mark.asyncio
    async def test_acl_query(self, server: Any) -> None:
        async with Client(server) as client:
            raw = await client.call_tool(
                "ansible.posix.acl",
                {"target": "ubuntu", "path": "/tmp", "state": "query"},
            )
        result = _result(raw)
        assert result["status"] == "successful"


# ---------------------------------------------------------------------------
# community.general (selected modules)
# ---------------------------------------------------------------------------


class TestCommunityGeneral:
    @pytest.fixture(scope="class")
    @staticmethod
    def server(ubuntu_container: Path) -> Any:
        return _server(
            ubuntu_container,
            [
                "community.general.ufw",
                "community.general.listen_ports_facts",
                "community.general.ini_file",
            ],
        )

    @pytest.mark.asyncio
    async def test_tools_registered(self, server: Any) -> None:
        async with Client(server) as client:
            names = _tool_names(await client.list_tools())
        assert "community.general.ufw" in names
        assert "community.general.listen_ports_facts" in names

    @pytest.mark.asyncio
    async def test_ini_file_idempotent(self, server: Any) -> None:
        async with Client(server) as client:
            # Write once, write again, second call should not change.
            await client.call_tool(
                "community.general.ini_file",
                {
                    "target": "ubuntu",
                    "path": "/tmp/idempotent.ini",
                    "section": "s",
                    "option": "k",
                    "value": "v",
                },
            )
            raw = await client.call_tool(
                "community.general.ini_file",
                {
                    "target": "ubuntu",
                    "path": "/tmp/idempotent.ini",
                    "section": "s",
                    "option": "k",
                    "value": "v",
                },
            )
        result = _result(raw)
        assert result["status"] == "successful"
        assert result["changed"] is False

    @pytest.mark.asyncio
    async def test_listen_ports_facts(self, server: Any) -> None:
        async with Client(server) as client:
            raw = await client.call_tool(
                "community.general.listen_ports_facts",
                {"target": "ubuntu"},
            )
        result = _result(raw)
        assert result["status"] == "successful"

    @pytest.mark.asyncio
    async def test_ini_file_write(self, server: Any) -> None:
        async with Client(server) as client:
            raw = await client.call_tool(
                "community.general.ini_file",
                {
                    "target": "ubuntu",
                    "path": "/tmp/rocannon-test.ini",
                    "section": "test",
                    "option": "key",
                    "value": "rocannon",
                },
            )
        result = _result(raw)
        assert result["status"] == "successful"


# ---------------------------------------------------------------------------
# community.crypto
# ---------------------------------------------------------------------------


class TestCommunityCrypto:
    @pytest.fixture(scope="class")
    @staticmethod
    def server(ubuntu_container: Path) -> Any:
        return _server(
            ubuntu_container,
            [
                "community.crypto.openssl_privatekey",
                "community.crypto.openssl_csr",
                "community.crypto.x509_certificate",
                "community.crypto.openssh_keypair",
            ],
        )

    @pytest.mark.asyncio
    async def test_tools_registered(self, server: Any) -> None:
        async with Client(server) as client:
            names = _tool_names(await client.list_tools())
        assert "community.crypto.openssl_privatekey" in names
        assert "community.crypto.openssh_keypair" in names
        assert "community.crypto.x509_certificate" in names

    @pytest.mark.asyncio
    async def test_openssh_keypair_generate(self, server: Any) -> None:
        async with Client(server) as client:
            raw = await client.call_tool(
                "community.crypto.openssh_keypair",
                {"target": "ubuntu", "path": "/tmp/rocannon_test_ed25519", "param_type": "ed25519"},
            )
        result = _result(raw)
        assert result["status"] == "successful"

    @pytest.mark.asyncio
    async def test_openssl_privatekey_generate(self, server: Any) -> None:
        async with Client(server) as client:
            raw = await client.call_tool(
                "community.crypto.openssl_privatekey",
                {"target": "ubuntu", "path": "/tmp/rocannon_test.key", "size": 2048},
            )
        result = _result(raw)
        assert result["status"] == "successful"

    @pytest.mark.asyncio
    async def test_x509_selfsigned(self, server: Any) -> None:
        """Generate a self-signed cert from the key created in the previous test."""
        async with Client(server) as client:
            # CSR first
            await client.call_tool(
                "community.crypto.openssl_csr",
                {
                    "target": "ubuntu",
                    "path": "/tmp/rocannon_test.csr",
                    "privatekey_path": "/tmp/rocannon_test.key",
                    "common_name": "rocannon.test",
                },
            )
            raw = await client.call_tool(
                "community.crypto.x509_certificate",
                {
                    "target": "ubuntu",
                    "path": "/tmp/rocannon_test.crt",
                    "privatekey_path": "/tmp/rocannon_test.key",
                    "csr_path": "/tmp/rocannon_test.csr",
                    "provider": "selfsigned",
                },
            )
        result = _result(raw)
        assert result["status"] == "successful"
