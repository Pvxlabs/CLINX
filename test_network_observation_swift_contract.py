"""Linux 可执行的共享 JSON/源码契约；不冒充 Swift build 或 GUI 验收。"""
import json
from pathlib import Path
import jsonschema


def test_versioned_fixture_and_real_ui_entrypoint():
    root=Path(__file__).parent
    raw=json.loads((root/"MonitorApp/Tests/CLINXMonitorTests/Fixtures/network-observations.json").read_text())
    schema=json.loads((root/"docs/monitor/observation-v1.schema.json").read_text())
    for detail in raw.values():
        jsonschema.validate(detail["item"],schema)
    assert raw["detail"]["item"]["task_ref"] is None
    assert raw["detail"]["item"]["turn"]["execution_ref"] is None
    sources=root/"MonitorApp/Sources/CLINXMonitor"
    assert "NetworkObservationView(monitor: store)" in (sources/"MonitorRootView.swift").read_text()
    assert "showNetwork = true" in (sources/"MonitorRootView.swift").read_text()
    assert "v2/observations" in (sources/"ObserverClient.swift").read_text()
    model=(sources/"NetworkObservationModels.swift").read_text()
    assert "let taskRef: String?" in model
    assert "var id: String { observationId }" in model
