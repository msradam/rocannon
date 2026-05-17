"""End-to-end tests for the FastMCP server using an in-memory Client.

These exercise tool registration, middleware composition, and tool invocation
without touching ansible-doc or ansible-runner. ``fetch_module_schema`` and
``run_module`` are mocked at the module boundary.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp.client import Client

from rocannon.config import Config

PING_SCHEMA: dict[str, Any] = {
    "name": "ansible.builtin.ping",
    "description": "Try to connect to host",
    "parameters": [
        {"name": "data", "description": "ping data", "type": "str", "required": False},
    ],
}

COPY_SCHEMA: dict[str, Any] = {
    "name": "ansible.builtin.copy",
    "description": "Copy files",
    "parameters": [
        {"name": "src", "description": "source", "type": "str", "required": True},
        {"name": "dest", "description": "dest", "type": "str", "required": True},
    ],
}


def _ok_result(host: str = "h1") -> dict[str, Any]:
    return {
        "status": "successful",
        "changed": False,
        "result": {"ping": "pong"},
        "stdout": "",
        "stderr": "",
    }


@pytest.fixture
def inventory_file(tmp_path: Path) -> Path:
    inv = tmp_path / "hosts"
    inv.write_text("[testgroup]\nh1 ansible_host=1.1.1.1\nh2 ansible_host=2.2.2.2\n")
    return inv


def _build_server(inv: Path, modules: list[str]) -> Any:
    """Construct a FastMCP server with schema and runner mocked.

    Patches stay scoped to the caller via the context manager protocol, the
    test enters the patch, builds the server, and exits with the server still
    usable because schemas are captured into closures at registration time.
    """
    from rocannon.server import create_server

    schemas = {"ansible.builtin.ping": PING_SCHEMA, "ansible.builtin.copy": COPY_SCHEMA}

    def _fetch(name: str) -> dict[str, Any]:
        return schemas[name]

    with patch("rocannon.cannons.ansible.fetch_module_schema", side_effect=_fetch):
        cfg = Config(inventories=[inv], modules=modules)
        return create_server(cfg)


class TestServerToolRegistration:
    async def test_lists_registered_tools(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        async with Client(server) as client:
            tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "ansible.builtin.ping" in names

    async def test_multiple_modules_register(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping", "ansible.builtin.copy"])
        async with Client(server) as client:
            tools = await client.list_tools()
        names = {t.name for t in tools}
        assert {"ansible.builtin.ping", "ansible.builtin.copy"} <= names

    async def test_skips_modules_whose_schema_fails(self, inventory_file: Path) -> None:
        from rocannon.schema import SchemaFetchError
        from rocannon.server import create_server

        def _fetch(name: str) -> dict[str, Any]:
            if name == "broken.module.x":
                raise SchemaFetchError("synthetic failure")
            return PING_SCHEMA

        with patch("rocannon.cannons.ansible.fetch_module_schema", side_effect=_fetch):
            cfg = Config(
                inventories=[inventory_file],
                modules=["ansible.builtin.ping", "broken.module.x"],
            )
            server = create_server(cfg)
        async with Client(server) as client:
            tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "ansible.builtin.ping" in names
        assert "broken.module.x" not in names


class TestServerToolInvocation:
    async def test_calls_run_module_with_target_and_args(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        with patch("rocannon.server.run_module", return_value=_ok_result()) as mock_run:
            async with Client(server) as client:
                result = await client.call_tool(
                    "ansible.builtin.ping", {"target": "h1", "data": "hello"}
                )
        assert mock_run.called
        kwargs = mock_run.call_args[1]
        assert kwargs["module"] == "ansible.builtin.ping"
        assert kwargs["host_pattern"] == "h1"
        assert kwargs["module_args"] == {"data": "hello"}
        # FastMCP wraps the tool return in result content; payload is JSON text.
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "successful"

    async def test_omits_none_args(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        with patch("rocannon.server.run_module", return_value=_ok_result()) as mock_run:
            async with Client(server) as client:
                await client.call_tool("ansible.builtin.ping", {"target": "h1"})
        # `data` had no explicit value and defaulted to None, should be excluded
        assert mock_run.call_args[1]["module_args"] == {}

    async def test_per_module_timeout_passed_through(self, inventory_file: Path) -> None:
        from rocannon.server import create_server

        with patch("rocannon.cannons.ansible.fetch_module_schema", return_value=PING_SCHEMA):
            cfg = Config(
                inventories=[inventory_file],
                modules=["ansible.builtin.ping"],
                timeouts={"ansible.builtin.ping": 999},
            )
            server = create_server(cfg)

        with patch("rocannon.server.run_module", return_value=_ok_result()) as mock_run:
            async with Client(server) as client:
                await client.call_tool("ansible.builtin.ping", {"target": "h1"})
        assert mock_run.call_args[1]["timeout"] == 999


class TestAuditMiddleware:
    async def test_audit_record_includes_request_id_and_target(
        self, inventory_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        caplog.set_level(logging.INFO, logger="rocannon.audit")

        with patch("rocannon.server.run_module", return_value=_ok_result()):
            async with Client(server) as client:
                await client.call_tool("ansible.builtin.ping", {"target": "h1"})

        audit_records = [r for r in caplog.records if r.name == "rocannon.audit"]
        assert audit_records, "expected at least one rocannon.audit record"
        payload = json.loads(audit_records[-1].message)
        assert payload["tool"] == "ansible.builtin.ping"
        assert payload["target"] == "h1"
        assert payload["status"] == "successful"
        assert len(payload["request_id"]) == 8
        assert isinstance(payload["latency_ms"], int)
