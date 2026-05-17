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
from rocannon.correlation import (
    CorrelationFormatter,
    get_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from rocannon.executor import (
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_TIMEOUT,
    _parse_runner_result,
    build_envvars,
    resolve_idle_timeout,
    resolve_timeout,
    run_module,
)
from rocannon.inventory import load_inventory
from rocannon.redaction import REDACTED, redact, redact_text
from rocannon.schema import (
    ANSIBLE_TYPE_MAP,
    SchemaFetchError,
    expand_modules,
    fetch_module_schema,
)

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

    def test_partial_ansible_modules_only_raises(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        with pytest.raises(ValueError, match="Partial Ansible config"):
            Config(inventories=[inv], modules=[])

    def test_partial_ansible_inventories_only_raises(self) -> None:
        with pytest.raises(ValueError, match="Partial Ansible config"):
            Config(inventories=[], modules=["ansible.builtin.ping"])

    def test_no_cannon_configured_raises(self) -> None:
        with pytest.raises(ValueError, match="No cannon configured"):
            Config()

    def test_load_profile(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        profile = tmp_path / "profile.yml"
        profile.write_text(f"inventories:\n  - {inv}\nmodules:\n  - ansible.builtin.ping\n")
        config = load_profile(profile)
        assert config.transport == "stdio"
        assert config.modules == ["ansible.builtin.ping"]

    def test_ansible_cfg_must_exist(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    h:\n")
        with pytest.raises(ValueError, match="file not found"):
            Config(
                inventories=[inv],
                modules=["ansible.builtin.ping"],
                ansible_cfg=Path("/nonexistent/ansible.cfg"),
            )

    def test_ansible_cfg_resolves_to_absolute(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    h:\n")
        cfg_file = tmp_path / "ansible.cfg"
        cfg_file.write_text("[defaults]\n")
        config = Config(
            inventories=[inv],
            modules=["ansible.builtin.ping"],
            ansible_cfg=cfg_file,
            vault_password_file=cfg_file,
        )
        assert config.ansible_cfg is not None and config.ansible_cfg.is_absolute()
        assert config.vault_password_file is not None and config.vault_password_file.is_absolute()

    def test_per_module_timeouts(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        config = Config(
            inventories=[inv],
            modules=["ansible.builtin.copy"],
            timeouts={"ansible.builtin.copy": 1800},
        )
        assert config.timeouts["ansible.builtin.copy"] == 1800

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

    def test_subprocess_failure_raises(self) -> None:
        completed = MagicMock()
        completed.returncode = 1
        completed.stderr = "error"
        completed.stdout = ""
        with (
            patch("rocannon.schema.subprocess.run", return_value=completed),
            pytest.raises(SchemaFetchError, match="ansible-doc failed"),
        ):
            fetch_module_schema("bad.module.name")

    def test_invalid_json_raises(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "not json"
        with (
            patch("rocannon.schema.subprocess.run", return_value=completed),
            pytest.raises(SchemaFetchError, match="Failed to parse"),
        ):
            fetch_module_schema("ansible.builtin.ping")

    def test_module_not_in_doc_raises(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps({"some.other.module": {}})
        with (
            patch("rocannon.schema.subprocess.run", return_value=completed),
            pytest.raises(SchemaFetchError, match="not present"),
        ):
            fetch_module_schema("ansible.builtin.ping")

    def test_empty_stdout_raises(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "   "
        with (
            patch("rocannon.schema.subprocess.run", return_value=completed),
            pytest.raises(SchemaFetchError, match="empty output"),
        ):
            fetch_module_schema("ansible.builtin.ping")

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
# executor.py, _parse_runner_result
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

    def test_multi_host_status_failed_when_any_rc_nonzero(self) -> None:
        runner = _make_runner(
            events=[_host_event("h1", rc=0), _host_event("h2", rc=2)],
            status="successful",
        )
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"

    def test_multi_host_status_failed_on_failed_flag(self) -> None:
        events = [
            _host_event("h1"),
            {"event_data": {"host": "h2", "res": {"failed": True, "msg": "boom"}}},
        ]
        runner = _make_runner(events=events, status="successful")
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"

    def test_multi_host_status_failed_on_unreachable(self) -> None:
        events = [
            _host_event("h1"),
            {"event_data": {"host": "h2", "res": {"unreachable": True, "msg": "ssh err"}}},
        ]
        runner = _make_runner(events=events, status="successful")
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"

    def test_single_host_status_failed_when_rc_nonzero(self) -> None:
        runner = _make_runner(events=[_host_event("h1", rc=1)], status="successful")
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"

    def test_multi_host_status_preserved_when_all_ok(self) -> None:
        runner = _make_runner(events=[_host_event("h1"), _host_event("h2")], status="successful")
        result = _parse_runner_result(runner)
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

    def test_envvars_passed_to_runner(self, tmp_path: Path) -> None:
        runner = _make_runner(events=[_host_event("h1")])
        with patch("rocannon.executor.ansible_runner.run", return_value=runner) as mock_run:
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="h1",
                envvars={"ANSIBLE_VAULT_PASSWORD_FILE": "/x"},
            )
        assert mock_run.call_args[1]["envvars"] == {"ANSIBLE_VAULT_PASSWORD_FILE": "/x"}

    def test_env_timeout_used_when_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROCANNON_TIMEOUT", "42")
        monkeypatch.setenv("ROCANNON_IDLE_TIMEOUT", "7")
        runner = _make_runner(events=[_host_event("localhost")])
        with patch("rocannon.executor.ansible_runner.run", return_value=runner) as mock_run:
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
            )
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 42
        assert call_kwargs["settings"]["idle_timeout"] == 7

    def test_explicit_timeout_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ROCANNON_TIMEOUT", "42")
        runner = _make_runner(events=[_host_event("localhost")])
        with patch("rocannon.executor.ansible_runner.run", return_value=runner) as mock_run:
            run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
                timeout=999,
            )
        assert mock_run.call_args[1]["timeout"] == 999

    def test_invalid_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_TIMEOUT", "not-an-int")
        monkeypatch.setenv("ROCANNON_IDLE_TIMEOUT", "")
        assert resolve_timeout() == DEFAULT_TIMEOUT
        assert resolve_idle_timeout() == DEFAULT_IDLE_TIMEOUT


class TestBuildEnvvars:
    def test_inherits_ansible_and_zoau_prefixes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANSIBLE_BECOME_PASS", "secret")
        monkeypatch.setenv("ZOAU_HOME", "/usr/lpp/IBM/zoautil")
        monkeypatch.setenv("HOME", "/Users/amsrahman")  # should NOT be inherited
        env = build_envvars()
        assert env["ANSIBLE_BECOME_PASS"] == "secret"
        assert env["ZOAU_HOME"] == "/usr/lpp/IBM/zoautil"
        assert "HOME" not in env

    def test_ansible_cfg_and_vault_set_canonical_names(self) -> None:
        env = build_envvars(
            ansible_cfg=Path("/etc/ansible.cfg"),
            vault_password_file=Path("/root/.vault_pass"),
        )
        assert env["ANSIBLE_CONFIG"] == "/etc/ansible.cfg"
        assert env["ANSIBLE_VAULT_PASSWORD_FILE"] == "/root/.vault_pass"

    def test_extra_envvars_override_profile_fields(self) -> None:
        env = build_envvars(
            extra_envvars={"ANSIBLE_CONFIG": "/override/path"},
            ansible_cfg=Path("/etc/ansible.cfg"),
        )
        assert env["ANSIBLE_CONFIG"] == "/override/path"

    def test_no_extra_when_empty(self) -> None:
        env = build_envvars()
        # Only inherited env vars should be present (test env may or may not have them)
        for k in env:
            assert k.startswith(("ANSIBLE_", "ZOAU_"))

    def test_exception_stderr_is_redacted(self, tmp_path: Path) -> None:
        with patch(
            "rocannon.executor.ansible_runner.run",
            side_effect=RuntimeError("ssh failed: password=hunter2"),
        ):
            result = run_module(
                module="ansible.builtin.ping",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="localhost",
            )
        assert "hunter2" not in result["stderr"]
        assert REDACTED in result["stderr"]

    def test_parse_redacts_sensitive_keys_in_result(self) -> None:
        events = [
            {
                "event_data": {
                    "host": "h1",
                    "res": {
                        "changed": False,
                        "rc": 0,
                        "stdout": "ok",
                        "stderr": "",
                        "invocation": {
                            "module_args": {
                                "url": "https://api/x",
                                "api_token": "abc123",
                                "password": "p@ss",
                            }
                        },
                    },
                }
            }
        ]
        runner = _make_runner(events=events)
        result = _parse_runner_result(runner)
        args = result["result"]["invocation"]["module_args"]
        assert args["url"] == "https://api/x"
        assert args["api_token"] == REDACTED
        assert args["password"] == REDACTED

    def test_parse_redacts_stdout_stderr_inline_secrets(self) -> None:
        events = [
            {
                "event_data": {
                    "host": "h1",
                    "res": {
                        "changed": False,
                        "rc": 0,
                        "stdout": "running curl --token deadbeef http://x",
                        "stderr": "PASSWORD=hunter2 invalid",
                    },
                }
            }
        ]
        runner = _make_runner(events=events)
        result = _parse_runner_result(runner)
        assert "deadbeef" not in result["stdout"]
        assert "hunter2" not in result["stderr"]
        assert REDACTED in result["stdout"]
        assert REDACTED in result["stderr"]

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


# ---------------------------------------------------------------------------
# server._ConcurrencyMiddleware
# ---------------------------------------------------------------------------


class TestConcurrencyMiddleware:
    @staticmethod
    def _fake_context(target: str) -> Any:
        msg = MagicMock()
        msg.arguments = {"target": target}
        ctx = MagicMock()
        ctx.message = msg
        return ctx

    async def test_per_host_semaphore_reused(self) -> None:
        from rocannon.server import _ConcurrencyMiddleware

        mw = _ConcurrencyMiddleware(max_concurrent=10, max_per_host=3)
        sem_a1 = mw._get_host("a")
        sem_a2 = mw._get_host("a")
        mw._get_host("b")
        assert set(mw._per_host.keys()) == {"a", "b"}
        assert sem_a1 is sem_a2

    async def test_per_host_cap_blocks_third(self) -> None:
        import asyncio

        from rocannon.server import _ConcurrencyMiddleware

        mw = _ConcurrencyMiddleware(max_concurrent=10, max_per_host=2)
        in_flight = 0
        peak = 0
        gate = asyncio.Event()

        async def fake_call_next(_ctx: Any) -> str:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await gate.wait()
            in_flight -= 1
            return "ok"

        ctx = self._fake_context("h1")
        tasks = [asyncio.create_task(mw.on_call_tool(ctx, fake_call_next)) for _ in range(5)]
        # Give tasks a chance to acquire semaphores
        await asyncio.sleep(0.05)
        assert peak == 2  # only two ran concurrently on h1
        gate.set()
        await asyncio.gather(*tasks)

    async def test_global_cap_independent_of_host(self) -> None:
        import asyncio

        from rocannon.server import _ConcurrencyMiddleware

        mw = _ConcurrencyMiddleware(max_concurrent=2, max_per_host=10)
        in_flight = 0
        peak = 0
        gate = asyncio.Event()

        async def fake_call_next(_ctx: Any) -> str:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await gate.wait()
            in_flight -= 1
            return "ok"

        # Different hosts, so per-host cap doesn't bind
        tasks = [
            asyncio.create_task(mw.on_call_tool(self._fake_context(f"h{i}"), fake_call_next))
            for i in range(5)
        ]
        await asyncio.sleep(0.05)
        assert peak == 2  # global cap binds across hosts
        gate.set()
        await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# redaction.py
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_default_unset(self) -> None:
        # In a fresh context the var is None
        assert get_request_id() is None

    def test_set_and_reset_round_trip(self) -> None:
        token = set_request_id("abc12345")
        try:
            assert get_request_id() == "abc12345"
        finally:
            reset_request_id(token)
        assert get_request_id() is None

    def test_new_request_id_is_unique_hex(self) -> None:
        ids = {new_request_id() for _ in range(50)}
        assert len(ids) == 50
        for rid in ids:
            assert len(rid) == 8
            int(rid, 16)  # valid hex

    def test_formatter_injects_request_id(self) -> None:
        import logging as _logging

        fmt = CorrelationFormatter("[%(request_id)s] %(message)s")
        record = _logging.LogRecord("x", _logging.INFO, __file__, 1, "hello", None, None)
        token = set_request_id("deadbeef")
        try:
            assert fmt.format(record) == "[deadbeef] hello"
        finally:
            reset_request_id(token)

    def test_formatter_uses_dash_when_unset(self) -> None:
        import logging as _logging

        fmt = CorrelationFormatter("[%(request_id)s] %(message)s")
        record = _logging.LogRecord("x", _logging.INFO, __file__, 1, "hello", None, None)
        assert fmt.format(record) == "[-] hello"


class TestRedaction:
    def test_redact_dict_sensitive_keys(self) -> None:
        out = redact(
            {"user": "alice", "password": "p", "api_token": "t", "nested": {"secret": "s"}}
        )
        assert out["user"] == "alice"
        assert out["password"] == REDACTED
        assert out["api_token"] == REDACTED
        assert out["nested"]["secret"] == REDACTED

    def test_redact_list_recurses(self) -> None:
        out = redact([{"password": "x"}, {"name": "y"}])
        assert out[0]["password"] == REDACTED
        assert out[1]["name"] == "y"

    def test_redact_text_key_value_forms(self) -> None:
        assert "hunter2" not in redact_text("password=hunter2 trailing")
        assert "hunter2" not in redact_text("PASSWORD: hunter2")
        assert "deadbeef" not in redact_text("curl --token deadbeef http://x")
        assert "abc" not in redact_text("api-key=abc")

    def test_redact_text_preserves_non_secret_content(self) -> None:
        assert redact_text("the user logged in") == "the user logged in"
        assert redact_text("") == ""

    def test_redact_is_non_mutating(self) -> None:
        src = {"password": "p", "ok": "v"}
        redact(src)
        assert src["password"] == "p"
