"""Integration tests for roles-as-tools against real ansible-doc + ansible-runner.

No docker; these create role fixtures and run them on localhost. Opt-in via
``pytest -m integration``; auto-skip if ansible-doc is not on PATH.
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

from rocannon.config import Config
from rocannon.server import create_server

pytestmark = pytest.mark.integration

_skip_no_ansible = pytest.mark.skipif(
    shutil.which("ansible-doc") is None or shutil.which("ansible-playbook") is None,
    reason="ansible-doc / ansible-playbook not available",
)

_LOCAL_INV = (
    "localhost ansible_connection=local "
    'ansible_python_interpreter="{{ ansible_playbook_python }}"\n'
)

_COPY_TASK = '- ansible.builtin.copy:\n    content: "{{ %s }}\\n"\n    dest: "{{ dest }}"\n'


def _standalone_role(root: Path) -> Path:
    role = root / "roles" / "noter"
    (role / "tasks").mkdir(parents=True)
    (role / "meta").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text(_COPY_TASK % "message")
    (role / "meta" / "argument_specs.yml").write_text(
        "argument_specs:\n"
        "  main:\n"
        "    short_description: Write a note\n"
        "    options:\n"
        "      dest: { type: path, required: true }\n"
        "      message: { type: str, default: hi }\n"
    )
    return root / "roles"


def _collection_role(root: Path) -> Path:
    base = root / "collections" / "ansible_collections" / "demo" / "ops"
    role = base / "roles" / "deploy"
    (role / "tasks").mkdir(parents=True)
    (role / "meta").mkdir(parents=True)
    base.joinpath("galaxy.yml").write_text(
        "namespace: demo\nname: ops\nversion: 1.0.0\nreadme: README.md\nauthors: [t]\n"
    )
    role.joinpath("tasks", "main.yml").write_text(_COPY_TASK % "app")
    role.joinpath("meta", "argument_specs.yml").write_text(
        "argument_specs:\n"
        "  main:\n"
        "    short_description: Deploy marker\n"
        "    options:\n"
        "      app: { type: str, required: true }\n"
        "      dest: { type: path, required: true }\n"
    )
    return root / "collections"


async def _call(server: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    from fastmcp.client import Client

    async with Client(server) as client:
        assert name in {t.name for t in await client.list_tools()}
        result = await client.call_tool(name, args)
        return result.structured_content


@_skip_no_ansible
async def test_standalone_role_via_roles_path(tmp_path: Path) -> None:
    inv = tmp_path / "hosts"
    inv.write_text(_LOCAL_INV)
    roles_path = _standalone_role(tmp_path)
    out = tmp_path / "note.txt"
    server = create_server(Config(inventories=[inv], roles=["noter"], roles_path=roles_path))
    res = await _call(
        server, "noter", {"target": "localhost", "dest": str(out), "message": "Managed by Rocannon"}
    )
    assert res["status"] == "successful"
    assert out.read_text().strip() == "Managed by Rocannon"


@_skip_no_ansible
async def test_collection_role_via_fqcn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inv = tmp_path / "hosts"
    inv.write_text(_LOCAL_INV)
    coll_path = _collection_role(tmp_path)
    monkeypatch.setenv("ANSIBLE_COLLECTIONS_PATH", str(coll_path))
    out = tmp_path / "marker.txt"
    server = create_server(Config(inventories=[inv], roles=["demo.ops.deploy"]))
    res = await _call(
        server, "demo.ops.deploy", {"target": "localhost", "app": "web", "dest": str(out)}
    )
    assert res["status"] == "successful"
    assert out.read_text().strip() == "web"


@_skip_no_ansible
async def test_role_missing_required_arg_is_rejected(tmp_path: Path) -> None:
    from fastmcp.exceptions import ToolError

    inv = tmp_path / "hosts"
    inv.write_text(_LOCAL_INV)
    roles_path = _standalone_role(tmp_path)
    server = create_server(Config(inventories=[inv], roles=["noter"], roles_path=roles_path))
    from fastmcp.client import Client

    async with Client(server) as client:
        with pytest.raises(ToolError):
            # `dest` is required; omitting it must be rejected, not silently run.
            await client.call_tool("noter", {"target": "localhost", "message": "x"})
