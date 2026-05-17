"""Real agent test: natural language → Granite 4 → MCP tool call → ansible-runner → result.

Uses Ollama with granite4:3b-32k against a curated multi-collection profile
spanning all three demo use cases (diagnostics, network, containers).

Records per-prompt outcomes to scratch/results/agent.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "dev" / "tests"))

import ollama  # noqa: E402
from fastmcp import Client  # noqa: E402
from mcp.types import TextContent  # noqa: E402

from rocannon.config import Config  # noqa: E402
from rocannon.server import create_server  # noqa: E402

MODEL = "granite4:3b-32k"
RESULTS = REPO / "scratch" / "results" / "agent.jsonl"
RESULTS.parent.mkdir(parents=True, exist_ok=True)

# Focused module set spanning the three use cases — keeps tool list small enough
# for a 3B model to reason cleanly about while still being demo-realistic.
MODULES = [
    "ansible.builtin.ping",
    "ansible.builtin.setup",
    "ansible.builtin.command",
    "ansible.builtin.shell",
    "ansible.builtin.copy",
    "ansible.builtin.file",
    "ansible.builtin.stat",
    "ansible.builtin.find",
    "ansible.builtin.service_facts",
    "ansible.builtin.package_facts",
    "ansible.builtin.iptables",
    "ansible.builtin.hostname",
    "ansible.posix.sysctl",
    "containers.podman.podman_image",
    "containers.podman.podman_container",
    "containers.podman.podman_network",
    "containers.podman.podman_pod",
]

SYSTEM_PROMPT = """You are an infrastructure operator agent. You have tools that
execute Ansible modules across a small fleet:
- linuxone group: ubuntu, rhel, sles (Linux containers via SSH)
- services group: pg-host (local connection for podman/container ops)

Pick ONE tool per turn. Pass the right `target` from the inventory.
Use minimal arguments — only what's needed. After tools return, summarize
the result for the user in one or two sentences."""


def mcp_tools_to_ollama(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": t.name,
         "description": t.description or t.name, "parameters": t.inputSchema}}
        for t in tools
    ]


async def run_agent(mcp_client: Client, ollama_tools: list[dict[str, Any]],
                    prompt: str, max_turns: int = 4) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    raw: list[str] = []
    final = ""

    for turn in range(max_turns):
        resp = ollama.chat(model=MODEL, messages=messages, tools=ollama_tools,
                           options={"temperature": 0, "num_ctx": 32000})
        msg = resp.message
        messages.append({"role": "assistant", "content": msg.content or ""})
        if not msg.tool_calls:
            final = msg.content or ""
            break
        for tc in msg.tool_calls:
            name = tc.function.name
            args = dict(tc.function.arguments)
            tool_calls.append((name, args))
            try:
                result = await mcp_client.call_tool(name, args)
                c = result.content[0] if result.content else None
                text = c.text if isinstance(c, TextContent) else str(c)
            except Exception as exc:
                text = json.dumps({"error": str(exc)})
            raw.append(text)
            messages.append({"role": "tool", "content": text[:4000]})
    return {"tool_calls": tool_calls, "final": final, "raw": raw, "turns": turn + 1}


# (prompt, expected_tool_substring or None, expected_target or None, success_check)
PROMPTS: list[tuple[str, str, str | None, str | None]] = [
    # Use case 1 — diagnostics
    ("Ping the ubuntu host to check it's reachable.",
     "diagnostics", "ansible.builtin.ping", "ubuntu"),
    ("Gather distribution facts from the rhel host.",
     "diagnostics", "ansible.builtin.setup", "rhel"),
    ("List running services on sles.",
     "diagnostics", "ansible.builtin.service_facts", "sles"),
    ("How much disk space is left on /  on ubuntu? Use a shell command.",
     "diagnostics", "command", "ubuntu"),
    ("Find all .log files in /var/log on rhel.",
     "diagnostics", "ansible.builtin.find", "rhel"),

    # Use case 2 — network config
    ("Enable IPv4 forwarding (net.ipv4.ip_forward=1) on ubuntu.",
     "network", "ansible.posix.sysctl", "ubuntu"),
    ("Add an iptables rule on rhel to allow tcp port 8080 on the INPUT chain.",
     "network", "ansible.builtin.iptables", "rhel"),

    # Use case 3 — containers
    ("Pull the alpine:3 image on pg-host using podman.",
     "containers", "containers.podman.podman_image", "pg-host"),
    ("Start a container named agent-demo from alpine:3 on pg-host that runs 'sleep 60'.",
     "containers", "containers.podman.podman_container", "pg-host"),
    ("Remove the container named agent-demo on pg-host.",
     "containers", "containers.podman.podman_container", "pg-host"),
]


async def main() -> None:
    config = Config(inventories=[REPO / "scratch" / "inventory.yml"], modules=MODULES)
    mcp_server = create_server(config)
    print(f"Server registered tools. Probing list...")
    async with Client(mcp_server) as mcp_client:
        tools = await mcp_client.list_tools()
        print(f"Registered {len(tools)} tools for agent.")
        ollama_tools = mcp_tools_to_ollama(tools)

        print(f"\nRunning {len(PROMPTS)} prompts against {MODEL}...\n")
        passes = 0
        for prompt, use_case, exp_tool, exp_target in PROMPTS:
            t0 = time.monotonic()
            try:
                r = await run_agent(mcp_client, ollama_tools, prompt)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                tool_picked = r["tool_calls"][0][0] if r["tool_calls"] else None
                target_picked = r["tool_calls"][0][1].get("target") if r["tool_calls"] else None

                tool_match = bool(tool_picked and exp_tool in tool_picked)
                target_match = (target_picked == exp_target) if exp_target else True

                executed_ok = False
                if r["raw"]:
                    try:
                        first = json.loads(r["raw"][0])
                        executed_ok = first.get("status") == "successful"
                    except (json.JSONDecodeError, AttributeError):
                        pass

                verdict = "PASS" if (tool_match and target_match and executed_ok) else "PARTIAL" if tool_match else "FAIL"
                if verdict == "PASS":
                    passes += 1
                rec = {
                    "use_case": use_case,
                    "prompt": prompt,
                    "expected_tool": exp_tool,
                    "expected_target": exp_target,
                    "tool_picked": tool_picked,
                    "target_picked": target_picked,
                    "tool_match": tool_match,
                    "target_match": target_match,
                    "executed_ok": executed_ok,
                    "verdict": verdict,
                    "turns": r["turns"],
                    "elapsed_ms": elapsed_ms,
                    "all_calls": [{"name": n, "args": a} for n, a in r["tool_calls"]],
                    "final_text": r["final"][:300],
                }
                with RESULTS.open("a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
                flag = {"PASS": "✓", "PARTIAL": "~", "FAIL": "✗"}[verdict]
                print(f"  {flag} [{verdict}] [{use_case:12s}] tool={tool_picked} target={target_picked} {elapsed_ms}ms")
                print(f"      prompt: {prompt[:80]}")
                if verdict != "PASS":
                    print(f"      expected: tool~={exp_tool} target={exp_target}; executed_ok={executed_ok}")
            except Exception as exc:
                print(f"  ✗ [ERROR] {prompt[:60]}: {exc}")
                with RESULTS.open("a") as f:
                    f.write(json.dumps({"prompt": prompt, "use_case": use_case,
                                        "verdict": "ERROR", "error": str(exc)},
                                       default=str) + "\n")
        print(f"\n=== {passes}/{len(PROMPTS)} full PASS ===")


if __name__ == "__main__":
    asyncio.run(main())
