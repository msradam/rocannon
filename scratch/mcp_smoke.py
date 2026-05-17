"""Spawn rocannon serve as subprocess, drive over MCP stdio, exercise tool calls.
Verifies the FastMCP wire layer plus ansible-runner integration.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


async def main() -> None:
    # Use FastMCP client to talk to rocannon over stdio
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command="uv",
        args=[
            "run", "--no-sync", "rocannon", "serve",
            "--profile", "scratch/profile_test.yml",
            "--log-level", "warning",
        ],
        env={"VIRTUAL_ENV": str(REPO / ".venv"), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )

    out: dict = {"events": []}
    t_start = time.monotonic()
    try:
        async with Client(transport) as client:
            t_ready = time.monotonic() - t_start
            tools = await client.list_tools()
            out["tools_count"] = len(tools)
            out["startup_seconds"] = round(t_ready, 2)
            out["sample_tool_names"] = [t.name for t in tools[:5]]

            # Find ping tool
            ping = next((t for t in tools if t.name == "ansible.builtin.ping"), None)
            out["has_ping"] = ping is not None
            if ping:
                # Inspect schema
                schema = ping.inputSchema
                out["ping_schema_keys"] = sorted(schema.get("properties", {}).keys())
                out["ping_target_enum"] = schema["properties"]["target"].get("enum")

                # Call it
                t0 = time.monotonic()
                result = await client.call_tool("ansible.builtin.ping", {"target": "ubuntu"})
                out["ping_call_ms"] = int((time.monotonic() - t0) * 1000)
                content = result.content[0].text if result.content else ""
                parsed = json.loads(content)
                out["ping_status"] = parsed.get("status")
                out["ping_changed"] = parsed.get("changed")
                # Bad target — Literal should reject at schema layer
                try:
                    t0 = time.monotonic()
                    bad = await client.call_tool("ansible.builtin.ping", {"target": "nonexistent"})
                    out["bad_target_handled"] = "accepted-by-server"
                    out["bad_target_response"] = (bad.content[0].text[:200] if bad.content else "")
                    out["bad_target_ms"] = int((time.monotonic() - t0) * 1000)
                except Exception as e:
                    out["bad_target_handled"] = "rejected-by-schema"
                    out["bad_target_error"] = str(e)[:300]

            # Find a community.docker tool
            docker_tool = next((t for t in tools if "docker_image" in t.name), None)
            out["has_docker_image"] = docker_tool is not None

            # Scoping: did only requested collections register?
            prefixes = set()
            for t in tools:
                parts = t.name.rsplit(".", 1)
                if len(parts) == 2:
                    prefixes.add(parts[0])
            out["registered_collections"] = sorted(prefixes)

    except Exception as e:
        out["error"] = str(e)
        out["error_type"] = type(e).__name__
    out["total_seconds"] = round(time.monotonic() - t_start, 2)

    Path(REPO / "scratch" / "results" / "mcp_smoke.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
