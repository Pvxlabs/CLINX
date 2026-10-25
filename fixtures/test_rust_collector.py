"""Public Collector facade smoke on private SQLite; no production/network tasks.

Checkpoint, ACK, crash and source qualification moved to
kernel/tests/collector_qualification.rs with the same original cases.
"""

import sqlite3
from types import SimpleNamespace

import pytest
from clinx.monitor.network_observation import ObservationWriter
from clinx.monitor.observation_source import (
    CodexObservationSource,
    ObservationCollector,
)
from clinx.nodes.node_protocol import NodeProtocolError, SharingScope
from fixtures.test_rust_observation import native as native


@pytest.fixture
def setup(native, tmp_path):
    source = CodexObservationSource(native.root, "air")
    writer = ObservationWriter(tmp_path / "centre.db")
    scope = SharingScope("alice", read_sessions=True, projects=("/approved",))
    live = {"scope": scope, "online": True, "uploads": []}

    def heartbeat(identity, scope, **kw):
        if not live["online"]:
            raise TimeoutError("isolated offline fixture")
        return dict(registered=True, scope=live["scope"].as_dict())

    def upload(identity, scope, **batch):
        live["uploads"].append(batch)
        return writer.accept("air", scope, batch)

    registration = SimpleNamespace(heartbeat=heartbeat, upload=upload)
    identity = SimpleNamespace(public={"node_id": "air"})
    instances = []

    def create():
        collector = ObservationCollector(
            tmp_path / "spool.db",
            source,
            identity,
            registration,
            lambda: live["scope"],
            1234,
            (),
        )
        instances.append(collector)
        return collector

    yield SimpleNamespace(
        create=create,
        source=source,
        writer=writer,
        live=live,
        registration=registration,
        path=tmp_path / "spool.db",
    )
    for c in instances:
        c.close()


def counts(path):
    with sqlite3.connect(path) as c:
        return tuple(
            c.execute("SELECT count(*) FROM " + table).fetchone()[0]
            for table in ("sessions", "outbox")
        )


def test_collector_singleton_durable_stream_and_restart(setup):
    c = setup.create()
    with pytest.raises(NodeProtocolError, match="COLLECTOR_ALREADY_RUNNING"):
        setup.create()
    assert c.tick() == 2
    assert counts(setup.path) == (1, 3)
    stream, seq = c.get("stream"), c.get("seq")
    c.close()
    c = setup.create()
    c.tick()
    assert c.get("stream") == stream and c.get("seq") == seq
    assert counts(setup.path) == (1, 0)
    with sqlite3.connect(setup.writer.path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_unknown_upload_ack_replays_only_identical_observation_receipts(setup):
    c = setup.create()
    c.tick()
    sent = []

    def upload(identity, scope, **batch):
        sent.append(batch)
        result = setup.writer.accept("air", scope, batch)
        if len(sent) == 1:
            raise TimeoutError("ACK lost after centre commit")
        return result

    setup.registration.upload = upload
    c.tick()
    assert c.status == "OFFLINE_BUFFERING" and counts(setup.path)[1] == 3
    seq = c.get("seq")
    c.close()
    c = setup.create()
    c.tick()
    assert sent[0] == sent[1] and counts(setup.path)[1] == 0 and c.get("seq") == seq
    with sqlite3.connect(setup.writer.path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3
