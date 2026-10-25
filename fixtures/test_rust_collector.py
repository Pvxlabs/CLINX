"""Collector qualification on private real SQLite; no production/network tasks."""

import json
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


@pytest.mark.parametrize("ack", [-1, True, 999, None, "1"])
def test_invalid_ack_cannot_discard_spool_or_advance_checkpoint(setup, ack):
    c = setup.create()
    c.tick()
    before = c.get("seq")
    setup.registration.upload = lambda *a, **kw: dict(accepted=True, ack_seq=ack)
    with pytest.raises(NodeProtocolError, match="OBSERVATION_ACK_CONFLICT"):
        c.tick()
    assert counts(setup.path) == (1, 3) and c.get("seq") == before


def test_ack_from_wrong_stream_cannot_delete_queued_content(setup):
    c = setup.create()
    c.tick()
    setup.registration.upload = lambda *a, **kw: dict(
        accepted=True, ack_seq=3, stream_id="another-stream"
    )
    with pytest.raises(NodeProtocolError, match="OBSERVATION_ACK_CONFLICT"):
        c.tick()
    assert counts(setup.path)[1] == 3


def test_worker_death_after_centre_commit_recovers_exact_batch(setup):
    c = setup.create()
    c.tick()
    batches = []

    def upload(identity, scope, **batch):
        batches.append(batch)
        result = setup.writer.accept("air", scope, batch)
        if len(batches) == 1:
            c.worker.child.kill()
            c.worker.child.wait()
        return result

    setup.registration.upload = upload
    with pytest.raises((TimeoutError, NodeProtocolError)):
        c.tick()
    c.close()
    assert counts(setup.path)[1] == 3
    c = setup.create()
    c.tick()
    assert batches[0] == batches[1] and counts(setup.path)[1] == 0
    with sqlite3.connect(setup.writer.path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_worker_death_before_sampling_commit_rolls_back_every_checkpoint(setup):
    c = setup.create()
    sample = setup.source.sample

    def interrupted(metadata, state):
        result = sample(metadata, state)
        c.worker.child.kill()
        c.worker.child.wait()
        return result

    setup.source.sample = interrupted
    with pytest.raises((TimeoutError, NodeProtocolError)):
        c.tick()
    c.close()
    assert counts(setup.path) == (0, 0)
    setup.source.sample = sample
    c = setup.create()
    assert c.get("seq") == 0
    c.tick()
    assert counts(setup.path) == (1, 3)


def test_spool_checkpoint_and_scrubbing_are_one_transaction(setup):
    c = setup.create()
    sample = setup.source.sample

    def malformed(metadata, state):
        events, state = sample(metadata, state)
        events[-1]["turn"]["summary"] = {"not": "display text"}
        return events, state

    setup.source.sample = malformed
    with pytest.raises(NodeProtocolError):
        c.tick()
    assert (
        counts(setup.path) == (0, 0)
        and c.get("seq") == 0
        and c.get("generation") is None
    )
    setup.source.sample = sample
    c.tick()
    assert counts(setup.path) == (1, 3)
    with sqlite3.connect(setup.path) as db:
        payloads = [json.loads(r[0]) for r in db.execute("SELECT payload FROM outbox")]
    assert all(e["native_thread_id"] for e in payloads)


def test_no_first_use_offline_grant_guess_and_no_native_writes(setup, native):
    before = {p.name: p.read_bytes() for p in native.root.iterdir() if p.is_file()}
    setup.live["online"] = False
    c = setup.create()
    assert c.tick() == 12
    assert c.status == "AWAITING_BILATERAL_APPROVAL" and counts(setup.path) == (0, 0)
    assert {
        p.name: p.read_bytes() for p in native.root.iterdir() if p.is_file()
    } == before


def test_scope_narrowing_uploads_only_redacted_sequences(setup):
    c = setup.create()
    c.tick()
    setup.live["scope"] = SharingScope(
        "alice", read_sessions=True, projects=("/other",)
    )
    c.tick()
    batch = setup.live["uploads"][-1]
    assert batch["events"] == [dict(seq=i, scope_redacted=True) for i in (1, 2, 3)]
    assert counts(setup.path)[1] == 0


def test_offline_backpressure_keeps_source_checkpoint(setup, monkeypatch):
    c = setup.create()
    c.tick()
    seq = c.get("seq")
    setup.live["online"] = False
    monkeypatch.setattr("clinx.monitor.observation_source.OUTBOX_LIMIT", 130)
    assert c.tick() == 2 and c.status == "OBSERVATION_BACKPRESSURE"
    assert c.get("seq") == seq and counts(setup.path)[1] == 3


def test_coverage_recovery_republishes_unchanged_turns(setup):
    c = setup.create()
    c.tick()
    sample = setup.source.sample

    def failed(metadata, state):
        events, state = sample(metadata, state)
        return [
            dict(
                events[-1],
                coverage="NATIVE_OBSERVER_UNAVAILABLE",
                turn=dict(turn_id=None, ordinal=0, native_state="UNKNOWN"),
            )
        ], state

    setup.source.sample = failed
    c.tick()
    failed_seq = c.get("seq")
    setup.source.sample = sample
    c.tick()
    assert c.get("seq") > failed_seq
    with sqlite3.connect(setup.path) as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM outbox")]
    assert events and all(e["coverage"] == "INDEXED" for e in events)
    assert any(e["turn"]["turn_id"] for e in events)


def test_recovery_queue_cannot_be_starved_by_active_sessions(setup):
    c = setup.create()
    c.put("generation", setup.source.generation)
    setup.source.inventory = lambda *a, **kw: ([], None, "INDEXED")
    sampled = []

    def sample(metadata, state):
        sampled.append(metadata["id"])
        return [], dict(state, active=True)

    setup.source.sample = sample
    for i in range(10):
        c.conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?)",
            (
                f"active-{i}",
                json.dumps(dict(id=f"active-{i}", cwd="/approved")),
                json.dumps(dict(active=True)),
                i,
            ),
        )
    c.conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?)",
        (
            "recover",
            json.dumps(dict(id="recover", cwd="/approved")),
            json.dumps(dict(active=False, hashes={"metadata": "old-error"})),
            100,
        ),
    )
    c.conn.commit()
    c.tick()
    assert "recover" in sampled and len(sampled) <= 16


def test_native_canonical_merge_has_exact_evidence_and_no_multiple_task_owner(setup):
    source = setup.source
    proof = dict(thread_id="t", turn_id="turn", observed_at=10, liveness="LIVE")
    source.native_turns = lambda metadata, state: (
        [
            dict(
                turn_id="turn",
                ordinal=5,
                native_state="RUNNING",
                model="gpt-fixture",
                live_observation=proof,
                summary="Native",
            )
        ],
        "INDEXED",
    )
    source.canonical_turns = lambda *a: [
        dict(
            task_ref="task",
            execution_ref="exec",
            turn_id="turn",
            ordinal=1,
            summary=None,
            native_state="UNKNOWN",
        )
    ]
    events, state = source.sample(dict(id="t", cwd="/approved"), {})
    assert len(events) == 1 and events[0]["turn"]["ordinal"] == 5
    assert (
        events[0]["turn"]["summary"] == "Native"
        and events[0]["turn"]["live_observation"] == proof
    )
    assert (
        events[0]["turn"]["model"] == "gpt-fixture"
        and events[0]["task_ref"] == "task"
        and state["active"]
    )
    source.canonical_turns = lambda *a: [
        dict(task_ref=x, turn_id=x, ordinal=1) for x in ("task-a", "task-b")
    ]
    with pytest.raises(NodeProtocolError, match="OBSERVATION_IDENTITY_CONFLICT"):
        source.sample(dict(id="t", cwd="/approved"), {})


def test_commit_busy_rolls_back_before_the_next_tick(setup):
    collector = setup.create()
    sample = setup.source.sample
    readers = []

    def hold_reader(metadata, state):
        result = sample(metadata, state)
        reader = sqlite3.connect(setup.path, check_same_thread=False)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM sessions").fetchone()
        readers.append(reader)
        return result

    setup.source.sample = hold_reader
    try:
        with pytest.raises(NodeProtocolError, match="COLLECTOR_STORE_UNAVAILABLE"):
            collector.tick()
        assert collector.get("seq") == 0
        assert counts(setup.path) == (0, 0)
    finally:
        for reader in readers:
            reader.rollback()
            reader.close()
    setup.source.sample = sample
    collector.tick()
    assert counts(setup.path) == (1, 3)
