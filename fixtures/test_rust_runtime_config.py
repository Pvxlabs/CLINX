"""Configuration authority, order and workflow contract over the actual Rust pipe."""
import pytest

from clinx.contracts.runtime_config import BridgeConfig, canonical_host, resolve_transport
from clinx.contracts.runtime_contracts import BridgeError
from clinx.host.production_workflows import load_workflows


BASE = '''[linear]
team_id = "team"
trigger_label = "label"
todo_state = "Todo"
running_state = "Running"
review_state = "Review"
[projects.z]
cwd = "./z"
branch = "main"
[projects.a]
cwd = "./a"
branch = "main"
'''


def load(tmp_path, extra="", base=BASE):
    path = tmp_path / "config.toml"
    path.write_text(base + extra)
    return BridgeConfig.load(path)


def test_order_and_path_io_preserve_existing_config(tmp_path, monkeypatch):
    (tmp_path / "destination").mkdir()
    (tmp_path / "z").symlink_to("destination", target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    cfg = load(tmp_path, '''\n[workspaces.z]
root="./z"
codex_host_ids=["desktop-z"]
[workspaces.a]
root="./a"
[host_executor]
trusted_workspace_roots=["'''+str(tmp_path)+'''", "'''+str(tmp_path)+'''"]
''')
    assert [p.project_alias for p in cfg.projects] == ["z", "a"]
    assert [w.alias for w in cfg.workspaces] == ["z", "a"]
    assert cfg.projects[0].repo == tmp_path / "destination"
    assert cfg.host_executor.trusted_workspace_roots == (tmp_path,)
    assert cfg.workspaces[0].codex_host_ids == ("desktop-z",)
    assert cfg.node_router is None


@pytest.mark.parametrize("value,expected", [('"1_024"', 1024), ('"５"', 5), ("5.9", 5), ('"+15"', 15)])
def test_integer_config_uses_original_python_lexical_contract(tmp_path, value, expected):
    cfg = load(tmp_path, base=BASE.replace('[projects.z]', f'poll_interval_seconds={value}\n[projects.z]'))
    assert cfg.poll_interval_seconds == expected


@pytest.mark.parametrize("value", ['"5.5"', '"5e0"', '"1__0"'])
def test_string_floats_cannot_become_integer_config(tmp_path, value):
    with pytest.raises(ValueError, match="invalid literal"):
        load(tmp_path, base=BASE.replace('[projects.z]', f'poll_interval_seconds={value}\n[projects.z]'))


def test_exact_host_selection_and_executor_binding(tmp_path):
    assert canonical_host(" Workstation-P620 ") == "p620"
    assert resolve_transport("workstation-p620", "P620", "local") == "local"
    with pytest.raises(BridgeError, match="cross-host"):
        resolve_transport("mac", "p620", "local")
    with pytest.raises(BridgeError, match="configured runtime host"):
        load(tmp_path, '\n[runtime]\nruntime_host="mac"\n[host_executor]\nenabled=true\nhost="p620"\n')


def workflow(**changes):
    return dict(identity="DEPLOY", target="app", action="apply", operation_class="PRODUCTION_MUTATION",
                argv=["/bin/echo", "/fixed/{release}", "{sha}"], resource="app",
                parameters={"sha": {"format": "source_sha"}, "release": {"format": "identity"}}, **changes)


def test_workflow_schema_and_render_share_rust_host_contract():
    item, = load_workflows({"entry": workflow()})
    assert item.parameter_schema["required"] == ["release", "sha"]
    assert item.render({"sha": "a" * 40, "release": "r1"}) == ("/bin/echo", "/fixed/r1", "a" * 40)
    with pytest.raises(ValueError, match="invalid workflow parameter release"):
        item.render({"sha": "a" * 40, "release": "../escape"})
    with pytest.raises(ValueError, match="registered schema"):
        item.render({"sha": "a" * 40, "release": "r1", "extra": "x"})
    with pytest.raises(ValueError, match="resource fence"):
        load_workflows({"first": workflow(), "second": {**workflow(), "action": "check", "resource": "other"}})


def test_workflow_registration_rejects_path_and_placeholder_changes():
    for argv in [["relative", "{sha}"], ["/bin/echo", "/fixed/../{release}", "{sha}"], ["/bin/echo", "--arg={release}", "{sha}"]]:
        with pytest.raises(ValueError):
            load_workflows({"entry": {**workflow(), "argv": argv}})
