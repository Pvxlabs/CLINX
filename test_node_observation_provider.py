"""Air's observation route must not depend on execution permission or start an owner."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest


def entrypoint():
    spec = importlib.util.spec_from_file_location("node_observation_entrypoint",
        Path(__file__).parent / "MonitorApp/Scripts/node_service_entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_read_only_node_loads_explicit_provider_without_execution_adapter(tmp_path):
    module = entrypoint()
    provider = object()
    config = {"observation_provider_config": str(tmp_path / "provider.toml"),
              "execute_tasks": False}
    with patch("bridge.BridgeConfig.load", return_value=provider) as load, \
            patch.object(module.CanonicalNodeExecutionAdapter, "from_config",
                         side_effect=AssertionError("read-only observation cannot start execution")), \
            patch("native_provider.existing_client",
                  side_effect=AssertionError("config loading cannot open a provider")):
        source = module.configured_observation_source(tmp_path, "air", config)
    load.assert_called_once_with(tmp_path / "provider.toml")
    assert source.provider_config is provider


def test_execution_config_is_not_implicit_observation_authority(tmp_path):
    with patch("bridge.BridgeConfig.load", side_effect=AssertionError("no implicit route")):
        source = entrypoint().configured_observation_source(tmp_path, "air",
            {"execution_config": "/execution-only.toml"})
    assert source.provider_config is None


@pytest.mark.parametrize("path", ["", " ", False, 3, []])
def test_invalid_explicit_provider_route_fails_closed(tmp_path, path):
    with pytest.raises(ValueError, match="observation_provider_config"):
        entrypoint().configured_observation_source(tmp_path, "air",
            {"observation_provider_config": path})


def test_missing_provider_config_does_not_fall_back_to_execution(tmp_path):
    with pytest.raises(FileNotFoundError):
        entrypoint().configured_observation_source(tmp_path, "air",
            {"observation_provider_config": str(tmp_path / "missing.toml"),
             "execution_config": "/execution-only.toml"})
