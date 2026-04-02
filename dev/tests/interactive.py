#!/usr/bin/env python3
"""Interactive REPL for testing Rocannon with a local LLM via Ollama.

Connects FastMCP Client to the Rocannon server and runs a tool-calling
agent loop against a local Ollama model. No cloud APIs required.

On first run, automatically:
  1. Starts Ollama if not running
  2. Pulls the model if not present
  3. Starts test containers if not running
  4. Launches the REPL

Usage:
    uv run python tests/interactive.py
    uv run python tests/interactive.py --model llama3.1:8b
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import ollama
from fastmcp import Client

from rocannon.config import Config
from rocannon.server import create_server

INVENTORY = Path(__file__).resolve().parent.parent / "inventories" / "podman.yml"
DEFAULT_MODEL = "ibm/granite4:micro"
MODULES = [
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

CONTAINER_DEFS = {
    "rocannon-rhel": ("rocannon-test:rhel", 2222),
    "rocannon-sles": ("rocannon-test:sles", 2223),
    "rocannon-ubuntu": ("rocannon-test:ubuntu", 2224),
}


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _ensure_ollama_running() -> None:
    """Start Ollama server if not already running."""
    try:
        ollama.list()
        return
    except Exception:
        pass

    print("Starting Ollama...")
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(30):
        try:
            ollama.list()
            print("Ollama ready.")
            return
        except Exception:
            time.sleep(0.5)

    print("ERROR: Could not start Ollama. Is it installed?")
    sys.exit(1)


def _ensure_model(model: str) -> None:
    """Pull the model if not already available."""
    try:
        models = ollama.list()
        for m in models.models:
            if m.model == model or m.model.startswith(model.split(":")[0]):
                return
    except Exception:
        pass

    print(f"Pulling {model} (one-time download)...")
    ollama.pull(model)
    print(f"{model} ready.")


def _ensure_containers() -> None:
    """Start test containers if not running."""
    runtime = "podman" if shutil.which("podman") else "docker"

    for name, (tag, port) in CONTAINER_DEFS.items():
        result = subprocess.run(
            [runtime, "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        if name in result.stdout:
            continue

        start = subprocess.run([runtime, "start", name], capture_output=True, text=True)
        if start.returncode == 0:
            print(f"Started {name}")
            continue

        subprocess.run(
            [runtime, "run", "-d", "--name", name, "-p", f"{port}:22", tag],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"Created {name}")

    import socket

    for _, (_, port) in CONTAINER_DEFS.items():
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def mcp_tools_to_ollama(tools: list) -> list[dict]:
    """Convert MCP tool list to Ollama function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or t.name,
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]


def _build_system_prompt(tools: list) -> str:
    """Build a system prompt with inventory context so the model knows about targets."""
    targets: list[str] = []
    for t in tools:
        target_prop = t.inputSchema.get("properties", {}).get("target", {})
        if "enum" in target_prop:
            targets = target_prop["enum"]
            break

    hosts = [t for t in targets if not t.startswith("linuxone")]
    groups = [t for t in targets if t.startswith("linuxone")]

    return (
        "You are Rocannon, an Ansible automation assistant. "
        "You execute Ansible modules on remote hosts via tool calls.\n\n"
        "IMPORTANT: Every tool call MUST include the 'target' parameter. "
        "This is the host or group to run the module on.\n\n"
        f"Available hosts: {', '.join(hosts)}\n"
        f"Available groups: {', '.join(groups)}\n\n"
        "If the user doesn't specify a host, pick the most appropriate one "
        "or use the 'linuxone' group for all hosts."
    )


async def agent_loop(
    mcp_client: Client,
    ollama_tools: list[dict],
    model: str,
    prompt: str,
    system_prompt: str,
) -> str:
    """Run LLM → tool-call → result loop until the model produces a final answer."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    last_error: str | None = None
    consecutive_errors = 0

    for _ in range(10):
        response = ollama.chat(
            model=model,
            messages=messages,
            tools=ollama_tools,
            options={"temperature": 0, "num_ctx": 16384},
        )
        messages.append(response.message)

        if not response.message.tool_calls:
            return response.message.content or "(no response)"

        for tc in response.message.tool_calls:
            name = tc.function.name
            args = tc.function.arguments
            print(f"  → {name}({json.dumps(args, separators=(',', ':'))})")

            try:
                result = await mcp_client.call_tool(name, args)
                text = result.content[0].text if hasattr(result, "content") else str(result)
                last_error = None
                consecutive_errors = 0
            except Exception as exc:
                error_msg = str(exc)
                text = json.dumps({"error": error_msg})

                if error_msg == last_error:
                    consecutive_errors += 1
                    if consecutive_errors >= 2:
                        return f"(giving up — model keeps failing with: {error_msg})"
                else:
                    last_error = error_msg
                    consecutive_errors = 1

            messages.append({"role": "tool", "content": text})

    return "(max turns reached)"


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


async def repl(model: str) -> None:
    """Main REPL loop."""
    if not INVENTORY.exists():
        print(f"Inventory not found: {INVENTORY}")
        sys.exit(1)

    config = Config(inventories=[INVENTORY], modules=MODULES)
    server = create_server(config)

    async with Client(server) as mcp_client:
        tools = await mcp_client.list_tools()
        ollama_tools = mcp_tools_to_ollama(tools)
        system_prompt = _build_system_prompt(tools)
        print(f"Rocannon ready — {len(tools)} tools, model: {model}")
        print("Type a natural language command, or 'quit' to exit.\n")

        while True:
            try:
                prompt = input("rocannon> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not prompt or prompt in ("quit", "exit"):
                break

            answer = await agent_loop(mcp_client, ollama_tools, model, prompt, system_prompt)
            print(f"\n{answer}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive Rocannon REPL")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model [{DEFAULT_MODEL}]")
    args = parser.parse_args()

    _ensure_ollama_running()
    _ensure_model(args.model)
    _ensure_containers()

    asyncio.run(repl(args.model))


if __name__ == "__main__":
    main()
