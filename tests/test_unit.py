"""Unit tests for Rocannon core modules.

Tests config, schema, inventory, and executor in isolation using mocked
subprocess and ansible-runner calls. No containers, Ollama, or network required.
"""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from rocannon.config import Config, load_profile
from rocannon.executor import _parse_runner_result, run_module
from rocannon.inventory import load_inventory
from rocannon.schema import ANSIBLE_TYPE_MAP, expand_modules, fetch_module_schema

# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


class TestConfig:
    def test_valid_config(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        config = Config(inventories=[inv], modules=["ansible.builtin.ping"])
        assert len(config.inventories) == 1
        assert config.inventories[0].is_absolute()

    def test_missing_inventory_raises(self) -> None:
        with pytest.raises(ValueError, match="Inventory file not found"):
            Config(inventories=[Path("/nonexistent/inv.yml")], modules=["ansible.builtin.ping"])

    def test_empty_modules_raises(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        with pytest.raises(ValueError, match="At least one module"):
            Config(inventories=[inv], modules=[])

    def test_empty_inventories_raises(self) -> None:
        with pytest.raises(ValueError, match="At least one inventory"):
            Config(inventories=[], modules=["ansible.builtin.ping"])

    def test_load_profile(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        profile = tmp_path / "profile.yml"
        profile.write_text(f"inventories:\n  - {inv}\nmodules:\n  - ansible.builtin.ping\n")
        config = load_profile(profile)
        assert config.transport == "stdio"
        assert config.modules == ["ansible.builtin.ping"]

    def test_load_profile_transport_override(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        profile = tmp_path / "profile.yml"
        profile.write_text(f"inventories:\n  - {inv}\nmodules:\n  - ansible.builtin\n")
        config = load_profile(profile, transport="http")
        assert config.transport == "http"

    def test_inventories_resolved_to_absolute(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        config = Config(inventories=[inv], modules=["ansible.builtin.ping"])
        assert all(p.is_absolute() for p in config.inventories)


# ---------------------------------------------------------------------------
# inventory.py
# ---------------------------------------------------------------------------


SAMPLE_INVENTORY_JSON = json.dumps(
    {
        "_meta": {
            "hostvars": {
                "host1": {"ansible_host": "1.2.3.4"},
                "host2": {"ansible_host": "1.2.3.5"},
            }
        },
        "all": {"hosts": ["host1", "host2"], "children": ["linuxone", "ungrouped"]},
        "linuxone": {"hosts": ["host1", "host2"]},
        "ungrouped": {"hosts": []},
    }
)


class TestLoadInventory:
    def test_returns_hosts_and_groups(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = SAMPLE_INVENTORY_JSON
        with patch("rocannon.inventory.subprocess.run", return_value=completed):
            result = load_inventory([Path("/inv.yml")])
        assert result["hosts"] == ["host1", "host2"]
        assert result["groups"] == ["linuxone"]

    def test_filters_meta_all_ungrouped(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = SAMPLE_INVENTORY_JSON
        with patch("rocannon.inventory.subprocess.run", return_value=completed):
            result = load_inventory([Path("/inv.yml")])
        assert "all" not in result["groups"]
        assert "ungrouped" not in result["groups"]
        assert "_meta" not in result["groups"]

    def test_subprocess_failure_returns_empty(self) -> None:
        completed = MagicMock()
        completed.returncode = 1
        completed.stderr = "No such file"
        with patch("rocannon.inventory.subprocess.run", return_value=completed):
            result = load_inventory([Path("/inv.yml")])
        assert result == {"hosts": [], "groups": []}

    def test_invalid_json_returns_empty(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "not json"
        with patch("rocannon.inventory.subprocess.run", return_value=completed):
            result = load_inventory([Path("/inv.yml")])
        assert result == {"hosts": [], "groups": []}

    def test_empty_inventory_returns_empty(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps({"_meta": {"hostvars": {}}, "all": {"hosts": []}})
        with patch("rocannon.inventory.subprocess.run", return_value=completed):
            result = load_inventory([Path("/inv.yml")])
        assert result["hosts"] == []
        assert result["groups"] == []

    def test_multiple_inventory_files_passed_as_args(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = SAMPLE_INVENTORY_JSON
        with patch("rocannon.inventory.subprocess.run", return_value=completed) as mock_run:
            load_inventory([Path("/a.yml"), Path("/b.yml")])
        cmd = mock_run.call_args[0][0]
        assert cmd.count("-i") == 2


# ---------------------------------------------------------------------------
# schema.py
# ---------------------------------------------------------------------------


SAMPLE_ANSIBLE_DOC = {
    "ansible.builtin.ping": {
        "doc": {
            "short_description": "Try to connect to host, verify a usable python and return pong",
            "options": {
                "data": {
                    "description": ["Data to return for the ping"],
                    "type": "str",
                    "default": "pong",
                    "required": False,
                }
            },
        }
    }
}

SAMPLE_MODULE_LIST = {
    "ansible.builtin.ping": "Ping module",
    "ansible.builtin.copy": "Copy files",
    "ibm.ibm_zos_core.zos_ping": "z/OS ping",
}


class TestExpandModules:
    def test_fqcn_returned_as_is(self) -> None:
        result = expand_modules(["ansible.builtin.ping", "ansible.builtin.copy"])
        assert set(result) == {"ansible.builtin.ping", "ansible.builtin.copy"}

    def test_prefix_expanded(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(SAMPLE_MODULE_LIST)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            result = expand_modules(["ansible.builtin"])
        assert "ansible.builtin.ping" in result
        assert "ansible.builtin.copy" in result
        assert "ibm.ibm_zos_core.zos_ping" not in result

    def test_subprocess_failure_returns_explicit_only(self) -> None:
        with patch(
            "rocannon.schema.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "ansible-doc"),
        ):
            result = expand_modules(["ansible.builtin", "ansible.builtin.ping"])
        assert result == ["ansible.builtin.ping"]

    def test_no_prefixes_skips_subprocess(self) -> None:
        with patch("rocannon.schema.subprocess.run") as mock_run:
            result = expand_modules(["ansible.builtin.ping"])
        mock_run.assert_not_called()
        assert result == ["ansible.builtin.ping"]

    def test_deduplicates_results(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps({"ansible.builtin.ping": "Ping"})
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            result = expand_modules(["ansible.builtin", "ansible.builtin.ping"])
        assert result.count("ansible.builtin.ping") == 1


class TestFetchModuleSchema:
    def test_parses_valid_doc(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(SAMPLE_ANSIBLE_DOC)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("ansible.builtin.ping")
        assert schema["name"] == "ansible.builtin.ping"
        assert "pong" in schema["description"].lower()
        assert len(schema["parameters"]) == 1
        assert schema["parameters"][0]["name"] == "data"

    def test_subprocess_failure_returns_stub(self) -> None:
        completed = MagicMock()
        completed.returncode = 1
        completed.stderr = "error"
        completed.stdout = ""
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("bad.module.name")
        assert schema["parameters"] == []
        assert schema["name"] == "bad.module.name"

    def test_invalid_json_returns_stub(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "not json"
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("ansible.builtin.ping")
        assert schema["parameters"] == []

    def test_module_not_in_doc_returns_stub(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps({"some.other.module": {}})
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("ansible.builtin.ping")
        assert schema["parameters"] == []

    def test_required_parameter_flagged(self) -> None:
        doc = {
            "ansible.builtin.copy": {
                "doc": {
                    "short_description": "Copy files",
                    "options": {
                        "src": {"description": "Source path", "type": "str", "required": True},
                        "dest": {"description": "Dest path", "type": "str", "required": True},
                        "mode": {"description": "File mode", "type": "str", "required": False},
                    },
                }
            }
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("ansible.builtin.copy")
        params = {p["name"]: p for p in schema["parameters"]}
        assert params["src"]["required"] is True
        assert params["dest"]["required"] is True
        assert params["mode"]["required"] is False

    def test_choices_preserved(self) -> None:
        doc = {
            "ansible.builtin.file": {
                "doc": {
                    "short_description": "Manage files",
                    "options": {
                        "state": {
                            "description": "File state",
                            "type": "str",
                            "choices": ["file", "directory", "absent", "touch", "link"],
                            "required": False,
                        }
                    },
                }
            }
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("ansible.builtin.file")
        state_param = schema["parameters"][0]
        assert state_param["choices"] == ["file", "directory", "absent", "touch", "link"]

    def test_type_map_coverage(self) -> None:
        for ansible_type in ["str", "int", "bool", "list", "dict", "path", "raw"]:
            assert ansible_type in ANSIBLE_TYPE_MAP


# ---------------------------------------------------------------------------
# executor.py — _parse_runner_result
# ---------------------------------------------------------------------------


def _make_runner(
    status: str = "successful",
    events: list[dict[str, Any]] | None = None,
    stdout_text: str = "",
    stderr_text: str = "",
) -> Any:
    runner = MagicMock()
    runner.status = status
    runner.events = events or []
    runner.stdout = MagicMock()
    runner.stdout.read.return_value = stdout_text
    runner.stderr = MagicMock()
    runner.stderr.read.return_value = stderr_text
    return runner


def _host_event(host: str, changed: bool = False, rc: int = 0) -> dict[str, Any]:
    return {
        "event_data": {
            "host": host,
            "res": {
                "changed": changed,
                "rc": rc,
                "stdout": f"output from {host}",
                "stderr": "",
            },
        }
    }


class TestParseRunnerResult:
    def test_single_host_flattened(self) -> None:
        runner = _make_runner(events=[_host_event("host1")])
        result = _parse_runner_result(runner)
        assert result["status"] == "successful"
        assert "hosts" not in result
        assert result["changed"] is False

    def test_multi_host_aggregated(self) -> None:
        runner = _make_runner(events=[_host_event("host1"), _host_event("host2", changed=True)])
        result = _parse_runner_result(runner)
        assert "hosts" in result
        assert result["changed"] is True
        assert "host1" in result["hosts"]
        assert "host2" in result["hosts"]

    def test_no_events_returns_stdout(self) -> None:
        runner = _make_runner(status="failed", stdout_text="some output", events=[])
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"
        assert result["stdout"] == "some output"
        assert result["changed"] is False

    def test_events_without_res_skipped(self) -> None:
        runner = _make_runner(
            events=[
                {"event_data": {"host": "host1"}},  # no res
                _host_event("host2"),
            ]
        )
        result = _parse_runner_result(runner)
        assert "hosts" not in result
        assert result["status"] == "successful"

    def test_changed_true_if_any_host_changed(self) -> None:
        runner = _make_runner(
            events=[
                _host_event("host1", changed=False),
                _host_event("host2", changed=True),
                _host_event("host3", changed=False),
            ]
        )
        result = _parse_runner_result(runner)
        assert result["changed"] is True


class TestRunModule:
    def test_passes_timeout_to_runner(self, tmp_path: Path) -> None:
        runner = _make_runner(events=[_host_event("localhost")])
        with patch("rocannon.executor.ansible_runner.run", return_value=runner) as mock_run:
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
                timeout=42,
                idle_timeout=10,
            )
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 42
        assert call_kwargs["settings"]["idle_timeout"] == 10

    def test_runner_exception_returns_error_dict(self, tmp_path: Path) -> None:
        with patch("rocannon.executor.ansible_runner.run", side_effect=RuntimeError("boom")):
            result = run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
            )
        assert result["status"] == "error"
        assert "boom" in result["stderr"]

    def test_relative_inventory_resolved_to_absolute(self, tmp_path: Path) -> None:
        runner = _make_runner(events=[_host_event("localhost")])
        with patch("rocannon.executor.ansible_runner.run", return_value=runner) as mock_run:
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=["relative/path.yml"],
                host_pattern="localhost",
            )
        call_kwargs = mock_run.call_args[1]
        passed_inv = call_kwargs["inventory"]
        assert all(Path(p).is_absolute() for p in passed_inv)

    def test_tempfile_cleaned_up_after_run(self, tmp_path: Path) -> None:
        runner = _make_runner(events=[_host_event("localhost")])
        captured: list[str] = []

        def fake_run(**kwargs: Any) -> Any:
            captured.append(kwargs["playbook"])
            return runner

        with patch("rocannon.executor.ansible_runner.run", side_effect=fake_run):
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
            )

        assert captured
        assert not Path(captured[0]).exists()

    def test_tempfile_cleaned_up_on_exception(self, tmp_path: Path) -> None:
        captured: list[str] = []

        def fake_run(**kwargs: Any) -> Any:
            captured.append(kwargs["playbook"])
            raise RuntimeError("forced error")

        with patch("rocannon.executor.ansible_runner.run", side_effect=fake_run):
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
            )

        assert captured
        assert not Path(captured[0]).exists()
