"""Drive Rocannon against a containerlab network fabric, in natural language.

Claude Haiku (via the Claude Agent SDK, reusing the logged-in Claude Code
session) is given plain-English tasks and a Rocannon MCP server whose tools are
the arista.eos modules. It gathers device state, inspects topology, and pushes a
config change against real Arista cEOS nodes running under containerlab.

Rocannon runs wherever the lab's management network is reachable. By default it
is launched locally (the usual case: containerlab and the agent on the same
Linux host). Set ROCANNON_SSH=user@host to run it over SSH on a remote lab host
instead.

Usage:
    uv run python examples/containerlab/agent_demo.py [profile.yml]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "ceos-profile.yml")

PROMPTS = [
    "What model and EOS software version is ceos1 running?",
    "What is ceos1 directly connected to? Look at its LLDP neighbors.",
    "Set the login banner on ceos1 to exactly 'Managed by Rocannon'"
    " and tell me whether it changed anything.",
]


def _mcp_server() -> dict:
    remote = os.environ.get("ROCANNON_SSH")
    if remote:
        serve = os.environ.get(
            "ROCANNON_SSH_CMD",
            f"cd {ROOT} && uv run rocannon mcp serve --profile {PROFILE}",
        )
        return {"type": "stdio", "command": "ssh", "args": ["-T", remote, serve]}
    return {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--directory", str(ROOT), "rocannon", "mcp", "serve", "--profile", PROFILE],
    }


_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def _result_text(content: object) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def _fmt_value(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _fmt_args(args: dict[str, object]) -> str:
    return "  ".join(f"{k}={_fmt_value(v)}" for k, v in args.items())


def _summarize_result(content: object) -> str:
    text = _result_text(content)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text.replace("\n", " ")[:160]
    if isinstance(data, dict) and "status" in data:
        bits = [f"status={data.get('status')}"]
        if data.get("changed") is not None:
            bits.append(f"changed={data.get('changed')}")
        return "  ".join(bits)
    return text.replace("\n", " ")[:160]


def _render(message: object) -> None:
    for block in getattr(message, "content", []) or []:
        if isinstance(block, TextBlock) and block.text.strip():
            print(f"  {_c('36', 'claude')}  {block.text.strip()}")
        elif isinstance(block, ToolUseBlock):
            name = _c("1", block.name.removeprefix("mcp__rocannon__"))
            print(f"  {_c('33', '→')} {name}  {_c('2', _fmt_args(block.input))}")
        elif isinstance(block, ToolResultBlock):
            print(f"    {_c('2', '↳ ' + _summarize_result(block.content))}")


async def main() -> None:
    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        permission_mode="acceptEdits",
        allowed_tools=["mcp__rocannon__*"],
        # Strip the built-in Claude Code tools so Haiku sees only Rocannon's
        # Ansible tools (no sub-agents, no tool-search deferral, no shell).
        disallowed_tools=[
            "Task",
            "Bash",
            "BashOutput",
            "KillShell",
            "Read",
            "Edit",
            "Write",
            "NotebookEdit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
            "TodoWrite",
            "ToolSearch",
            "Skill",
            "ExitPlanMode",
        ],
        setting_sources=[],
        mcp_servers={"rocannon": _mcp_server()},
    )
    async with ClaudeSDKClient(options=options) as client:
        # The first query spawns and connects the MCP server, which lags; drain
        # a throwaway turn so the real prompts see the tools.
        print("connecting to the Rocannon MCP server...")
        await client.query("Which hosts can you manage?")
        async for _ in client.receive_response():
            pass
        for prompt in PROMPTS:
            print(f"\nUSER: {prompt}")
            await client.query(prompt)
            async for message in client.receive_response():
                _render(message)


if __name__ == "__main__":
    asyncio.run(main())
