from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

from task_registry import TaskRegistry


PARENT = "fd948f0cabc30b6f48d32d6cb20d76970f8d3630"


def create_task(registry: TaskRegistry, directory: Path):
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    return registry.create_task(
        host="p620",
        workspace_alias="p620",
        project_alias="clinx",
        project_name="CLINX",
        cwd=str(workspace),
        repository_origin=None,
        branch="main",
        title="PVX-1805 compatibility fixture",
    )


def make_running(registry: TaskRegistry, task):
    with registry.execution(task.task_id, execution_ref="exec_compat", retain=True):
        registry.set_execution_state(
            task.task_id,
            "CODEX_RUNNING",
            current_stage="CODEX_RUNNING",
            codex_running=True,
            turn_id="turn_compat",
        )


def parent_registry_class(tmp_path: Path):
    result = subprocess.run(
        ["git", "show", f"{PARENT}:task_registry.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    name = "pvx1805_parent_registry_" + uuid.uuid4().hex
    path = tmp_path / f"{name}.py"
    path.write_text(result.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.TaskRegistry


@pytest.mark.parametrize("shadow_events", [False, True])
@pytest.mark.parametrize("reopen", [False, True])
def test_terminal_task_orphan_lease_reclaims_without_shadow_or_with_shadow(
    tmp_path, shadow_events, reopen
):
    db = tmp_path / "registry.sqlite3"
    registry = TaskRegistry(db, shadow_events=shadow_events)
    task = create_task(registry, tmp_path)
    make_running(registry, task)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET execution_state='COMPLETED', codex_running=0 WHERE task_id=?",
            (task.task_id,),
        )
        conn.execute("DELETE FROM executions WHERE task_id=?", (task.task_id,))

    if reopen:
        TaskRegistry(db, shadow_events=shadow_events)
    else:
        assert registry.reclaim_stale_worktree_leases() == 1

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM worktree_leases").fetchone()[0] == 0
    assert TaskRegistry(db, shadow_events=shadow_events).reclaim_stale_worktree_leases() == 0


@pytest.mark.parametrize("shadow_events", [False, True])
def test_task_recovery_required_does_not_reclaim_active_execution(
    tmp_path, shadow_events
):
    db = tmp_path / "active.sqlite3"
    registry = TaskRegistry(db, shadow_events=shadow_events)
    task = create_task(registry, tmp_path)
    make_running(registry, task)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE tasks SET execution_state='RECOVERY_REQUIRED', codex_running=0 WHERE task_id=?",
            (task.task_id,),
        )

    assert registry.reclaim_stale_worktree_leases() == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM worktree_leases").fetchone()[0] == 1


def test_parent_created_database_preserves_legacy_running_update(tmp_path):
    db = tmp_path / "parent.sqlite3"
    parent = parent_registry_class(tmp_path)(db)
    task = create_task(parent, tmp_path)
    make_running(parent, task)
    parent.set_execution_state(
        task.task_id, "CODEX_RUNNING", current_stage="CODEX_RUNNING"
    )
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
        assert "turn_id" not in columns
        assert "execution_state" not in columns

    migrated = TaskRegistry(db)
    migrated.set_execution_state(
        task.task_id, "CODEX_RUNNING", current_stage="CODEX_RUNNING"
    )
    migrated.set_execution_state(
        task.task_id, "CODEX_RUNNING", current_stage="CODEX_RUNNING"
    )
    assert migrated.get_task(task.task_id).turn_id == "turn_compat"
    assert migrated.get_execution_record("exec_compat")["execution_owned_turn"] is None


def test_legacy_running_update_with_shadow_does_not_fabricate_exact_turn(tmp_path):
    db = tmp_path / "parent-shadow.sqlite3"
    parent = parent_registry_class(tmp_path)(db)
    task = create_task(parent, tmp_path)
    make_running(parent, task)

    migrated = TaskRegistry(db, shadow_events=True)
    migrated.set_execution_state(
        task.task_id, "CODEX_RUNNING", current_stage="legacy-recheck"
    )
    events = migrated.shadow_event_store.read_events()
    progress = [
        event.event.payload.to_dict()
        for event in events
        if event.event.event_type == "V1ExecutionProgressObserved"
    ]
    assert len(progress) == 1
    assert progress[0]["turn_id"] is None
    assert progress[0].get("source_execution_ref") is None
    assert progress[0]["attribution"] == "UNATTRIBUTED"
    assert progress[0]["execution_state"] == "UNKNOWN"
    assert progress[0]["current_stage"] == "UNKNOWN"


def test_execution_and_task_state_reads_have_distinct_ownership_keys(tmp_path):
    db = tmp_path / "aliases.sqlite3"
    registry = TaskRegistry(db)
    task = create_task(registry, tmp_path)
    with registry.execution(task.task_id, execution_ref="exec_alias", retain=True):
        registry.set_execution_state(
            task.task_id,
            "CODEX_RUNNING",
            current_stage="provider_wait",
            codex_running=True,
            turn_id="turn_alias",
        )
        active = registry.get_active_execution("exec_alias")
        assert active["execution_state"] == "CODEX_RUNNING"
        assert active["execution_owned_state"] == "CODEX_RUNNING"
        assert active["execution_owned_turn"] == "turn_alias"
