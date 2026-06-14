"""Drive the Rocannon Execution Environment from natural language with Haiku.

The MCP server here IS the EE container: the Agent SDK launches
`docker run -i rocannon-ee:demo rocannon mcp serve ...`, so Haiku is talking to
the Rocannon baked into the frozen image, executing against the EE's own host.

Build the image first:
    ansible-builder build -t rocannon-ee:demo -f execution-environment.yml
Then:
    uv run python examples/execution-environment/agent_demo.py
"""

import asyncio
import json
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

EE_DIR = Path(__file__).resolve().parent

PROMPTS = [
    "Run 'uname -a' on the local host and report the kernel version.",
    "Gather the host facts and tell me the OS distribution and Python version.",
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
            print(f"  -> calls {block.name}  {json.dumps(block.input)}")
        elif isinstance(block, ToolResultBlock):
            print(f"     result: {_result_text(block.content).replace(chr(10), ' ')[:200]}")


async def main() -> None:
    options = ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        permission_mode="acceptEdits",
        allowed_tools=["mcp__rocannon__*"],
        disallowed_tools=[
            "Task", "Bash", "BashOutput", "KillShell", "Read", "Edit", "Write",
            "NotebookEdit", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite",
            "ToolSearch", "Skill", "ExitPlanMode",
        ],
        setting_sources=[],
        mcp_servers={
            "rocannon": {
                "type": "stdio",
                "command": "docker",
                "args": [
                    "run", "-i", "--rm",
                    "-v", f"{EE_DIR}:/cfg",
                    "rocannon-ee:demo",
                    "rocannon", "mcp", "serve", "--profile", "/cfg/profile-agent.yml",
                ],
            }
        },
    )
    async with ClaudeSDKClient(options=options) as client:
        # The first query spawns and connects the MCP server (a `docker run`
        # here), which lags; drain a throwaway turn so the real prompts see the
        # tools.
        await client.query("Which host can you manage?")
        async for _ in client.receive_response():
            pass
        for prompt in PROMPTS:
            print(f"\nUSER: {prompt}")
            await client.query(prompt)
            async for message in client.receive_response():
                _render(message)


if __name__ == "__main__":
    asyncio.run(main())
