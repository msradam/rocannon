"""Three-cannon demo: drive Ansible + Terraform + Helm through one Rocannon MCP server.

Targets prepared by ``setup.sh``:
  - UBI9 SSH container at 127.0.0.1:2222 (Ansible)
  - Docker daemon via Colima socket (Terraform)
  - kind cluster ``rocannon-test`` (Helm)

This is the same in-process MCP client an external agent would speak to.
Output is JSON-y and intentionally structured, meant to be captured as a
transcript artifact for the pitch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp.client import Client

from rocannon.config import (
    Config,
    HelmChartSpec,
    HelmConfig,
    TerraformConfig,
    TerraformModuleSpec,
)
from rocannon.server import create_server

HERE = Path(__file__).resolve().parent
WORK = HERE / "_work"

# Section banner, short and grep-able for the transcript reader.
def banner(text: str) -> None:
    print()
    print("─" * 72)
    print(f"  {text}")
    print("─" * 72)


def show(label: str, payload: Any, *, max_chars: int = 800) -> None:
    text = json.dumps(payload, indent=2, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n  …({len(text) - max_chars} more chars)"
    print(f"{label}")
    for line in text.splitlines():
        print(f"  {line}")


async def call(c: Client, tool: str, args: dict[str, Any]) -> Any:
    """Call a tool, return ``.data``/parsed text. Never throws, errors come back as dicts."""
    try:
        r = await c.call_tool(tool, args)
    except Exception as exc:
        return {"_demo_error": str(exc), "tool": tool}
    if getattr(r, "data", None) is not None:
        return r.data
    if r.content:
        try:
            return json.loads(r.content[0].text)
        except Exception:
            return r.content[0].text
    return None


def colima_socket() -> str:
    home = Path.home()
    return f"unix://{home}/.colima/default/docker.sock"


def build_config() -> Config:
    inv = WORK / "hosts.ini"
    if not inv.exists():
        sys.exit(
            f"inventory not found: {inv}\n"
            "Run ./setup.sh first."
        )
    return Config(
        # Ansible
        inventories=[inv],
        modules=[
            "ansible.builtin.ping",
            "ansible.builtin.setup",
            "ansible.builtin.command",
            "ansible.builtin.shell",
            "ansible.builtin.dnf",
            "ansible.builtin.service",
        ],
        # Terraform, providers (docker) + a community module (cloudposse/label/null)
        terraform=TerraformConfig(
            workspace=WORK / "tf",
            providers={
                "docker": {"source": "kreuzwerker/docker", "version": "~> 3.0"},
                "null":   {"source": "hashicorp/null",     "version": "~> 3.2"},
            },
            provider_config={"docker": {"host": colima_socket()}},
            modules=[
                TerraformModuleSpec(
                    source="cloudposse/label/null", version="0.25.0",
                ),
            ],
        ),
        # Helm, install a chart into the kind cluster
        helm=HelmConfig(
            charts=[HelmChartSpec(name="bitnami/nginx", version="21.0.6")],
            default_namespace="rocannon-demo",
        ),
    )


async def section_ansible(c: Client) -> None:
    banner("ANSIBLE, Red Hat target diagnostics")

    # ping
    show("> ansible.builtin.ping", await call(c, "ansible.builtin.ping", {"target": "ubi9"}))

    # gather facts (just trim, full output is huge)
    facts = await call(c, "ansible.builtin.setup", {"target": "ubi9"})
    af = (facts or {}).get("result", {}).get("ansible_facts", {})
    summary = {
        "distribution": af.get("ansible_distribution"),
        "distribution_version": af.get("ansible_distribution_version"),
        "architecture": af.get("ansible_architecture"),
        "memtotal_mb": af.get("ansible_memtotal_mb"),
        "memfree_mb": af.get("ansible_memfree_mb"),
        "processor_cores": af.get("ansible_processor_cores"),
        "default_ipv4": af.get("ansible_default_ipv4", {}).get("address"),
    }
    show("> ansible.builtin.setup  (excerpt of facts)", summary)

    # network: ip addr
    show(
        "> ansible.builtin.command  cmd='ip -br addr'",
        await call(c, "ansible.builtin.command", {
            "target": "ubi9", "cmd": "ip -br addr",
        }),
    )

    # memory: free -h
    show(
        "> ansible.builtin.command  cmd='free -h'",
        await call(c, "ansible.builtin.command", {
            "target": "ubi9", "cmd": "free -h",
        }),
    )

    # processes (use shell, command doesn't do pipes)
    show(
        "> ansible.builtin.shell  cmd='ps -eo pid,user,comm,pcpu,pmem --sort=-pmem | head -10'",
        await call(c, "ansible.builtin.shell", {
            "target": "ubi9",
            "cmd": "ps -eo pid,user,comm,pcpu,pmem --sort=-pmem | head -10",
        }),
    )

    # disk
    show(
        "> ansible.builtin.command  cmd='df -h /'",
        await call(c, "ansible.builtin.command", {
            "target": "ubi9", "cmd": "df -h /",
        }),
    )


async def section_terraform(c: Client) -> None:
    banner("TERRAFORM, provider resources (docker) + community module (label)")

    # Provider-resource side: docker network + container
    show(
        "> tf_docker_network  instance='rocannon_demo_net'",
        await call(c, "tf_docker_network", {
            "instance": "rc_demo_net", "name": "rocannon_demo_net",
        }),
    )
    await call(c, "tf_docker_image", {
        "instance": "alpine", "name": "alpine:3.20",
    })
    show(
        "> tf_docker_container  instance='rc_demo_worker'",
        await call(c, "tf_docker_container", {
            "instance": "rc_demo_worker",
            "name": "rocannon_demo_worker",
            "image": "${docker_image.alpine.image_id}",
            "command": ["sh", "-c", "echo hello-from-rocannon && sleep 600"],
            "must_run": True,
            "networks_advanced": [{"name": "${docker_network.rc_demo_net.name}"}],
        }),
    )

    # Community-module side: cloudposse/label/null reflected from variables.tf
    # at startup; called here with typed args; returns its declared outputs.
    show(
        "> tf_module_null_label  instance='label_demo'",
        await call(c, "tf_module_null_label", {
            "instance": "label_demo",
            "namespace": "rocannon",
            "stage": "demo",
            "name": "three-cannon",
            "attributes": ["primary"],
            "delimiter": "-",
        }),
        max_chars=600,
    )

    show("> tf_state_list", await call(c, "tf_state_list", {}))


async def section_helm(c: Client) -> None:
    banner("HELM, install nginx chart into the kind cluster")

    show(
        "> helm_install_bitnami_nginx",
        await call(c, "helm_install_bitnami_nginx", {
            "release_name": "rc-demo",
            "namespace": "rocannon-demo",
            "values": {
                "replicaCount": 1,
                "service": {"type": "ClusterIP"},
            },
        }),
        max_chars=400,
    )
    show("> helm_list  namespace='rocannon-demo'",
         await call(c, "helm_list", {"namespace": "rocannon-demo"}),
         max_chars=400)


async def section_cleanup(c: Client) -> None:
    banner("CLEANUP, destroy in reverse order")

    # Helm
    show("> helm_uninstall  rc-demo",
         await call(c, "helm_uninstall", {
             "release_name": "rc-demo", "namespace": "rocannon-demo",
         }))

    # Terraform, includes the module instance
    for addr in ["module.label_demo",
                 "docker_container.rc_demo_worker",
                 "docker_image.alpine",
                 "docker_network.rc_demo_net"]:
        show(f"> tf_destroy  {addr}",
             await call(c, "tf_destroy", {"address": addr}))


async def main() -> None:
    if not WORK.exists():
        sys.exit("Run ./setup.sh first to prepare the demo workspace.")

    config = build_config()
    banner("BOOT, construct multi-cannon MCP server in-process")
    server = create_server(config)
    print("  → 1 FastMCP instance, cannons: ansible + terraform + helm")

    async with Client(server) as c:
        tools = await c.list_tools()
        by_prefix: dict[str, int] = {}
        for t in tools:
            prefix = (
                "ansible.builtin" if t.name.startswith("ansible.") else
                "tf_" if t.name.startswith("tf_") else
                "helm_" if t.name.startswith("helm_") else "meta"
            )
            by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
        print(f"  → {len(tools)} MCP tools registered: {dict(by_prefix)}")

        await section_ansible(c)
        await section_terraform(c)
        await section_helm(c)
        if os.environ.get("ROCANNON_KEEP", "0") != "1":
            await section_cleanup(c)
        else:
            print("\n(Skipping cleanup because ROCANNON_KEEP=1)")


if __name__ == "__main__":
    asyncio.run(main())
