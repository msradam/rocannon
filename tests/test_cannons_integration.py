"""End-to-end integration tests against real infrastructure.

These tests spin up real docker containers, talk to a real Kubernetes cluster
(via kind), and execute real ``tofu``/``helm`` CLIs. They are **opt-in** via
``pytest -m integration``; the default ``pytest`` run skips them.

Prereqs (any missing → auto-skip):
  - docker daemon reachable (Colima socket or /var/run/docker.sock)
  - ``tofu`` or ``terraform`` binary on PATH
  - ``helm`` binary on PATH
  - a kind cluster named ``rocannon-test`` (``kind create cluster --name rocannon-test``)

Each test owns its disposable artifacts and cleans up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml
from fastmcp.client import Client

from rocannon.config import (
    Config,
    HelmChartSpec,
    HelmConfig,
    TerraformConfig,
    TerraformModuleSpec,
)
from rocannon.server import create_server

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# environment probes (cheap; tests use them to auto-skip)
# ---------------------------------------------------------------------------


def _docker_socket() -> str | None:
    """Find a working docker socket, preferring Colima on macOS."""
    candidates = [
        Path.home() / ".colima/default/docker.sock",
        Path("/var/run/docker.sock"),
    ]
    for sock in candidates:
        if sock.exists():
            return f"unix://{sock}"
    return None


def _docker_alive() -> bool:
    return subprocess.run(
        ["docker", "info"], capture_output=True, timeout=5
    ).returncode == 0


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _kind_cluster_up(name: str = "rocannon-test") -> bool:
    proc = subprocess.run(
        ["kind", "get", "clusters"], capture_output=True, text=True, timeout=10
    )
    return proc.returncode == 0 and name in proc.stdout.split()


_skip_no_docker = pytest.mark.skipif(
    not _docker_alive(), reason="docker daemon not reachable",
)
_skip_no_tofu = pytest.mark.skipif(
    not (_have("tofu") or _have("terraform")), reason="tofu/terraform not on PATH",
)
_skip_no_helm = pytest.mark.skipif(
    not _have("helm"), reason="helm not on PATH",
)
_skip_no_kind = pytest.mark.skipif(
    not (_have("kind") and _kind_cluster_up()),
    reason="kind cluster 'rocannon-test' not running",
)


# ---------------------------------------------------------------------------
# shared session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def ssh_key(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Generate an ed25519 key once per session for the UBI9 SSH target."""
    keydir = tmp_path_factory.mktemp("ssh")
    key = keydir / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "rocannon-integ"],
        capture_output=True, check=True,
    )
    return key


@pytest.fixture(scope="session")
def ubi_container(ssh_key: Path) -> Generator[tuple[str, int], None, None]:
    """Build + run a UBI9 SSH container; yield (host, port). Cleaned at session end."""
    if not _docker_alive():
        pytest.skip("docker not reachable")

    name = f"rocannon-integ-ubi-{uuid.uuid4().hex[:8]}"
    image = f"{name}:latest"
    port = 22000 + (hash(name) % 1000)

    build_ctx = ssh_key.parent
    dockerfile = build_ctx / "Dockerfile"
    dockerfile.write_text(
        "FROM redhat/ubi9-minimal\n"
        "RUN microdnf install -y openssh-server openssh-clients python3 "
        "iproute procps-ng iputils && microdnf clean all && ssh-keygen -A "
        "&& mkdir -p /root/.ssh && chmod 700 /root/.ssh\n"
        f"COPY {ssh_key.name}.pub /root/.ssh/authorized_keys\n"
        "RUN chmod 600 /root/.ssh/authorized_keys && "
        "sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' "
        "/etc/ssh/sshd_config\n"
        "EXPOSE 22\nCMD [\"/usr/sbin/sshd\", \"-D\", \"-e\"]\n"
    )
    subprocess.run(
        ["docker", "build", "-t", image, str(build_ctx)],
        capture_output=True, check=True, timeout=180,
    )
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:22", image],
        capture_output=True, check=True, timeout=30,
    )

    # Wait for sshd
    for _ in range(20):
        check = subprocess.run(
            ["ssh", "-i", str(ssh_key), "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-p", str(port),
             "-o", "ConnectTimeout=2", "root@127.0.0.1", "echo ok"],
            capture_output=True, timeout=5,
        )
        if check.returncode == 0:
            break
        time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(f"ssh did not come up on {name}")

    try:
        yield ("127.0.0.1", port)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture
def ansible_inventory(
    tmp_path: Path, ssh_key: Path, ubi_container: tuple[str, int]
) -> Path:
    """Per-test inventory pointing at the session-shared UBI container."""
    host, port = ubi_container
    inv = tmp_path / "hosts.ini"
    inv.write_text(
        "[ubi]\n"
        f"ubi9 ansible_host={host} ansible_port={port} ansible_user=root "
        f"ansible_ssh_private_key_file={ssh_key} "
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null'\n"
    )
    return inv


# ---------------------------------------------------------------------------
# AnsibleCannon
# ---------------------------------------------------------------------------


class TestAnsibleCannonIntegration:
    @_skip_no_docker
    async def test_ping_real_ubi9_target(self, ansible_inventory: Path) -> None:
        cfg = Config(
            inventories=[ansible_inventory],
            modules=["ansible.builtin.ping"],
        )
        server = create_server(cfg)
        async with Client(server) as c:
            r = await c.call_tool("ansible.builtin.ping", {"target": "ubi9"})
            payload = json.loads(r.content[0].text)
            assert payload["status"] == "successful"
            assert payload["result"]["ping"] == "pong"

    @_skip_no_docker
    async def test_command_redacts_passwords(self, ansible_inventory: Path) -> None:
        cfg = Config(
            inventories=[ansible_inventory],
            modules=["ansible.builtin.command"],
        )
        server = create_server(cfg)
        async with Client(server) as c:
            # Echo a fake secret; verify the result has been scrubbed.
            r = await c.call_tool("ansible.builtin.command", {
                "target": "ubi9",
                "cmd": "echo password=hunter2 trailing",
            })
            text = r.content[0].text
            assert "hunter2" not in text, "secret leaked into tool result"
            assert "REDACTED" in text or "password=" not in text


# ---------------------------------------------------------------------------
# TerraformCannon, provider resources
# ---------------------------------------------------------------------------


@_skip_no_tofu
@_skip_no_docker
class TestTerraformCannonResources:
    async def test_create_and_destroy_docker_container(
        self, tmp_path: Path
    ) -> None:
        """Verify a container created via TF is observable in docker, then cleaned up.

        We deliberately don't destroy the image here, image refcounts are a
        host-wide concern; other tests / parallel runs may share it. The
        container has a unique name and is the only side effect we assert on.
        """
        socket = _docker_socket()
        if socket is None:
            pytest.skip("no docker socket")
        name = f"rocannon_integ_{uuid.uuid4().hex[:8]}"

        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"docker": {"source": "kreuzwerker/docker", "version": "~> 3.0"}},
            provider_config={"docker": {"host": socket}},
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            await c.call_tool("tf_docker_image", {
                "instance": "alpine", "name": "alpine:3.20",
            })
            r = await c.call_tool("tf_docker_container", {
                "instance": "ctr",
                "name": name,
                "image": "${docker_image.alpine.image_id}",
                "command": ["sleep", "60"],
                "must_run": True,
            })
            payload = json.loads(r.content[0].text)
            assert payload["ok"]

            proc = subprocess.run(
                ["docker", "ps", "--filter", f"name={name}", "-q"],
                capture_output=True, text=True,
            )
            assert proc.stdout.strip(), f"container {name} not running"

            await c.call_tool("tf_destroy", {"address": "docker_container.ctr"})

            proc = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={name}", "-q"],
                capture_output=True, text=True,
            )
            assert not proc.stdout.strip(), f"container {name} not cleaned up"


# ---------------------------------------------------------------------------
# TerraformCannon, community modules
# ---------------------------------------------------------------------------


@_skip_no_tofu
class TestTerraformCannonModules:
    async def test_cloudposse_label_module(self, tmp_path: Path) -> None:
        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"null": {"source": "hashicorp/null", "version": "~> 3.2"}},
            modules=[TerraformModuleSpec(
                source="cloudposse/label/null", version="0.25.0",
            )],
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            tools = await c.list_tools()
            module_tools = [t.name for t in tools if t.name.startswith("tf_module_")]
            assert "tf_module_null_label" in module_tools

            r = await c.call_tool("tf_module_null_label", {
                "instance": "test",
                "namespace": "rc",
                "stage": "integ",
                "name": "wing",
                "attributes": ["one"],
                "delimiter": "-",
            })
            payload = json.loads(r.content[0].text)
            assert payload["ok"]
            outputs = payload["outputs"]
            assert outputs["id"] == "rc-integ-wing-one"


# ---------------------------------------------------------------------------
# HelmCannon
# ---------------------------------------------------------------------------


@_skip_no_helm
@_skip_no_kind
class TestHelmCannonIntegration:
    async def test_install_and_uninstall_nginx(self) -> None:
        ns = f"rc-helm-{uuid.uuid4().hex[:6]}"
        cfg = Config(helm=HelmConfig(
            charts=[HelmChartSpec(name="bitnami/nginx", version="21.0.6")],
            default_namespace=ns,
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            r = await c.call_tool("helm_install_bitnami_nginx", {
                "release_name": "rc-int",
                "namespace": ns,
                "values": {"replicaCount": 1, "service": {"type": "ClusterIP"}},
            })
            payload = r.data or json.loads(r.content[0].text)
            assert payload["ok"], f"install failed: {payload.get('error')}"
            assert payload["info"]["info"]["status"] == "deployed"

            r = await c.call_tool("helm_list", {"namespace": ns})
            releases = r.data or json.loads(r.content[0].text)
            assert any(rel["name"] == "rc-int" for rel in releases)

            r = await c.call_tool("helm_uninstall", {
                "release_name": "rc-int", "namespace": ns,
            })
            payload = r.data or json.loads(r.content[0].text)
            assert payload["ok"]


# ---------------------------------------------------------------------------
# Cross-cannon save/replay, meta tools work for any cannon's tool calls
# ---------------------------------------------------------------------------


@_skip_no_tofu
class TestCrossCannonSave:
    """commit_session + saved-playbook prompts work across cannons, not just Ansible."""

    async def test_commit_session_captures_tf_module_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TF module call → commit_session writes valid YAML with the right tool name."""
        monkeypatch.setenv("ROCANNON_DATA_DIR", str(tmp_path / "data"))
        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"null": {"source": "hashicorp/null", "version": "~> 3.2"}},
            modules=[TerraformModuleSpec(
                source="cloudposse/label/null", version="0.25.0",
            )],
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            await c.call_tool("tf_module_null_label", {
                "instance": "saved",
                "namespace": "x", "stage": "y", "name": "z",
                "delimiter": "-",
            })
            r = await c.call_tool("commit_session", {
                "name": "tf_saved", "description": "captured TF module call",
            })
            payload = r.data or json.loads(r.content[0].text)
            assert payload["ok"], f"commit_session failed: {payload.get('error')}"

        saved_path = tmp_path / "data" / ".rocannon" / "playbooks" / "tf_saved.yml"
        assert saved_path.exists()
        pb = yaml.safe_load(saved_path.read_text())
        assert pb["name"] == "tf_saved"
        # Generic {tool, args} shape, not Ansible-specific {module, target, args}
        steps = pb["steps"]
        assert len(steps) == 1
        assert steps[0]["tool"] == "tf_module_null_label"
        assert "module" not in steps[0]  # old shape should NOT be present
        assert steps[0]["args"]["instance"] == "saved"

    async def test_saved_tf_playbook_loads_as_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After save, a fresh server should expose the playbook as an MCP prompt."""
        monkeypatch.setenv("ROCANNON_DATA_DIR", str(tmp_path / "data"))

        # Pre-write a playbook YAML in the new shape directly
        pb_dir = tmp_path / "data" / ".rocannon" / "playbooks"
        pb_dir.mkdir(parents=True)
        (pb_dir / "tf_replay.yml").write_text(
            "name: tf_replay\n"
            "description: replay terraform module call\n"
            "steps:\n"
            "  - tool: tf_module_null_label\n"
            "    args:\n"
            "      instance: replayed\n"
            "      namespace: rc\n"
            "      stage: test\n"
            "      name: thing\n"
        )

        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"null": {"source": "hashicorp/null", "version": "~> 3.2"}},
            modules=[TerraformModuleSpec(
                source="cloudposse/label/null", version="0.25.0",
            )],
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            prompts = await c.list_prompts()
            names = {p.name for p in prompts}
            assert "playbook_tf_replay" in names, \
                f"saved TF playbook not loaded as prompt: {names}"

            rendered = await c.get_prompt("playbook_tf_replay")
            body = rendered.messages[0].content.text
            assert "tf_module_null_label" in body
            assert "instance" in body and "replayed" in body


# ---------------------------------------------------------------------------
# Behavioral correctness, tool typed args produce the real-world effect
# ---------------------------------------------------------------------------


@_skip_no_tofu
@_skip_no_docker
class TestTerraformResourceBehavior:
    """Beyond 'it ran', does the typed arg shape actually reach the provider?"""

    async def test_nested_ports_block_produces_real_port_mapping(
        self, tmp_path: Path
    ) -> None:
        """docker_container.ports (a nested block) should map a real port on the host."""
        socket = _docker_socket()
        if socket is None:
            pytest.skip("no docker socket")
        # Random high port to avoid collisions with whatever else is running.
        host_port = 28000 + (hash(tmp_path.name) % 1000)
        name = f"rocannon_ports_{uuid.uuid4().hex[:8]}"

        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"docker": {"source": "kreuzwerker/docker", "version": "~> 3.0"}},
            provider_config={"docker": {"host": socket}},
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            await c.call_tool("tf_docker_image", {
                "instance": "nginx", "name": "nginx:alpine",
            })
            r = await c.call_tool("tf_docker_container", {
                "instance": "web",
                "name": name,
                "image": "${docker_image.nginx.image_id}",
                "must_run": True,
                # The nested ports block, list of objects passed opaquely.
                "ports": [
                    {"internal": 80, "external": host_port, "protocol": "tcp"},
                ],
            })
            payload = json.loads(r.content[0].text)
            assert payload["ok"]

            # Probe: docker port mapping should reflect what we asked for.
            proc = subprocess.run(
                ["docker", "port", name, "80/tcp"],
                capture_output=True, text=True, timeout=10,
            )
            assert proc.returncode == 0, proc.stderr
            assert f":{host_port}" in proc.stdout, \
                f"expected host port {host_port} in mapping; got {proc.stdout!r}"

            for addr in ("docker_container.web", "docker_image.nginx"):
                await c.call_tool("tf_destroy", {"address": addr})

    async def test_noop_apply_reports_no_changes(self, tmp_path: Path) -> None:
        """Applying a resource twice with identical args → second plan: no changes."""
        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"null": {"source": "hashicorp/null", "version": "~> 3.2"}},
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            args = {"instance": "x", "triggers": {"a": "1"}}
            first = json.loads(
                (await c.call_tool("tf_null_resource", args)).content[0].text
            )
            second = json.loads(
                (await c.call_tool("tf_null_resource", args)).content[0].text
            )
            assert first["ok"] and second["ok"]
            assert "no changes" in second["plan_summary"].lower()
            await c.call_tool("tf_destroy", {"address": "null_resource.x"})

    async def test_destroy_cascades_dependents(self, tmp_path: Path) -> None:
        """Destroying an image that a container depends on should cascade.

        Uses a uniquely-tagged image so this test has exclusive ownership at
        the docker level, other concurrent runs can't conflict on shared tags.
        """
        socket = _docker_socket()
        if socket is None:
            pytest.skip("no docker socket")
        ctr_name = f"rocannon_cascade_{uuid.uuid4().hex[:8]}"
        # Unique image tag means destroy only affects our test
        unique_tag = f"localhost/rocannon-test:{uuid.uuid4().hex[:8]}"
        subprocess.run(
            ["docker", "tag", "alpine:3.20", unique_tag],
            capture_output=True, check=True, timeout=10,
        )
        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"docker": {"source": "kreuzwerker/docker", "version": "~> 3.0"}},
            provider_config={"docker": {"host": socket}},
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            await c.call_tool("tf_docker_image", {
                "instance": "img", "name": unique_tag,
            })
            await c.call_tool("tf_docker_container", {
                "instance": "dep",
                "name": ctr_name,
                "image": "${docker_image.img.image_id}",
                "command": ["sleep", "60"],
                "must_run": True,
            })
            # Destroying the image should also destroy the dependent container.
            await c.call_tool("tf_destroy", {"address": "docker_image.img"})

            state = await c.call_tool("tf_state_list", {})
            # When state is empty, FastMCP returns no content, treat as [].
            if state.data is not None:
                state_list = state.data
            elif state.content:
                state_list = json.loads(state.content[0].text)
            else:
                state_list = []
            assert "docker_container.dep" not in state_list, \
                "cascade destroy did not remove the dependent container"
            assert "docker_image.img" not in state_list, \
                "primary resource not removed"

            proc = subprocess.run(
                ["docker", "ps", "-a", "--filter", f"name={ctr_name}", "-q"],
                capture_output=True, text=True,
            )
            assert not proc.stdout.strip(), "container should have been destroyed"


@_skip_no_helm
@_skip_no_kind
class TestHelmCannonBehavior:
    async def test_values_override_actually_takes_effect(self) -> None:
        """replicaCount=2 in values should produce a 2-replica deployment."""
        ns = f"rc-helm-vals-{uuid.uuid4().hex[:6]}"
        release = "rc-vals"
        cfg = Config(helm=HelmConfig(
            charts=[HelmChartSpec(name="bitnami/nginx", version="21.0.6")],
            default_namespace=ns,
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            r = await c.call_tool("helm_install_bitnami_nginx", {
                "release_name": release,
                "namespace": ns,
                "values": {"replicaCount": 2, "service": {"type": "ClusterIP"}},
            })
            payload = r.data or json.loads(r.content[0].text)
            assert payload["ok"], payload.get("error")

            # kubectl is the ground truth. Bitnami's nginx chart names the
            # Deployment ``<release>-nginx``. Find any deploy with .spec.replicas=2
            # in the namespace; that's our chart-installed one.
            proc = subprocess.run(
                ["kubectl", "get", "deploy", "-n", ns,
                 "-o", "jsonpath={range .items[*]}{.metadata.name}={.spec.replicas} {end}"],
                capture_output=True, text=True, timeout=30,
            )
            assert proc.returncode == 0, proc.stderr
            entries = dict(
                kv.split("=", 1) for kv in proc.stdout.split() if "=" in kv
            )
            assert entries, f"no deployments found in ns {ns}: {proc.stdout!r}"
            assert "2" in entries.values(), (
                f"replicaCount override did not apply, saw {entries}"
            )

            await c.call_tool("helm_uninstall", {
                "release_name": release, "namespace": ns,
            })


# ---------------------------------------------------------------------------
# Schema fidelity, what we register matches the upstream catalog
# ---------------------------------------------------------------------------


class TestSchemaFidelity:
    """Catches drift between our registered tools and the upstream truth.

    Marked integration because they shell out to ansible-doc / tofu / helm,
    but they don't touch docker, kind, or any real targets.
    """

    @pytest.mark.skipif(not _have("ansible-doc"), reason="ansible-doc not on PATH")
    async def test_ansible_ping_schema_matches_ansible_doc(self) -> None:
        """ansible.builtin.ping → tool inputSchema should reflect ansible-doc truth."""
        cfg = Config(
            inventories=[self._dummy_inventory()],
            modules=["ansible.builtin.ping"],
        )
        server = create_server(cfg)
        async with Client(server) as c:
            tool = next(
                t for t in await c.list_tools() if t.name == "ansible.builtin.ping"
            )
            props = tool.inputSchema.get("properties", {})
            required = set(tool.inputSchema.get("required", []))
            # ansible.builtin.ping has one optional param: data
            assert "data" in props
            assert "data" not in required
            assert "target" in props
            assert "target" in required

    @pytest.mark.skipif(
        not (_have("tofu") or _have("terraform")),
        reason="tofu/terraform not on PATH",
    )
    async def test_tf_docker_container_required_matches_provider(
        self, tmp_path: Path
    ) -> None:
        """docker_container's required attrs in our tool == provider schema's required."""
        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"docker": {"source": "kreuzwerker/docker", "version": "~> 3.0"}},
        ))
        server = create_server(cfg)

        # Ground truth from tofu providers schema
        proc = subprocess.run(
            ["tofu", f"-chdir={tmp_path / 'tf'}", "providers", "schema", "-json"],
            capture_output=True, text=True, timeout=60,
        )
        upstream = json.loads(proc.stdout)
        pkey = next(iter(upstream["provider_schemas"]))
        upstream_required = {
            attr_name
            for attr_name, info in upstream["provider_schemas"][pkey]
                ["resource_schemas"]["docker_container"]["block"]["attributes"].items()
            if info.get("required")
        }

        async with Client(server) as c:
            tool = next(
                t for t in await c.list_tools() if t.name == "tf_docker_container"
            )
            registered_required = set(tool.inputSchema.get("required", []))
            # Our reserved slot adds 'instance'; subtract before comparison.
            assert registered_required - {"instance"} == upstream_required, \
                f"required drift: registered={registered_required} upstream={upstream_required}"

    @pytest.mark.skipif(
        not (_have("tofu") or _have("terraform")),
        reason="tofu/terraform not on PATH",
    )
    async def test_tf_module_variables_count_matches_variables_tf(
        self, tmp_path: Path
    ) -> None:
        """tf_module_null_label should expose every variable declared in the module."""
        import hcl2
        cfg = Config(terraform=TerraformConfig(
            workspace=tmp_path / "tf",
            providers={"null": {"source": "hashicorp/null", "version": "~> 3.2"}},
            modules=[TerraformModuleSpec(
                source="cloudposse/label/null", version="0.25.0",
            )],
        ))
        server = create_server(cfg)

        # Ground truth: parse variables.tf directly
        mod_dir = tmp_path / "tf" / ".terraform" / "modules" / "null_label_reflect"
        upstream_var_names: set[str] = set()
        for vf in mod_dir.glob("variables*.tf"):
            with vf.open() as f:
                parsed = hcl2.load(f)
            for block in parsed.get("variable", []):
                for raw_name in block:
                    upstream_var_names.add(raw_name.strip('"'))

        async with Client(server) as c:
            tool = next(
                t for t in await c.list_tools() if t.name == "tf_module_null_label"
            )
            registered = set(tool.inputSchema.get("properties", {}).keys())
            registered.discard("instance")  # our reserved slot
            assert registered == upstream_var_names, (
                f"variable drift:\n"
                f"  registered only: {registered - upstream_var_names}\n"
                f"  upstream only:   {upstream_var_names - registered}"
            )

    @pytest.mark.skipif(not _have("helm"), reason="helm not on PATH")
    async def test_helm_chart_description_in_tool_metadata(self) -> None:
        """helm_install_<chart> tool description should include the chart description."""
        cfg = Config(helm=HelmConfig(
            charts=[HelmChartSpec(name="bitnami/nginx", version="21.0.6")],
        ))
        server = create_server(cfg)
        async with Client(server) as c:
            tool = next(
                t for t in await c.list_tools()
                if t.name == "helm_install_bitnami_nginx"
            )
            # Truth: helm show chart returns YAML with a description field
            proc = subprocess.run(
                ["helm", "show", "chart", "bitnami/nginx", "--version", "21.0.6"],
                capture_output=True, text=True, timeout=60,
            )
            chart_meta = yaml.safe_load(proc.stdout)
            upstream_desc = chart_meta.get("description", "").strip()
            if upstream_desc:
                # Tool description should reference it (we embed it).
                assert upstream_desc[:30] in tool.description, \
                    "chart description not propagated to tool"

    @staticmethod
    def _dummy_inventory() -> Path:
        """Minimal in-memory inventory for cases where we just need schema, not execution."""
        f = Path(tempfile.mkstemp(suffix=".ini")[1])
        f.write_text("[local]\nlocalhost ansible_connection=local\n")
        return f
