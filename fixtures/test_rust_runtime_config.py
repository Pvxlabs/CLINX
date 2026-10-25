"""Python configuration DTO/path/tuple conversion; native contracts live in Rust."""
from clinx.contracts.runtime_config import BridgeConfig


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
