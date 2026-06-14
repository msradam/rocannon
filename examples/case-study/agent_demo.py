"""Drive Rocannon's MCP tools from natural language with Claude Haiku.

Uses the Claude Agent SDK against the logged-in Claude Code session (no API
key). Haiku is given plain-English tasks and a Rocannon MCP server; it picks
the right Ansible module, calls it against the real host, and answers.

Usage:
    uv run python examples/case-study/agent_demo.py [profile.yml]
"""

import asyncio
import json
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rocannon-demo-env/profile-agent.yml"

PROMPTS = [
    "What OS and version is host ubi9 running?",
    "Run 'uptime' on host ubi9 and report the load averages.",
    "Set the message of the day on host ubi9 to exactly 'Managed by Rocannon', and tell me whether it changed anything.",
]


def _result_text(content: object) -> str:
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def _render(message: object) -> None:
    for block in getattr(message, "content", []) or []:
        if isinstance(block, TextBlock) and block.text.strip():
            print(f"  haiku: {block.text.strip()}")
        elif isinstance(block, ToolUseBlock):
            print(f"  -> calls {block.name}")
            print(f"     args: {json.dumps(block.input)}")
        elif isinstance(block, ToolResultBlock):
            text = _result_text(block.content).replace("\n", " ")
            print(f"     result: {text[:240]}")


async def main() -> None:
    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        permission_mode="acceptEdits",
        allowed_tools=["mcp__rocannon__*"],
        # Strip the built-in Claude Code tools so Haiku sees only Rocannon's
        # Ansible tools (no sub-agents, no tool-search deferral, no shell).
        disallowed_tools=[
            "Task", "Bash", "BashOutput", "KillShell", "Read", "Edit", "Write",
            "NotebookEdit", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite",
            "ToolSearch", "Skill", "ExitPlanMode",
        ],
        setting_sources=[],
        mcp_servers={
            "rocannon": {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "--directory", str(ROOT), "rocannon", "mcp", "serve", "--profile", PROFILE],
            }
        },
    )
    async with ClaudeSDKClient(options=options) as client:
        for prompt in PROMPTS:
            print(f"\nUSER: {prompt}")
            await client.query(prompt)
            async for message in client.receive_response():
                _render(message)


if __name__ == "__main__":
    asyncio.run(main())
