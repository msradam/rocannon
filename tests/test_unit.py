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

from rocannon.ansible import (
    _build_annotations,
    _make_tool_fn,
    _needs_approval,
    _request_approval,
)
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
    _parse_role_result,
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
    _parse_attributes,
    expand_modules,
    fetch_module_schema,
    fetch_module_schemas,
    fetch_role_schemas,
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

    def test_inventories_without_modules_raises(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        with pytest.raises(ValueError, match="needs 'inventories' and at least one"):
            Config(inventories=[inv], modules=[])

    def test_modules_without_inventories_raises(self) -> None:
        with pytest.raises(ValueError, match="needs 'inventories' and at least one"):
            Config(inventories=[], modules=["ansible.builtin.ping"])

    def test_empty_config_raises(self) -> None:
        with pytest.raises(ValueError, match="needs 'inventories' and at least one"):
            Config()

    def test_roles_only_config_is_valid(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        rp = tmp_path / "roles"
        rp.mkdir()
        config = Config(inventories=[inv], roles=["my.coll.role"], roles_path=rp)
        assert config.roles == ["my.coll.role"]
        assert config.roles_path is not None and config.roles_path.is_absolute()

    def test_missing_roles_path_raises(self, tmp_path: Path) -> None:
        inv = tmp_path / "inv.yml"
        inv.write_text("all:\n  hosts:\n    localhost:\n")
        with pytest.raises(ValueError, match="roles_path not found"):
            Config(inventories=[inv], roles=["r"], roles_path=tmp_path / "nope")

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

    def test_meta_carries_ansible_doc_fields(self) -> None:
        doc = {
            "x.y.z": {
                "doc": {
                    "short_description": "Z",
                    "options": {},
                    "requirements": ["lib >= 1.0"],
                    "version_added": "2.5.0",
                    "deprecated": {"why": "old"},
                    "seealso": [{"module": "x.y.other"}, {"ref": "g", "description": "d"}],
                },
                "return": {"path": {}, "changed": {}},
            }
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            meta = fetch_module_schema("x.y.z")["meta"]
        assert meta["requirements"] == ["lib >= 1.0"]
        assert meta["version_added"] == "2.5.0"
        assert meta["deprecated"] is True
        assert meta["seealso"] == ["x.y.other"]  # ref-only entries dropped
        assert set(meta["returns"]) == {"path", "changed"}

    def test_meta_omits_absent_and_historical_fields(self) -> None:
        doc = {
            "x.y.z": {
                "doc": {"short_description": "Z", "options": {}, "version_added": "historical"}
            }
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            meta = fetch_module_schema("x.y.z")["meta"]
        assert meta == {}


class TestToolTags:
    def test_tags_include_collection_and_namespace(self) -> None:
        from rocannon.ansible import _tags_for

        assert _tags_for("ansible.builtin.copy") == {"ansible.builtin", "ansible"}
        assert _tags_for("community.crypto.openssl_privatekey") == {"community.crypto", "community"}


class TestFetchRoleSchemas:
    _ROLE_DOC = {
        "my.coll.web": {
            "entry_points": {
                "main": {
                    "short_description": "Configure the web tier",
                    "options": {
                        "port": {"type": "int", "required": True, "description": "Listen port"},
                        "workers": {"type": "int", "default": 4},
                    },
                }
            }
        }
    }

    def test_parses_role_main_entry_point(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(self._ROLE_DOC)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schemas = fetch_role_schemas(["my.coll.web"])
        s = schemas["my.coll.web"]
        assert s["is_role"] is True
        assert s["meta"] == {"kind": "role", "entry_point": "main"}
        assert {p["name"] for p in s["parameters"]} == {"port", "workers"}
        assert s["description"] == "Configure the web tier"

    def test_role_without_argspec_is_skipped(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps({"my.coll.bare": {}})  # no entry_points
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schemas = fetch_role_schemas(["my.coll.bare"])
        assert schemas == {}

    def test_empty_input_makes_no_call(self) -> None:
        with patch("rocannon.schema.subprocess.run") as run:
            assert fetch_role_schemas([]) == {}
        run.assert_not_called()

    def test_only_main_entry_point_is_mapped(self) -> None:
        doc = {
            "my.coll.web": {
                "entry_points": {
                    "main": {"short_description": "M", "options": {"port": {"type": "int"}}},
                    "install": {"short_description": "I", "options": {"pkg": {"type": "str"}}},
                }
            }
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            s = fetch_role_schemas(["my.coll.web"])["my.coll.web"]
        # Only the main entry point's options become params; install's pkg is ignored.
        assert {p["name"] for p in s["parameters"]} == {"port"}

    def test_role_with_entry_points_but_no_main_is_skipped(self) -> None:
        doc = {"my.coll.x": {"entry_points": {"install": {"options": {}}}}}
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            assert fetch_role_schemas(["my.coll.x"]) == {}


class TestParseRoleResult:
    @staticmethod
    def _runner(status: str, stats: dict[str, Any], events: list[dict[str, Any]] | None = None):
        r = MagicMock()
        r.status = status
        r.stats = stats
        r.events = events or []
        return r

    def test_success_with_change(self) -> None:
        runner = self._runner(
            "successful",
            {"ok": {"h": 2}, "changed": {"h": 1}, "failures": {}, "dark": {}},
        )
        res = _parse_role_result(runner)
        assert res["status"] == "successful"
        assert res["changed"] is True
        assert res["result"]["stats"]["changed"] == {"h": 1}
        assert res["stderr"] == ""

    def test_unchanged_run(self) -> None:
        runner = self._runner(
            "successful",
            {"ok": {"h": 1}, "changed": {}, "failures": {}, "dark": {}},
        )
        assert _parse_role_result(runner)["changed"] is False

    def test_failure_is_surfaced(self) -> None:
        events = [
            {"event": "runner_on_failed", "event_data": {"host": "h", "res": {"msg": "boom"}}}
        ]
        runner = self._runner(
            "failed",
            {"ok": {}, "changed": {}, "failures": {"h": 1}, "dark": {}},
            events,
        )
        res = _parse_role_result(runner)
        assert res["status"] == "failed"
        assert "boom" in res["stderr"]


class TestFetchModuleSchemas:
    _DOC = {
        "ansible.builtin.ping": {"doc": {"short_description": "Ping", "options": {}}},
        "ansible.builtin.copy": {
            "doc": {
                "short_description": "Copy files",
                "options": {"dest": {"type": "str", "required": True}},
            }
        },
    }

    def test_batches_all_modules_in_one_call(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(self._DOC)
        with patch("rocannon.schema.subprocess.run", return_value=completed) as run:
            schemas = fetch_module_schemas(["ansible.builtin.ping", "ansible.builtin.copy"])
        # One subprocess for both modules, not one per module.
        assert run.call_count == 1
        assert set(schemas) == {"ansible.builtin.ping", "ansible.builtin.copy"}
        assert schemas["ansible.builtin.copy"]["parameters"][0]["name"] == "dest"

    def test_missing_module_is_omitted_not_raised(self) -> None:
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(self._DOC)  # no entry for the bad name
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schemas = fetch_module_schemas(["ansible.builtin.ping", "bad.module.nope"])
        assert "ansible.builtin.ping" in schemas
        assert "bad.module.nope" not in schemas

    def test_empty_input_makes_no_call(self) -> None:
        with patch("rocannon.schema.subprocess.run") as run:
            assert fetch_module_schemas([]) == {}
        run.assert_not_called()

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
        for ansible_type in ("str", "int", "bool", "list", "dict", "path", "raw"):
            assert ansible_type in ANSIBLE_TYPE_MAP

    def test_attributes_extracted_from_doc(self) -> None:
        doc = {
            "ansible.builtin.copy": {
                "doc": {
                    "short_description": "Copy files",
                    "options": {},
                    "attributes": {
                        "check_mode": {"support": "full"},
                        "diff_mode": {"support": "partial"},
                    },
                }
            }
        }
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = json.dumps(doc)
        with patch("rocannon.schema.subprocess.run", return_value=completed):
            schema = fetch_module_schema("ansible.builtin.copy")
        assert schema["attributes"]["check_mode"] == "full"
        assert schema["attributes"]["diff_mode"] == "partial"


class TestParseAttributes:
    def test_extracts_support_levels(self) -> None:
        attrs = _parse_attributes(
            {"check_mode": {"support": "full"}, "diff_mode": {"support": "none"}}
        )
        assert attrs["check_mode"] == "full"
        assert attrs["diff_mode"] == "none"

    def test_facts_and_raw_presence_flags(self) -> None:
        attrs = _parse_attributes({"facts": {"support": "full"}, "raw": {"support": "full"}})
        assert attrs["facts"] is True
        assert attrs["raw"] is True

    def test_missing_attributes_default_to_none_and_false(self) -> None:
        assert _parse_attributes({}) == {
            "check_mode": None,
            "diff_mode": None,
            "facts": False,
            "raw": False,
        }


class TestBuildAnnotations:
    """ansible.py, _build_annotations maps ansible-doc attributes to MCP hints."""

    def test_facts_module_is_read_only(self) -> None:
        ann = _build_annotations("community.general.thing_facts", {"facts": True})
        assert ann is not None
        assert ann.readOnlyHint is True

    def test_info_suffix_is_read_only(self) -> None:
        ann = _build_annotations("community.general.widget_info", {})
        assert ann is not None
        assert ann.readOnlyHint is True

    def test_curated_builtin_is_read_only(self) -> None:
        ann = _build_annotations("ansible.builtin.ping", {})
        assert ann is not None
        assert ann.readOnlyHint is True

    def test_raw_family_is_destructive_and_open_world(self) -> None:
        ann = _build_annotations("ansible.builtin.command", {"raw": True})
        assert ann is not None
        assert ann.destructiveHint is True
        assert ann.openWorldHint is True
        assert ann.readOnlyHint is None

    def test_plain_state_module_is_unannotated(self) -> None:
        assert _build_annotations("ansible.builtin.copy", {"check_mode": "full"}) is None


def _tool_signature(schema: dict[str, Any]) -> Any:
    import inspect

    fn = _make_tool_fn("a.b.c", schema, {"hosts": ["h1"], "groups": []}, MagicMock())
    return inspect.signature(fn).parameters


def _schema_with_attrs(
    check: str | None, diff: str | None, params: list[dict] | None = None
) -> dict:
    return {
        "name": "a.b.c",
        "description": "d",
        "parameters": params or [],
        "attributes": {"check_mode": check, "diff_mode": diff, "facts": False, "raw": False},
    }


class TestDryRunParams:
    """ansible.py, _make_tool_fn injects check/diff gated by ansible-doc support."""

    def test_check_injected_when_supported(self) -> None:
        params = _tool_signature(_schema_with_attrs("full", "none"))
        assert "check" in params
        assert params["check"].default is False
        assert "diff" not in params

    def test_partial_support_still_injects_check(self) -> None:
        assert "check" in _tool_signature(_schema_with_attrs("partial", "none"))

    def test_diff_injected_when_supported(self) -> None:
        assert "diff" in _tool_signature(_schema_with_attrs("full", "full"))

    def test_no_params_when_support_is_none(self) -> None:
        params = _tool_signature(_schema_with_attrs("none", "none"))
        assert "check" not in params
        assert "diff" not in params

    def test_no_params_when_attributes_absent(self) -> None:
        params = _tool_signature({"name": "a.b.c", "description": "d", "parameters": []})
        assert "check" not in params
        assert "diff" not in params

    def test_module_param_named_check_is_renamed(self) -> None:
        schema = _schema_with_attrs(
            "full", "none", params=[{"name": "check", "type": "str", "required": False}]
        )
        params = _tool_signature(schema)
        assert params["check"].default is False  # the injected dry-run flag
        assert "module_check" in params  # the module's own colliding param


class _FakeCtx:
    """Minimal Context stand-in for the approval gate.

    ``decision`` drives ``elicit``: 'accept', 'reject' (accepted-but-false),
    'decline', or 'raise' (client cannot elicit).
    """

    def __init__(self, decision: str) -> None:
        self.request_context = object()
        self._decision = decision

    async def info(self, *_a: Any, **_k: Any) -> None:
        return None

    async def elicit(self, _message: str, **_k: Any) -> Any:
        from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation

        if self._decision == "raise":
            raise RuntimeError("client does not support elicitation")
        if self._decision == "accept":
            return AcceptedElicitation(data=True)
        if self._decision == "reject":
            return AcceptedElicitation(data=False)
        return DeclinedElicitation()


class TestApprovalGate:
    """ansible.py human-in-the-loop approval gate (ROCANNON_APPROVAL)."""

    def test_mode_off_never_gates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ROCANNON_APPROVAL", raising=False)
        assert _needs_approval(read_only=False, destructive=True) is False

    def test_destructive_mode_gates_only_destructive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_APPROVAL", "destructive")
        assert _needs_approval(read_only=False, destructive=True) is True
        assert _needs_approval(read_only=False, destructive=False) is False

    def test_writes_mode_gates_all_non_readonly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_APPROVAL", "writes")
        assert _needs_approval(read_only=False, destructive=False) is True
        assert _needs_approval(read_only=True, destructive=False) is False

    def test_unknown_mode_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_APPROVAL", "yolo")
        assert _needs_approval(read_only=False, destructive=True) is False

    async def test_request_approval_accept_reject_decline(self) -> None:
        assert await _request_approval(_FakeCtx("accept"), "m", "h1", {}) is True
        assert await _request_approval(_FakeCtx("reject"), "m", "h1", {}) is False
        assert await _request_approval(_FakeCtx("decline"), "m", "h1", {}) is False

    async def test_request_approval_unsupported_client_returns_none(self) -> None:
        assert await _request_approval(_FakeCtx("raise"), "m", "h1", {}) is None
        assert await _request_approval(None, "m", "h1", {}) is None

    def _destructive_fn(self) -> Any:
        runtime = MagicMock()
        runtime.is_module_active.return_value = True
        runtime.active_name = "default"
        schema = _schema_with_attrs("none", "none")
        return _make_tool_fn(
            "ansible.builtin.command",
            schema,
            {"hosts": ["h1"], "groups": []},
            runtime,
            destructive=True,
        )

    async def test_decline_blocks_execution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_APPROVAL", "destructive")
        fn = self._destructive_fn()
        with patch("rocannon.ansible.run_module") as run:
            result = await fn(target="h1", ctx=_FakeCtx("decline"))
        run.assert_not_called()
        assert result["status"] == "denied"
        assert "declined" in result["stderr"]

    async def test_unsupported_client_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_APPROVAL", "destructive")
        fn = self._destructive_fn()
        with patch("rocannon.ansible.run_module") as run:
            result = await fn(target="h1", ctx=_FakeCtx("raise"))
        run.assert_not_called()
        assert result["status"] == "denied"
        assert "does not support elicitation" in result["stderr"]

    async def test_dry_run_is_never_gated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ROCANNON_APPROVAL", "destructive")
        schema = _schema_with_attrs("full", "none")
        runtime = MagicMock()
        runtime.is_module_active.return_value = True
        runtime.active_name = "default"
        runtime.active_config.return_value.timeouts = {}
        fn = _make_tool_fn(
            "ansible.builtin.command",
            schema,
            {"hosts": ["h1"], "groups": []},
            runtime,
            destructive=True,
        )
        with (
            patch("rocannon.ansible.run_module", return_value={"status": "ok"}) as run,
            patch("rocannon.ansible.build_inventory_list", return_value=[]),
            patch("rocannon.ansible.build_envvars", return_value={}),
        ):
            elicited = _FakeCtx("decline")
            result = await fn(target="h1", check=True, ctx=elicited)
        run.assert_called_once()
        assert result["status"] == "ok"


class TestOptionalDefaults:
    """Omitted optional params must not be forwarded with their ansible-doc default.

    Regression: a defaulted optional (e.g. template's block_start_string="{%")
    was baked into the signature and sent on every call, crashing ansible-core's
    templar on the literal "{%".
    """

    @staticmethod
    def _fn() -> Any:
        schema = {
            "name": "a.b.c",
            "description": "d",
            "parameters": [
                {"name": "src", "type": "str", "required": True},
                {
                    "name": "block_start_string",
                    "type": "str",
                    "required": False,
                    "default": "{%",
                },
            ],
            "attributes": {"check_mode": "none", "diff_mode": "none", "facts": False, "raw": False},
        }
        runtime = MagicMock()
        runtime.is_module_active.return_value = True
        runtime.active_name = "d"
        runtime.active_config.return_value.timeouts = {}
        return _make_tool_fn("a.b.c", schema, {"hosts": ["h1"], "groups": []}, runtime)

    @staticmethod
    async def _args_for(fn: Any, **call: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake(**kw: Any) -> dict[str, Any]:
            captured.update(kw)
            return {"status": "ok"}

        with (
            patch("rocannon.ansible.run_module", side_effect=fake),
            patch("rocannon.ansible.build_inventory_list", return_value=[]),
            patch("rocannon.ansible.build_envvars", return_value={}),
        ):
            await fn(**call)
        return dict(captured["module_args"])

    async def test_omitted_default_not_sent(self) -> None:
        args = await self._args_for(self._fn(), target="h1", src="x")
        assert args == {"src": "x"}
        assert "block_start_string" not in args

    async def test_explicit_value_is_sent(self) -> None:
        args = await self._args_for(self._fn(), target="h1", src="x", block_start_string="[%")
        assert args["block_start_string"] == "[%"

    def test_default_surfaced_in_description(self) -> None:
        import inspect as _inspect

        fn = self._fn()
        param = _inspect.signature(fn).parameters["block_start_string"]
        assert param.default is None
        assert "{%" in param.annotation.__metadata__[0].description


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

    def test_ensure_ansible_on_path_prepends_interpreter_dir(self) -> None:
        import os
        import sys

        from rocannon.executor import ensure_ansible_on_path

        bin_dir = str(Path(sys.executable).parent)
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=False):
            ensure_ansible_on_path()
            assert os.environ["PATH"].split(os.pathsep)[0] == bin_dir
            # Idempotent: a second call does not duplicate the entry.
            ensure_ansible_on_path()
            assert os.environ["PATH"].split(os.pathsep).count(bin_dir) == 1

    def test_list_stdout_coerced_to_text(self) -> None:
        # Network modules (e.g. arista.eos.eos_command) return stdout as a list
        # of per-command outputs; it must not crash redaction.
        events = [
            {
                "event_data": {
                    "host": "ceos1",
                    "res": {"changed": False, "stdout": ["line one", "line two"], "stderr": []},
                }
            }
        ]
        runner = _make_runner(events=events)
        result = _parse_runner_result(runner)
        assert result["stdout"] == "line one\nline two"
        assert result["stderr"] == ""

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

    def test_no_host_results_reports_failure_not_success(self) -> None:
        """An empty host list (target matched nothing) is a failure from the
        caller's perspective, even though runner.status='successful'."""
        runner = _make_runner(
            events=[],
            status="successful",
            stdout_text="Could not match supplied host pattern, ignoring: ghost\n",
        )
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"
        assert "no host produced a result" in result["stderr"]

    def test_no_host_results_with_failed_runner_keeps_runner_status(self) -> None:
        """If the runner itself failed before producing events, preserve that."""
        runner = _make_runner(events=[], status="failed", stderr_text="boom")
        result = _parse_runner_result(runner)
        assert result["status"] == "failed"


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


class TestRunModuleCheckDiff:
    """check/diff become play-level keywords and mark the result."""

    def _run(self, tmp_path: Path, **kw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        import yaml as yaml_mod

        captured: dict[str, Any] = {}

        def fake_run(**kwargs: Any) -> Any:
            captured["play"] = yaml_mod.safe_load(Path(kwargs["playbook"]).read_text())[0]
            return _make_runner(events=[_host_event("h1")])

        with patch("rocannon.executor.ansible_runner.run", side_effect=fake_run):
            result = run_module(
                module="ansible.builtin.copy",
                module_args={},
                inventory=[str(tmp_path)],
                host_pattern="h1",
                **kw,
            )
        return captured["play"], result

    def test_check_sets_play_keyword_and_marks_result(self, tmp_path: Path) -> None:
        play, result = self._run(tmp_path, check=True)
        assert play["check_mode"] is True
        assert result["check_mode"] is True

    def test_diff_sets_play_keyword(self, tmp_path: Path) -> None:
        play, _ = self._run(tmp_path, diff=True)
        assert play["diff"] is True

    def test_default_leaves_play_and_result_unmarked(self, tmp_path: Path) -> None:
        play, result = self._run(tmp_path)
        assert "check_mode" not in play
        assert "diff" not in play
        assert "check_mode" not in result


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


# ---------------------------------------------------------------------------
# Playbook persistence (real Ansible YAML round-trip)
# ---------------------------------------------------------------------------


from rocannon.playbook import (  # noqa: E402
    Playbook,
    PlaybookError,
    PlaybookStep,
    load_playbook,
    save_playbook,
)


class TestPlaybookSerialization:
    def test_saved_file_is_a_list_of_plays(self, tmp_path: Path) -> None:
        """The on-disk YAML must be a real Ansible playbook (list of plays)."""
        import yaml

        pb = Playbook(
            name="demo",
            description="",
            steps=[
                PlaybookStep(tool="ansible.builtin.ping", args={"target": "localhost"}),
                PlaybookStep(
                    tool="ansible.builtin.command",
                    args={"target": "webhosts", "cmd": "uptime"},
                ),
            ],
        )
        path = save_playbook(pb, root=tmp_path)
        data = yaml.safe_load(path.read_text())
        assert isinstance(data, list), "playbook must be a list of plays"
        assert len(data) == 2
        # First play
        assert data[0]["hosts"] == "localhost"
        assert data[0]["gather_facts"] is False
        task = data[0]["tasks"][0]
        assert "ansible.builtin.ping" in task
        # Second play
        assert data[1]["hosts"] == "webhosts"
        assert data[1]["tasks"][0]["ansible.builtin.command"] == {"cmd": "uptime"}

    def test_target_becomes_play_hosts_not_a_task_arg(self, tmp_path: Path) -> None:
        """target is metadata for the play, not a key under the module."""
        pb = Playbook(
            name="t",
            description="",
            steps=[PlaybookStep(tool="ansible.builtin.ping", args={"target": "h1", "data": "x"})],
        )
        path = save_playbook(pb, root=tmp_path)
        text = path.read_text()
        # 'target' should not appear under the module's args dict
        import yaml

        data = yaml.safe_load(text)
        module_args = data[0]["tasks"][0]["ansible.builtin.ping"]
        assert "target" not in module_args
        assert module_args == {"data": "x"}
        assert data[0]["hosts"] == "h1"

    def test_round_trip_preserves_step_shape(self, tmp_path: Path) -> None:
        """Save then load returns equivalent steps."""
        pb = Playbook(
            name="rt",
            description="round trip check",
            steps=[
                PlaybookStep(
                    tool="ansible.builtin.command",
                    args={"target": "localhost", "cmd": "uptime"},
                ),
                PlaybookStep(
                    tool="ansible.builtin.copy",
                    args={"target": "h2", "src": "/a", "dest": "/b"},
                ),
            ],
        )
        path = save_playbook(pb, root=tmp_path)
        loaded = load_playbook(path)
        assert loaded.name == pb.name
        assert loaded.description == pb.description
        assert len(loaded.steps) == len(pb.steps)
        for original, parsed in zip(pb.steps, loaded.steps, strict=True):
            assert parsed.tool == original.tool
            assert parsed.args == original.args

    def test_description_round_trips_via_header_comment(self, tmp_path: Path) -> None:
        pb = Playbook(
            name="d",
            description="Line one.\nLine two.",
            steps=[PlaybookStep(tool="ansible.builtin.ping", args={"target": "localhost"})],
        )
        path = save_playbook(pb, root=tmp_path)
        loaded = load_playbook(path)
        assert loaded.description == "Line one.\nLine two."

    def test_legacy_dict_shape_still_loads(self, tmp_path: Path) -> None:
        """Pre-v0.5.1 {name, description, steps} files keep working."""
        legacy = tmp_path / ".rocannon" / "playbooks" / "legacy.yml"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            "name: legacy\n"
            "description: from before\n"
            "steps:\n"
            "- tool: ansible.builtin.ping\n"
            "  args:\n"
            "    target: localhost\n"
            "- module: ansible.builtin.command\n"
            "  target: webhosts\n"
            "  args:\n"
            "    cmd: uptime\n"
        )
        pb = load_playbook(legacy)
        assert pb.name == "legacy"
        assert pb.description == "from before"
        assert len(pb.steps) == 2
        assert pb.steps[0].tool == "ansible.builtin.ping"
        assert pb.steps[0].args["target"] == "localhost"
        assert pb.steps[1].tool == "ansible.builtin.command"
        assert pb.steps[1].args == {"cmd": "uptime", "target": "webhosts"}

    def test_hand_written_ansible_playbook_loads(self, tmp_path: Path) -> None:
        """A sysadmin-authored playbook (no Rocannon header) still parses."""
        pb_file = tmp_path / "pb.yml"
        pb_file.write_text(
            "- name: setup\n"
            "  hosts: all\n"
            "  gather_facts: false\n"
            "  tasks:\n"
            "  - name: install\n"
            "    ansible.builtin.apt:\n"
            "      name: nginx\n"
            "      state: present\n"
            "    become: true\n"
        )
        pb = load_playbook(pb_file)
        assert pb.name == "pb"
        assert len(pb.steps) == 1
        assert pb.steps[0].tool == "ansible.builtin.apt"
        # become: true is a task-control keyword and is ignored, not treated as a module
        assert pb.steps[0].args == {"name": "nginx", "state": "present", "target": "all"}

    def test_multi_task_play_yields_one_step_per_task(self, tmp_path: Path) -> None:
        """A play with N tasks expands to N steps, all sharing the play's hosts."""
        pb_file = tmp_path / "multi.yml"
        pb_file.write_text(
            "- name: webs\n"
            "  hosts: web\n"
            "  tasks:\n"
            "  - name: ping\n"
            "    ansible.builtin.ping:\n"
            "  - name: hello\n"
            "    ansible.builtin.command:\n"
            "      cmd: echo hi\n"
        )
        pb = load_playbook(pb_file)
        assert len(pb.steps) == 2
        assert all(s.args["target"] == "web" for s in pb.steps)
        assert pb.steps[0].tool == "ansible.builtin.ping"
        assert pb.steps[1].tool == "ansible.builtin.command"
        assert pb.steps[1].args["cmd"] == "echo hi"

    def test_refuse_overwrite_without_flag(self, tmp_path: Path) -> None:
        pb = Playbook(
            name="x",
            description="",
            steps=[PlaybookStep(tool="ansible.builtin.ping", args={"target": "h"})],
        )
        save_playbook(pb, root=tmp_path)
        with pytest.raises(PlaybookError, match="already exists"):
            save_playbook(pb, root=tmp_path)
        save_playbook(pb, root=tmp_path, overwrite=True)  # ok with flag

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        for bad in ("", "-leading-dash", ".hidden", "has spaces", "slash/in/it"):
            pb = Playbook(
                name=bad,
                description="",
                steps=[PlaybookStep(tool="ansible.builtin.ping", args={})],
            )
            with pytest.raises(PlaybookError, match="invalid playbook name"):
                save_playbook(pb, root=tmp_path)


# ---------------------------------------------------------------------------
# `rocannon <fqcn>` CLI dispatch
# ---------------------------------------------------------------------------


import argparse  # noqa: E402

from rocannon.cli import (  # noqa: E402
    _add_module_param,
    _append_to_record,
    _build_module_parser,
    _looks_like_fqcn,
    _safe_record_name,
)


class TestFqcnRouting:
    def test_looks_like_fqcn_yes(self) -> None:
        assert _looks_like_fqcn("ansible.builtin.ping")
        assert _looks_like_fqcn("community.general.docker_container")
        assert _looks_like_fqcn("ibm.ibm_zos_core.zos_data_set")

    def test_looks_like_fqcn_no(self) -> None:
        assert not _looks_like_fqcn("mcp")
        assert not _looks_like_fqcn("doctor")
        assert not _looks_like_fqcn("--help")
        assert not _looks_like_fqcn("-p")
        assert not _looks_like_fqcn("")
        assert not _looks_like_fqcn("ping")  # no dot, ambiguous; require FQCN

    def test_safe_record_name_sanitizes(self) -> None:
        assert _safe_record_name("simple") == "simple"
        assert _safe_record_name("with-dashes") == "with-dashes"
        assert _safe_record_name("with.dots.in.it") == "with_dots_in_it"
        assert _safe_record_name("has spaces") == "has_spaces"
        assert _safe_record_name("-leading-dash") == "leading-dash"
        assert _safe_record_name("") == "session"


class TestModuleParamArgparseBuilding:
    """Map ansible-doc parameter schemas into argparse options."""

    def _build(self, params: list[dict]) -> argparse.ArgumentParser:
        import argparse as ap

        p = ap.ArgumentParser(prog="t")
        reserved: set[str] = set()
        for param in params:
            _add_module_param(p, param, reserved)
        return p

    def test_required_str_param_becomes_required_flag(self) -> None:
        parser = self._build([{"name": "src", "type": "str", "required": True}])
        ns = parser.parse_args(["--src", "/foo"])
        assert ns.src == "/foo"

    def test_optional_param_with_default_preserved(self) -> None:
        parser = self._build([{"name": "state", "type": "str", "default": "present"}])
        ns = parser.parse_args([])
        assert ns.state == "present"

    def test_int_type_coerced(self) -> None:
        parser = self._build([{"name": "port", "type": "int", "default": 22}])
        ns = parser.parse_args(["--port", "8080"])
        assert ns.port == 8080

    def test_bool_supports_negation(self) -> None:
        parser = self._build([{"name": "wait", "type": "bool", "default": False}])
        ns = parser.parse_args(["--wait"])
        assert ns.wait is True
        ns2 = parser.parse_args(["--no-wait"])
        assert ns2.wait is False

    def test_list_param_takes_multiple_values(self) -> None:
        parser = self._build([{"name": "users", "type": "list", "required": True}])
        ns = parser.parse_args(["--users", "alice", "bob"])
        assert ns.users == ["alice", "bob"]

    def test_choices_enforce(self) -> None:
        parser = self._build([{"name": "state", "type": "str", "choices": ["present", "absent"]}])
        ns = parser.parse_args(["--state", "absent"])
        assert ns.state == "absent"
        with pytest.raises(SystemExit):
            parser.parse_args(["--state", "bogus"])

    def test_param_colliding_with_reserved_name_gets_mangled(self) -> None:
        """A module param literally called `target` becomes `--module-target`."""
        import argparse as ap

        p = ap.ArgumentParser(prog="t")
        reserved: set[str] = {"target"}
        dest, ansible_name = _add_module_param(
            p, {"name": "target", "type": "str", "required": True}, reserved
        )
        assert ansible_name == "target"
        assert dest == "module_target"
        ns = p.parse_args(["--module-target", "x"])
        assert ns.module_target == "x"


class TestCliDryRunFlags:
    """rocannon <fqcn> exposes --check/--diff gated by ansible-doc support."""

    def _parser(self, check: str | None, diff: str | None) -> Any:
        schema = {
            "name": "a.b.c",
            "description": "d",
            "parameters": [],
            "attributes": {"check_mode": check, "diff_mode": diff, "facts": False, "raw": False},
        }
        parser, _ = _build_module_parser("a.b.c", schema)
        return parser

    def test_flags_present_when_supported(self) -> None:
        ns = self._parser("full", "full").parse_args(["--target", "h1", "--check", "--diff"])
        assert ns.check is True
        assert ns.diff is True

    def test_check_absent_when_unsupported(self) -> None:
        with pytest.raises(SystemExit):
            self._parser("none", "none").parse_args(["--target", "h1", "--check"])


class TestAppendToRecord:
    def test_creates_new_playbook_file(self, tmp_path: Path) -> None:
        path = tmp_path / "rb.yml"
        _append_to_record(
            path,
            "ansible.builtin.command",
            "localhost",
            {"cmd": "uptime"},
        )
        import yaml as yaml_mod

        data = yaml_mod.safe_load(path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["hosts"] == "localhost"
        assert data[0]["tasks"][0]["ansible.builtin.command"] == {"cmd": "uptime"}

    def test_appends_to_existing_playbook(self, tmp_path: Path) -> None:
        path = tmp_path / "rb.yml"
        _append_to_record(path, "ansible.builtin.ping", "h1", {})
        _append_to_record(path, "ansible.builtin.command", "h2", {"cmd": "ls"})
        import yaml as yaml_mod

        data = yaml_mod.safe_load(path.read_text())
        assert len(data) == 2
        assert data[0]["hosts"] == "h1"
        assert data[0]["tasks"][0]["ansible.builtin.ping"] == {}
        assert data[1]["hosts"] == "h2"
        assert data[1]["tasks"][0]["ansible.builtin.command"] == {"cmd": "ls"}

    def test_recorded_file_is_runnable_by_ansible_playbook(self, tmp_path: Path) -> None:
        """Sanity: the artifact must be parseable as standard Ansible YAML."""
        import yaml as yaml_mod

        path = tmp_path / "rb.yml"
        _append_to_record(path, "ansible.builtin.ping", "localhost", {"data": "pong"})
        data = yaml_mod.safe_load(path.read_text())
        play = data[0]
        # Required shape: top-level list of dicts with hosts + tasks
        assert set(play.keys()) >= {"name", "hosts", "tasks"}
        task = play["tasks"][0]
        # Module key is the FQCN; args are a dict under it
        assert "ansible.builtin.ping" in task
        assert task["ansible.builtin.ping"] == {"data": "pong"}


# ---------------------------------------------------------------------------
# cli.py, `quickstart`
# ---------------------------------------------------------------------------


class TestQuickstart:
    def test_scaffolds_profile_and_inventory(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from rocannon.cli import app

        target = tmp_path / ".rocannon"
        result = CliRunner().invoke(app, ["quickstart", "--dir", str(target)])
        assert result.exit_code == 0

        profile = target / "quickstart.yml"
        inventory = target / "inventory.ini"
        assert profile.exists()
        assert inventory.exists()
        assert "ansible_connection=local" in inventory.read_text()
        # Pin the interpreter so modules with deps run in rocannon's own env.
        assert "ansible_python_interpreter" in inventory.read_text()
        assert "ansible.builtin.setup" in profile.read_text()

        # Wiring is printed for both Claude Code and a generic client config.
        assert "claude mcp add rocannon" in result.stdout
        assert "mcpServers" in result.stdout

    def test_does_not_overwrite_without_force(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from rocannon.cli import app

        target = tmp_path / ".rocannon"
        runner = CliRunner()
        runner.invoke(app, ["quickstart", "--dir", str(target)])
        (target / "quickstart.yml").write_text("custom: true\n")

        result = runner.invoke(app, ["quickstart", "--dir", str(target)])
        assert result.exit_code == 0
        assert "already exist" in result.stdout
        assert (target / "quickstart.yml").read_text() == "custom: true\n"

        forced = runner.invoke(app, ["quickstart", "--dir", str(target), "--force"])
        assert forced.exit_code == 0
        assert "ansible.builtin.ping" in (target / "quickstart.yml").read_text()
