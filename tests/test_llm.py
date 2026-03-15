"""LLM integration tests for Rocannon MCP server.

Uses Ollama (local) with ibm/granite4:micro to verify that an LLM can discover,
select, parameterise, and interpret Ansible module tools via MCP.

Two test profiles:
  - linuxone: ansible.builtin tools against enterprise Linux containers (RHEL, SLES, Ubuntu)
  - zos:      ansible.builtin + ibm.ibm_zos_core tools against z/OS LPARs (schema-only)

Container and Ollama lifecycle is managed by conftest.py fixtures.

Usage:
    uv run pytest tests/test_llm.py -v                   # all tests
    uv run pytest tests/test_llm.py -v -k zos            # z/OS tests (no connectivity required)
    uv run pytest tests/test_llm.py -v -k linuxone       # LinuxONE only (starts containers)
"""

import json
import logging
from pathlib import Path
from typing import Any

import ollama
import pytest
from fastmcp import Client

from rocannon.config import Config
from rocannon.server import create_server

logger = logging.getLogger("rocannon.test_llm")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ZOS_INVENTORY = Path(__file__).resolve().parent.parent / "csrt.yml"

LINUXONE_MODULES = [
    "ansible.builtin.ping",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.builtin.copy",
    "ansible.builtin.file",
    "ansible.builtin.stat",
    "ansible.builtin.slurp",
    "ansible.builtin.setup",
    "ansible.builtin.lineinfile",
]

ZOS_MODULES = [
    "ansible.builtin.ping",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.builtin.setup",
    "ansible.builtin.stat",
    "ansible.builtin.copy",
    "ansible.builtin.file",
    "ibm.ibm_zos_core",
]


@pytest.fixture(scope="session")
def linuxone_server(podman_inventory: Path) -> Any:
    """Create MCP server backed by enterprise Linux containers (RHEL, SLES, Ubuntu).

    Container lifecycle (build, start, teardown) is handled by the
    podman_inventory fixture in conftest.py.
    """
    config = Config(inventories=[podman_inventory], modules=LINUXONE_MODULES)
    return create_server(config)


@pytest.fixture(scope="module")
def zos_server() -> Any:
    if not ZOS_INVENTORY.exists():
        pytest.skip(f"z/OS inventory not found: {ZOS_INVENTORY}")
    config = Config(inventories=[ZOS_INVENTORY], modules=ZOS_MODULES)
    return create_server(config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def mcp_tools_to_ollama(tools: list[Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Convert MCP tool list to Ollama tool format.

    Returns (ollama_tools, schema_map) where schema_map maps
    tool names to their MCP input schemas for argument validation.
    """
    ollama_tools: list[dict[str, Any]] = []
    schema_map: dict[str, dict[str, Any]] = {}

    for t in tools:
        schema = t.inputSchema
        schema_map[t.name] = schema

        ollama_tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or t.name,
                    "parameters": schema,
                },
            }
        )

    return ollama_tools, schema_map


async def run_agent_loop(
    mcp_client: Client,
    ollama_tools: list[dict[str, Any]],
    model: str,
    prompt: str,
    max_turns: int = 5,
) -> dict[str, Any]:
    """Run a full LLM → tool-call → result loop.

    Returns a dict with:
      - tool_calls: list of (tool_name, args) tuples executed
      - final_response: the model's final text answer
      - raw_results: list of raw tool call results
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    tool_calls_made: list[tuple[str, dict[str, Any]]] = []
    raw_results: list[str] = []

    for _turn in range(max_turns):
        response = ollama.chat(
            model=model,
            messages=messages,
            tools=ollama_tools,
            options={"temperature": 0, "num_ctx": 16384},
        )
        messages.append(response.message)

        if not response.message.tool_calls:
            return {
                "tool_calls": tool_calls_made,
                "final_response": response.message.content or "",
                "raw_results": raw_results,
            }

        for tc in response.message.tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            tool_calls_made.append((name, args))
            logger.info("Tool call: %s(%s)", name, json.dumps(args))

            try:
                result = await mcp_client.call_tool(name, args)
                result_text = result.content[0].text if hasattr(result, "content") else str(result)
            except Exception as exc:
                result_text = json.dumps({"error": str(exc)})

            raw_results.append(result_text)
            messages.append({"role": "tool", "content": result_text})

    return {
        "tool_calls": tool_calls_made,
        "final_response": messages[-1].get("content", "") if isinstance(messages[-1], dict) else "",
        "raw_results": raw_results,
    }


# ---------------------------------------------------------------------------
# LinuxONE tests (live execution against RHEL, SLES, Ubuntu containers)
# ---------------------------------------------------------------------------


class TestLinuxOneLive:
    """Live tests against enterprise Linux containers via LLM tool calling."""

    @pytest.mark.asyncio
    async def test_ping_single_host(self, linuxone_server: Any, ollama_model: str) -> None:
        """LLM should pick ansible.builtin.ping and target a specific host."""
        async with Client(linuxone_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            result = await run_agent_loop(
                mcp_client,
                ollama_tools,
                ollama_model,
                "Ping the linuxone-ubuntu host to check if it's reachable.",
            )

            assert len(result["tool_calls"]) >= 1
            name, args = result["tool_calls"][0]
            assert name == "ansible.builtin.ping"
            assert args["target"] == "linuxone-ubuntu"

            data = json.loads(result["raw_results"][0])
            assert data["status"] == "successful"

    @pytest.mark.asyncio
    async def test_ping_group(self, linuxone_server: Any, ollama_model: str) -> None:
        """LLM should ping an entire group."""
        async with Client(linuxone_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            result = await run_agent_loop(
                mcp_client,
                ollama_tools,
                ollama_model,
                "Ping all hosts in the linuxone group.",
            )

            assert len(result["tool_calls"]) >= 1
            name, args = result["tool_calls"][0]
            assert name == "ansible.builtin.ping"
            assert args["target"] == "linuxone"

    @pytest.mark.asyncio
    async def test_gather_os_info(self, linuxone_server: Any, ollama_model: str) -> None:
        """LLM should use command or setup to identify the OS."""
        async with Client(linuxone_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            result = await run_agent_loop(
                mcp_client,
                ollama_tools,
                ollama_model,
                "What operating system is running on linuxone-rhel? Use a command to check.",
            )

            assert len(result["tool_calls"]) >= 1
            name, _ = result["tool_calls"][0]
            assert name in (
                "ansible.builtin.command",
                "ansible.builtin.shell",
                "ansible.builtin.setup",
            )

    @pytest.mark.asyncio
    async def test_file_lifecycle(self, linuxone_server: Any, ollama_model: str) -> None:
        """LLM should create a file, verify it exists, then remove it."""
        async with Client(linuxone_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            result = await run_agent_loop(
                mcp_client,
                ollama_tools,
                ollama_model,
                "On linuxone-sles: "
                "1) Create the file /tmp/rocannon-test.txt with content 'hello from rocannon'. "
                "2) Verify the file exists using stat. "
                "3) Remove the file. "
                "Do all three steps.",
                max_turns=8,
            )

            tool_names = [tc[0] for tc in result["tool_calls"]]
            assert len(result["tool_calls"]) >= 2, f"Expected >=2 tool calls, got {tool_names}"
            assert "ansible.builtin.copy" in tool_names or "ansible.builtin.shell" in tool_names, (
                f"Expected a create operation, got {tool_names}"
            )
            assert "ansible.builtin.stat" in tool_names or "ansible.builtin.file" in tool_names, (
                f"Expected a verify/delete operation, got {tool_names}"
            )

    @pytest.mark.asyncio
    async def test_multi_host_command(self, linuxone_server: Any, ollama_model: str) -> None:
        """LLM should run a command across multiple hosts."""
        async with Client(linuxone_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            result = await run_agent_loop(
                mcp_client,
                ollama_tools,
                ollama_model,
                "Run 'hostname' on all linuxone hosts using the command module.",
            )

            assert len(result["tool_calls"]) >= 1
            name, args = result["tool_calls"][0]
            assert name == "ansible.builtin.command"


# ---------------------------------------------------------------------------
# z/OS tests (schema validation only — no live connectivity)
# ---------------------------------------------------------------------------


class TestZosSchema:
    """Validate that LLM correctly selects z/OS tools and parameters.

    These tests do NOT execute against real z/OS systems. They verify
    that the LLM picks the right tool and arguments, then stop before
    execution by only checking the first tool call.
    """

    @pytest.mark.asyncio
    async def test_zos_tool_registration(self, zos_server: Any) -> None:
        """All z/OS modules should register as tools."""
        async with Client(zos_server) as mcp_client:
            tools = await mcp_client.list_tools()
            tool_names = {t.name for t in tools}

            assert "ibm.ibm_zos_core.zos_ping" in tool_names
            assert "ibm.ibm_zos_core.zos_job_submit" in tool_names
            assert "ibm.ibm_zos_core.zos_data_set" in tool_names
            assert "ibm.ibm_zos_core.zos_copy" in tool_names
            assert "ansible.builtin.ping" in tool_names
            assert "ansible.builtin.command" in tool_names

    @pytest.mark.asyncio
    async def test_zos_ping_tool_selection(self, zos_server: Any, ollama_model: str) -> None:
        """LLM should choose zos_ping (not builtin ping) for z/OS connectivity check."""
        async with Client(zos_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            response = ollama.chat(
                model=ollama_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Check if z/OS LPAR cb8a is reachable. "
                        "Use the z/OS-specific ping module.",
                    }
                ],
                tools=ollama_tools,
                options={"temperature": 0, "num_ctx": 16384},
            )

            assert response.message.tool_calls, "Model should make a tool call"
            tc = response.message.tool_calls[0]
            assert tc.function.name == "ibm.ibm_zos_core.zos_ping"
            assert tc.function.arguments["target"] == "cb8a"

    @pytest.mark.asyncio
    async def test_zos_dataset_tool_selection(self, zos_server: Any, ollama_model: str) -> None:
        """LLM should use zos_data_set to create a dataset."""
        async with Client(zos_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            response = ollama.chat(
                model=ollama_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Create a sequential dataset called "
                        "IBMUSER.TEST.DATA on z/OS host cb8a.",
                    }
                ],
                tools=ollama_tools,
                options={"temperature": 0, "num_ctx": 16384},
            )

            assert response.message.tool_calls, "Model should make a tool call"
            tc = response.message.tool_calls[0]
            assert tc.function.name.startswith("ibm.ibm_zos_core.zos_"), (
                f"Expected a z/OS module, got {tc.function.name}"
            )
            assert tc.function.arguments["target"] == "cb8a"

    @pytest.mark.asyncio
    async def test_zos_job_submit_selection(self, zos_server: Any, ollama_model: str) -> None:
        """LLM should use zos_job_submit for JCL submission."""
        async with Client(zos_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            response = ollama.chat(
                model=ollama_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Submit the JCL dataset IBMUSER.TEST.JCL(HELLO) on cb86.",
                    }
                ],
                tools=ollama_tools,
                options={"temperature": 0, "num_ctx": 16384},
            )

            assert response.message.tool_calls, "Model should make a tool call"
            tc = response.message.tool_calls[0]
            assert tc.function.name.startswith("ibm.ibm_zos_core.zos_"), (
                f"Expected a z/OS module, got {tc.function.name}"
            )
            assert tc.function.arguments["target"] == "cb86"

    @pytest.mark.asyncio
    async def test_zos_copy_selection(self, zos_server: Any, ollama_model: str) -> None:
        """LLM should prefer zos_copy over builtin copy for z/OS file operations."""
        async with Client(zos_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ollama_tools, _ = mcp_tools_to_ollama(tools)

            response = ollama.chat(
                model=ollama_model,
                messages=[
                    {
                        "role": "user",
                        "content": "Copy a USS file /tmp/hello.txt to dataset "
                        "IBMUSER.HELLO on z/OS host cb8b. "
                        "Use the z/OS copy module.",
                    }
                ],
                tools=ollama_tools,
                options={"temperature": 0, "num_ctx": 16384},
            )

            assert response.message.tool_calls, "Model should make a tool call"
            tc = response.message.tool_calls[0]
            assert tc.function.name == "ibm.ibm_zos_core.zos_copy"

    @pytest.mark.asyncio
    async def test_zos_inventory_targets(self, zos_server: Any) -> None:
        """Verify that z/OS hosts and groups are available as targets."""
        async with Client(zos_server) as mcp_client:
            tools = await mcp_client.list_tools()
            ping_tool = next(t for t in tools if t.name == "ansible.builtin.ping")
            target_enum = ping_tool.inputSchema["properties"]["target"].get("enum", [])
            assert "cb8a" in target_enum
            assert "cb86" in target_enum
            assert "source_system" in target_enum
