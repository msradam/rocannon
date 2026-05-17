"""Five demo-quality scenarios — multi-turn agent conversations against the
3-OS Linux fleet + local Podman, captured verbatim for presentation.

Output: scratch/results/demo_transcripts/<scenario>.md (per-scenario)
        DEMO_SCENARIOS.md (assembled top-level)
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import ollama  # noqa: E402
from fastmcp import Client  # noqa: E402
from mcp.types import TextContent  # noqa: E402

from rocannon.config import Config  # noqa: E402
from rocannon.server import create_server  # noqa: E402

MODEL = "granite4:3b-32k"
TRANSCRIPT_DIR = REPO / "scratch" / "results" / "demo_transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

MODULES = [
    "ansible.builtin.ping", "ansible.builtin.setup", "ansible.builtin.command",
    "ansible.builtin.shell", "ansible.builtin.copy", "ansible.builtin.file",
    "ansible.builtin.stat", "ansible.builtin.find", "ansible.builtin.service_facts",
    "ansible.builtin.package_facts", "ansible.builtin.iptables",
    "ansible.builtin.lineinfile", "ansible.builtin.systemd",
    "ansible.posix.sysctl",
    "containers.podman.podman_image", "containers.podman.podman_container",
    "containers.podman.podman_network", "containers.podman.podman_pod",
]

SYSTEM_PROMPT = """You are an SRE/operator agent that runs Ansible modules
against a small fleet through tool calls. The fleet:
  - linuxone group (Linux containers via SSH): ubuntu, rhel, sles
  - services group (local connection on the controller): pg-host, mongo-host

EVERY tool call MUST include a `target` parameter — the host or group from
the inventory above. There is no default.

When the user asks a question or gives an instruction:
  1. Plan: what tools and which targets do I need?
  2. Call one tool at a time. Wait for the result.
  3. After tools return, summarize concisely for the user.
  4. Use natural language to explain decisions.

Prefer specific modules over shell commands when one exists
(e.g. service_facts over `systemctl list-units`). Use ansible.builtin.command
or shell for ad-hoc queries that don't have a dedicated module.

Container operations target pg-host (which has access to the local podman
daemon). Linux fleet operations target ubuntu, rhel, sles individually
or the linuxone group."""


# Always preserve these fact keys verbatim if present (drift / inventory queries
# rely on them; everything else gets aggressively trimmed).
PRIORITY_FACTS = (
    "ansible_distribution", "ansible_distribution_version", "ansible_distribution_release",
    "ansible_os_family", "ansible_kernel", "ansible_machine", "ansible_architecture",
    "ansible_python_version", "ansible_hostname", "ansible_fqdn", "ansible_lsb",
    "ansible_processor_count", "ansible_memtotal_mb", "ansible_pkg_mgr",
    "ansible_service_mgr", "ansible_selinux", "ansible_uptime_seconds",
)


def trim_result(text: str, max_len: int = 3000) -> str:
    """Compact a tool result for the model — keep status, changed, priority facts, key fields."""
    try:
        d = json.loads(text)
        keep: dict[str, Any] = {"status": d.get("status"), "changed": d.get("changed")}
        res = d.get("result", {})
        if isinstance(res, dict):
            af = res.get("ansible_facts")
            if isinstance(af, dict):
                priority = {k: af[k] for k in PRIORITY_FACTS if k in af}
                if priority:
                    keep["ansible_facts"] = priority
            # service_facts puts data under result.services
            if isinstance(res.get("services"), dict):
                svcs = res["services"]
                keep["services_count"] = len(svcs)
                running = [n for n, s in svcs.items()
                           if isinstance(s, dict) and s.get("state") == "running"][:25]
                keep["services_running_sample"] = running
            # package_facts puts data under result.packages
            if isinstance(res.get("packages"), dict):
                keep["packages_count"] = len(res["packages"])
                keep["packages_sample"] = list(res["packages"].keys())[:25]
            # stat
            if isinstance(res.get("stat"), dict):
                st = res["stat"]
                keep["stat"] = {k: st[k] for k in ("exists", "isdir", "isreg",
                                "size", "mode", "pw_name") if k in st}
            # find
            if isinstance(res.get("files"), list):
                keep["files_matched"] = res.get("matched", len(res["files"]))
                keep["files_sample"] = [{"path": f.get("path"), "size": f.get("size")}
                                         for f in res["files"][:10]]
            # ad-hoc
            for k in ("msg", "stdout", "stderr", "rc", "failed"):
                if k in res:
                    v = res[k]
                    if isinstance(v, str) and len(v) > 1500:
                        keep[k] = v[:1500] + "...[truncated]"
                    else:
                        keep[k] = v
        s = json.dumps(keep, default=str)
        return s if len(s) <= max_len else s[:max_len] + "...[truncated]"
    except (json.JSONDecodeError, TypeError):
        return text[:max_len]


async def run_conversation(mcp_client: Client, ollama_tools: list[dict[str, Any]],
                            operator_prompts: list[str], max_turns: int = 8) -> dict[str, Any]:
    """Run a multi-turn conversation. operator_prompts is a list — typically
    one item, but can be multiple for follow-up questions."""
    transcript: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    total_calls = 0
    t_start = time.monotonic()

    for prompt in operator_prompts:
        messages.append({"role": "user", "content": prompt})
        transcript.append({"role": "user", "text": prompt, "ts": time.monotonic() - t_start})

        for _turn in range(max_turns):
            resp = ollama.chat(model=MODEL, messages=messages, tools=ollama_tools,
                               options={"temperature": 0, "num_ctx": 32000})
            msg = resp.message
            content = msg.content or ""
            tool_calls = msg.tool_calls or []

            messages.append({"role": "assistant", "content": content,
                             "tool_calls": [tc.model_dump() if hasattr(tc, "model_dump") else tc
                                            for tc in tool_calls] if tool_calls else None})

            if tool_calls:
                transcript.append({"role": "assistant", "thought": content,
                                   "tool_calls": [{"name": tc.function.name,
                                                   "args": dict(tc.function.arguments)}
                                                  for tc in tool_calls],
                                   "ts": time.monotonic() - t_start})
                for tc in tool_calls:
                    name = tc.function.name
                    args = dict(tc.function.arguments)
                    total_calls += 1
                    try:
                        result = await mcp_client.call_tool(name, args)
                        c = result.content[0] if result.content else None
                        text = c.text if isinstance(c, TextContent) else str(c)
                        compact = trim_result(text)
                    except Exception as exc:
                        text = json.dumps({"error": str(exc)[:500]})
                        compact = text
                    transcript.append({"role": "tool",
                                       "name": name, "args": args,
                                       "result": compact,
                                       "ts": time.monotonic() - t_start})
                    messages.append({"role": "tool", "content": compact})
            else:
                transcript.append({"role": "assistant_final", "text": content,
                                   "ts": time.monotonic() - t_start})
                break

    return {"transcript": transcript, "total_calls": total_calls,
            "elapsed_seconds": round(time.monotonic() - t_start, 1)}


# ============================================================
# SCENARIOS
# ============================================================

def setup_scenario_1_incident() -> None:
    """Plant a broken service condition on rhel container."""
    # Stop a default service, fill /var/log with a suspicious-looking entry
    subprocess.run(["podman", "exec", "rocannon-rhel", "bash", "-c",
                    "echo 'ERROR [database] connection refused: too many open files' >> /var/log/app.log; "
                    "echo 'CRITICAL [database] pool exhausted' >> /var/log/app.log; "
                    "fallocate -l 100M /tmp/big_file 2>/dev/null || dd if=/dev/zero of=/tmp/big_file bs=1M count=100 2>/dev/null"],
                   capture_output=True, timeout=30)


def setup_scenario_4_containers() -> None:
    subprocess.run(["podman", "rm", "-f", "demo-pg", "demo-redis", "demo-stack"],
                   capture_output=True, timeout=30)
    subprocess.run(["podman", "network", "rm", "-f", "demo-net"],
                   capture_output=True, timeout=30)


def teardown_scenario_4_containers() -> None:
    setup_scenario_4_containers()


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "1_incident",
        "title": "Incident triage — 'something's wrong with rhel'",
        "narrative": (
            "An on-call engineer wakes up to a Slack message: 'rhel host is misbehaving — "
            "the database keeps timing out.' They open Claude Desktop, where Rocannon is "
            "configured as an MCP server, and ask the agent to investigate."
        ),
        "setup": setup_scenario_1_incident,
        "prompts": [
            "Something's wrong with the rhel host — the database is timing out. "
            "Investigate by running these checks on rhel, in order: "
            "(1) ping rhel to verify reachability, "
            "(2) use the command module with cmd='df -h /tmp' to check disk usage, "
            "(3) use the find module with paths='/var/log' patterns='*.log' to enumerate log files, "
            "(4) use the command module with cmd='grep -i error /var/log/app.log' to look for errors. "
            "Then tell me what you found and what's likely wrong."
        ],
        "max_turns": 10,
    },
    {
        "id": "2_drift",
        "title": "Fleet drift detection — 'what's different across our Linux fleet?'",
        "narrative": (
            "Before a quarterly compliance audit, the platform team wants a quick read on "
            "how consistent their three-OS fleet is. They ask the agent to compare facts "
            "across ubuntu, rhel, and sles."
        ),
        "setup": None,
        "prompts": [
            "Gather distribution and kernel facts for ubuntu, rhel, and sles. "
            "Tell me which OS family each is running, the kernel version, "
            "and call out anything notably different between the three."
        ],
        "max_turns": 8,
    },
    {
        "id": "3_hardening",
        "title": "Compliance hardening — 'apply baseline to the fleet'",
        "narrative": (
            "A security baseline review identified two gaps: IPv4 forwarding should be "
            "explicitly enabled (router role) and port 9999 must be blocked at the host "
            "firewall. The agent applies both across all three OSes idempotently."
        ),
        "setup": None,
        "prompts": [
            "Apply this baseline to ubuntu, rhel, and sles: "
            "STEP 1 (IPv4 forwarding): for each host, call ansible.posix.sysctl with "
            "name='net.ipv4.ip_forward', value='1', state='present', sysctl_set=true, reload=false. "
            "STEP 2 (firewall block): for each host, call ansible.builtin.iptables with "
            "chain='INPUT', protocol='tcp', destination_port='9999', jump='DROP', comment='baseline-block-9999'. "
            "Do step 1 on all three hosts, then step 2 on all three. "
            "Then summarize: 'Applied baseline to <hosts>: ip_forward=1 (changed=N), iptables drop 9999 (changed=N).'"
        ],
        "max_turns": 12,
    },
    {
        "id": "4_stack",
        "title": "Container stack deployment — 'stand up a postgres+redis pod'",
        "narrative": (
            "An app team needs a quick local stack: postgres + redis sharing a pod, with "
            "an isolated network. The agent pulls images, builds the network, creates the "
            "pod, and starts both containers in it."
        ),
        "setup": setup_scenario_4_containers,
        "teardown": teardown_scenario_4_containers,
        "prompts": [
            "Deploy a stack for the app team on pg-host. Use the podman tools. "
            "STEP 1: pull docker.io/library/postgres:16 — call containers.podman.podman_image "
            "with name='docker.io/library/postgres:16', state='present', target='pg-host'. "
            "STEP 2: pull docker.io/library/redis:7 — same module, name='docker.io/library/redis:7', "
            "state='present', target='pg-host'. "
            "STEP 3: create the network — call containers.podman.podman_network with "
            "name='demo-net', state='present', target='pg-host'. "
            "STEP 4: create the pod — call containers.podman.podman_pod with "
            "name='demo-stack', state='started', network='demo-net', target='pg-host'. "
            "STEP 5: start postgres in the pod — call containers.podman.podman_container with "
            "name='demo-pg', image='docker.io/library/postgres:16', pod='demo-stack', "
            "env={'POSTGRES_PASSWORD':'demo'}, state='started', target='pg-host'. "
            "STEP 6: start redis in the pod — call containers.podman.podman_container with "
            "name='demo-redis', image='docker.io/library/redis:7', pod='demo-stack', "
            "state='started', target='pg-host'. "
            "Do the steps strictly in order. Then summarize what's running."
        ],
        "max_turns": 14,
    },
    {
        "id": "5_audit",
        "title": "Package audit — 'who has openssl below 3.0.7?'",
        "narrative": (
            "A CVE notice references openssl 3.0.6 and below. The agent sweeps the fleet "
            "for installed openssl versions to flag at-risk hosts."
        ),
        "setup": None,
        "prompts": [
            "Check each of ubuntu, rhel, sles for the installed openssl version. "
            "For each host, use the command module with cmd='openssl version' once. "
            "After all three are done, tell me each version and "
            "flag any host below 3.0.7."
        ],
        "max_turns": 8,
    },
]


# ============================================================
# Transcript formatter
# ============================================================

def format_transcript(scen: dict[str, Any], result: dict[str, Any]) -> str:
    md: list[str] = []
    md.append(f"# Scenario {scen['id'][0]}: {scen['title']}\n")
    md.append(f"_{scen['narrative']}_\n")
    md.append(f"**Model:** `{MODEL}`  ·  **Tool calls:** {result['total_calls']}  ·  "
              f"**Elapsed:** {result['elapsed_seconds']}s\n")

    for entry in result["transcript"]:
        role = entry["role"]
        ts = entry.get("ts", 0)
        if role == "user":
            md.append(f"\n### 👤 Operator [t+{ts:.1f}s]\n")
            md.append(f"> {entry['text']}\n")
        elif role == "assistant":
            md.append(f"\n### 🤖 Agent [t+{ts:.1f}s]")
            if entry.get("thought"):
                md.append(f"\n_{entry['thought'][:500]}_\n")
            md.append("")
            for tc in entry["tool_calls"]:
                args_str = json.dumps(tc["args"], indent=2)
                md.append(f"**Tool call:** `{tc['name']}`")
                md.append("```json")
                md.append(args_str)
                md.append("```\n")
        elif role == "tool":
            md.append(f"\n### 🔧 Tool result [t+{ts:.1f}s]")
            md.append("```json")
            md.append(entry["result"][:1500])
            md.append("```\n")
        elif role == "assistant_final":
            md.append(f"\n### 🤖 Agent — final answer [t+{ts:.1f}s]\n")
            md.append(f"{entry['text']}\n")
    return "\n".join(md)


# ============================================================
# Main
# ============================================================

async def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    config = Config(inventories=[REPO / "scratch" / "inventory.yml"], modules=MODULES)
    server = create_server(config)
    print(f"Server ready with module set ({len(MODULES)} modules)\n")

    async with Client(server) as mcp_client:
        tools = await mcp_client.list_tools()
        ollama_tools = [
            {"type": "function", "function": {"name": t.name,
             "description": t.description or t.name, "parameters": t.inputSchema}}
            for t in tools
        ]
        print(f"Registered {len(tools)} tools\n")

        for scen in SCENARIOS:
            if only and scen["id"] != only and not scen["id"].startswith(only):
                continue
            print(f"━━━ Scenario: {scen['title']} ━━━")
            if scen.get("setup"):
                scen["setup"]()
                print("  setup: ok")
            try:
                result = await run_conversation(mcp_client, ollama_tools,
                                                scen["prompts"], scen["max_turns"])
                md = format_transcript(scen, result)
                out = TRANSCRIPT_DIR / f"{scen['id']}.md"
                out.write_text(md)
                print(f"  transcript: {out} — {result['total_calls']} calls, "
                      f"{result['elapsed_seconds']}s")
            except Exception as exc:
                print(f"  ERROR: {exc}")
            finally:
                if scen.get("teardown"):
                    scen["teardown"]()
            print()


if __name__ == "__main__":
    asyncio.run(main())
