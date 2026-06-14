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

COMMAND_SCHEMA: dict[str, Any] = {
    "name": "ansible.builtin.command",
    "description": "Run a command",
    "parameters": [
        {"name": "cmd", "description": "the command", "type": "str", "required": False},
    ],
    "attributes": {"check_mode": "partial", "diff_mode": "none", "facts": False, "raw": True},
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

    schemas = {
        "ansible.builtin.ping": PING_SCHEMA,
        "ansible.builtin.copy": COPY_SCHEMA,
        "ansible.builtin.command": COMMAND_SCHEMA,
    }

    def _fetch(name: str) -> dict[str, Any]:
        return schemas[name]

    with patch("rocannon.ansible.fetch_module_schema", side_effect=_fetch):
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

        with patch("rocannon.ansible.fetch_module_schema", side_effect=_fetch):
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
        with patch("rocannon.ansible.run_module", return_value=_ok_result()) as mock_run:
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
        with patch("rocannon.ansible.run_module", return_value=_ok_result()) as mock_run:
            async with Client(server) as client:
                await client.call_tool("ansible.builtin.ping", {"target": "h1"})
        # `data` had no explicit value and defaulted to None, should be excluded
        assert mock_run.call_args[1]["module_args"] == {}

    async def test_per_module_timeout_passed_through(self, inventory_file: Path) -> None:
        from rocannon.server import create_server

        with patch("rocannon.ansible.fetch_module_schema", return_value=PING_SCHEMA):
            cfg = Config(
                inventories=[inventory_file],
                modules=["ansible.builtin.ping"],
                timeouts={"ansible.builtin.ping": 999},
            )
            server = create_server(cfg)

        with patch("rocannon.ansible.run_module", return_value=_ok_result()) as mock_run:
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

        with patch("rocannon.ansible.run_module", return_value=_ok_result()):
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


class TestStructuredOutput:
    async def test_tool_declares_output_schema(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["ansible.builtin.ping"].outputSchema is not None

    async def test_result_is_structured_content(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        with patch("rocannon.ansible.run_module", return_value=_ok_result()):
            async with Client(server) as client:
                result = await client.call_tool("ansible.builtin.ping", {"target": "h1"})
        assert result.structured_content == _ok_result()
        # The text block stays valid JSON so existing string-parsing clients work.
        assert json.loads(result.content[0].text)["status"] == "successful"


class TestToolAnnotations:
    async def test_read_only_module_annotated(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        assert tools["ansible.builtin.ping"].annotations.readOnlyHint is True

    async def test_raw_family_module_flagged_destructive(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.command"])
        async with Client(server) as client:
            tools = {t.name: t for t in await client.list_tools()}
        annotations = tools["ansible.builtin.command"].annotations
        assert annotations.destructiveHint is True
        assert annotations.openWorldHint is True


class TestDryRunPassThrough:
    async def test_check_flag_reaches_executor_not_module_args(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.command"])
        with patch("rocannon.ansible.run_module", return_value=_ok_result()) as mock_run:
            async with Client(server) as client:
                await client.call_tool(
                    "ansible.builtin.command",
                    {"target": "h1", "cmd": "id", "check": True},
                )
        kwargs = mock_run.call_args[1]
        assert kwargs["check"] is True
        assert "check" not in kwargs["module_args"]
        assert kwargs["module_args"] == {"cmd": "id"}


class TestLivePlaybookPrompts:
    async def test_save_playbook_registers_prompt_without_restart(
        self, inventory_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROCANNON_DATA_DIR", str(tmp_path))
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        async with Client(server) as client:
            assert "playbook_sess1" not in {p.name for p in await client.list_prompts()}
            res = await client.call_tool(
                "save_playbook",
                {
                    "name": "sess1",
                    "description": "demo",
                    "steps": [{"tool": "ansible.builtin.ping", "args": {"target": "h1"}}],
                },
            )
            payload = res.structured_content
            assert payload["ok"] is True
            assert payload["prompt"] == "playbook_sess1"
            assert "restart" not in payload["note"].lower()
            assert "playbook_sess1" in {p.name for p in await client.list_prompts()}

    async def test_commit_session_registers_prompt(
        self, inventory_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROCANNON_DATA_DIR", str(tmp_path))
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        with patch("rocannon.ansible.run_module", return_value=_ok_result()):
            async with Client(server) as client:
                await client.call_tool("ansible.builtin.ping", {"target": "h1"})
                res = await client.call_tool("commit_session", {"name": "sess2"})
                assert res.structured_content["ok"] is True
                assert "playbook_sess2" in {p.name for p in await client.list_prompts()}


class TestDiscoveryResources:
    async def test_profiles_resource(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        async with Client(server) as client:
            contents = await client.read_resource("rocannon://profiles")
        data = json.loads(contents[0].text)
        assert data["active"] == "default"
        assert any("ansible.builtin.ping" in p["modules"] for p in data["profiles"])

    async def test_collections_resource(self, inventory_file: Path) -> None:
        server = _build_server(inventory_file, ["ansible.builtin.ping", "ansible.builtin.copy"])
        async with Client(server) as client:
            contents = await client.read_resource("rocannon://collections")
        data = json.loads(contents[0].text)
        builtin = next(c for c in data["collections"] if c["name"] == "ansible.builtin")
        assert "ansible.builtin.ping" in builtin["modules"]
        assert builtin["module_count"] == 2

    async def test_playbooks_resource_lists_saved(
        self, inventory_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROCANNON_DATA_DIR", str(tmp_path))
        server = _build_server(inventory_file, ["ansible.builtin.ping"])
        async with Client(server) as client:
            await client.call_tool(
                "save_playbook",
                {
                    "name": "rb1",
                    "description": "d",
                    "steps": [{"tool": "ansible.builtin.ping", "args": {"target": "h1"}}],
                },
            )
            contents = await client.read_resource("rocannon://playbooks")
        data = json.loads(contents[0].text)
        assert any(pb["name"] == "rb1" for pb in data)
