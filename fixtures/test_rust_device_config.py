"""Native assembly config and current App closure, without production state."""
import dataclasses
import json
import os
import subprocess
from pathlib import Path

import pytest

from clinx.authority.request_pipe import api_config, invoke
from clinx.contracts.runtime_config import BridgeConfig


BASE = '[linear]\nteam_id="t"\ntrigger_label="go"\ntodo_state="Todo"\nrunning_state="Run"\nreview_state="Review"\n'


def rust(operation, **args):
    def no_callback(_):
        pytest.fail("Native config called a Python IO callback")
    return invoke(dict(mode="config", operation=operation, arguments=args), no_callback)


def test_native_toml_matches_existing_config_and_provider_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "deep" / "repository").mkdir(parents=True)
    (tmp_path / "link").symlink_to("deep/repository")
    p = tmp_path / "config.toml"
    p.write_text(BASE + '''[runtime]
runtime_host="Workstation-Air"
[app_server]
local_socket="provider.sock"
native_socket="~//native.sock"
[projects.z]
cwd="link/../absent"
branch="main"
[projects.a]
cwd="a"
branch="main"
[workspaces.ß]
root="link"
host="AIR"
codex_host_ids=["desktop-air"]
[workspaces.SS]
root="./new"
host="imac"
codex_host_ids=["desktop-air"]
''')
    # TOML bare keys intentionally remain ASCII under the TOML specification.
    p.write_text(p.read_text().replace("[workspaces.ß]", '[workspaces."ß"]'))
    old = BridgeConfig.load(p)
    expected = dataclasses.asdict(old)
    expected.pop("node_router")
    expected = json.loads(json.dumps(expected, default=str))
    native = rust("native_load", path=str(p))
    assert native == expected
    assert [p["alias"] for p in native["projects"]] == ["z", "a"]
    assert native["projects"][0]["repo"] == str(tmp_path / "deep/absent")
    assert rust("api_config", config=native) == api_config(old)
    assert rust("api_config", config=native)["host_aliases"]["ss"] == ["air", "imac"]


def test_native_config_missing_provider_is_explicit_and_does_not_fabricate_socket(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(BASE + '[runtime]\nruntime_host="air"\n[projects.test]\ncwd="."\nbranch="main"\n')
    cfg = rust("native_load", path=str(p))
    from clinx.authority.rust_authority import RustAuthorityError
    with pytest.raises(RustAuthorityError, match="PROVIDER_ENDPOINT_REQUIRED"):
        rust("api_config", config=cfg)


def test_current_monitor_resources_are_python_free_and_remove_old_readonly_runtime(tmp_path):
    root = tmp_path / "App/Contents/Resources"
    for name in ["clinx/nodes/service.py", "bridge.py", "DiscoveryRuntime/bin/python",
                 "runtime_control/owner.py", "provider_adapters/provider.py"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("retired")
    (root / "DiscoveryRuntime/bin").chmod(0o500)
    (root / "icon.icns").write_bytes(b"asset")
    binary = Path(os.environ.get("CLINX_BUILD_BINARY") or
                  str(Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / "clinx-build"))
    subprocess.run([str(binary), "native-monitor-resources",
                    str(Path(__file__).resolve().parents[1]), str(root)], check=True, timeout=10)
    receipt = json.loads((root / "python-sources.json").read_text())
    assert receipt["files"] == {}
    assert receipt["required_helpers"] == ["clinx-provider", "clinx-node", "clinx-node-daemon"]
    assert not list(root.rglob("*.py"))
    assert not (root / "DiscoveryRuntime").exists()
    assert (root / "icon.icns").read_bytes() == b"asset"


def test_native_gui_private_pipe_has_no_python_dependency_or_unbounded_error(tmp_path):
    binary = Path(os.environ["CLINX_TEST_RUST_BIN_DIR"]) / "clinx-node"
    env = {"HOME": str(tmp_path), "PATH": "/nonexistent", "LANG": "C.UTF-8"}
    for wire in ["[]\n", "x" * 4097 + "\n"]:
        result = subprocess.run([str(binary), "--discovery-gui"], input=wire,
            env=env, text=True, capture_output=True, timeout=5, check=True)
        assert json.loads(result.stdout) == {"error": "DISCOVERY_UNAVAILABLE"}
        assert result.stderr == ""
    wire = json.dumps({"operation": "forget", "node_id": "missing"}) + "\n"
    for _ in range(2):
        result = subprocess.run([str(binary), "--discovery-gui"], input=wire,
            env=env, text=True, capture_output=True, timeout=5, check=True)
        assert json.loads(result.stdout) == {"forgotten": True}
        assert result.stderr == ""
    raw = json.loads((tmp_path / "Library/Application Support/CLINX Monitor/Devices/peers.json").read_text())
    assert raw["peers"] == {}


def test_legacy_cli_locator_uses_installed_sibling_and_respects_explicit_identity(tmp_path, monkeypatch):
    from clinx.nodes import node_centre_entrypoint as entrypoint
    monkeypatch.setattr(entrypoint, "ROOT", tmp_path / "control")
    monkeypatch.delenv("CLINX_NODE_DAEMON_BINARY", raising=False)
    monkeypatch.delenv("CLINX_NODE_BINARY", raising=False)
    binary = tmp_path / "clinx-node-daemon"
    binary.write_text("fixture")
    binary.chmod(0o700)
    assert entrypoint.daemon_binary() == str(binary)
    monkeypatch.setenv("CLINX_NODE_DAEMON_BINARY", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="RUST_NODE_DAEMON_REQUIRED"):
        entrypoint.daemon_binary()
