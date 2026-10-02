"""CLINX Monitor v1: a read-only projection of the canonical registry.

No TaskRegistry construction, migrations, provider calls, reconciliation or writes.
Run separately from the authority. The HTTP listener is always loopback-only.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

VERSION = "1"
TASK_LIMIT, EVENT_LIMIT, OP_LIMIT = 50, 100, 20
MAX_RESPONSE = 2 * 1024 * 1024
TERMINAL = {"COMPLETED", "IN_REVIEW", "BLOCKED", "FAILED", "RECOVERY_REQUIRED",
            "CANCELLED", "STOPPED"}
RUNNING = {"CLAIMED", "DISPATCHING", "TURN_STARTED", "CODEX_RUNNING", "FINALIZING",
           "CANCEL_REQUESTED", "CANCELLATION_PENDING"}
BLOCKED = {"BLOCKED", "FAILED", "RECOVERY_REQUIRED", "TRANSPORT_UNCERTAIN"}
EVENT_TYPES = {
    "V1SnapshotBaselineImported", "V1TerminalStateObserved",
    "V1ExecutionProgressObserved", "V1ExecutionResultPersistedObserved",
    "V1HostExecutionStartedObserved", "V1HostEvidenceObserved",
    "V1LeaseReleasedObserved", "V1UnattributedLeaseReleasedObserved",
    "V1ExecutionClaimObserved", "V1UnattributedExecutionClaimObserved",
}
OP_COLUMNS = """host_execution_ref,execution_ref,host,surface,operation_class,
    capability,operation,started_at,completed_at,duration_ms,exit_code,result_state,
    timed_out"""
TASK_COLUMNS = """task_id,project_alias,title,host,status,execution_state,current_stage,
    current_blocker,created_at,updated_at,last_progress_at,codex_running,retry_required,
    turn_id"""
EXEC_COLUMNS = """execution_ref,task_id,logical_model,resolved_model,stage,
    execution_state,turn_id,acquired_at,routing_identity_json,execution_policy_json"""


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_text(value, limit=256):
    if not isinstance(value, str):
        return None
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value[:limit])
    value = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    return re.sub(r"(?i)\b(password|secret|api[_-]?key|token)\s*[:=]\s*[^\s,;]+",
                  r"\1=[REDACTED]", value)


def object_json(value):
    try:
        item = json.loads(value or "{}")
        return item if isinstance(item, dict) else {}
    except (ValueError, TypeError):
        return {}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def elapsed(start, end):
    try:
        seconds = int((datetime.fromisoformat(end) -
                       datetime.fromisoformat(start)).total_seconds())
        return seconds if seconds >= 0 else None
    except (ValueError, TypeError):
        return None


def menu_state(state, running, retry, result):
    if state in BLOCKED or retry or (result and result["status"] == "BLOCKED"):
        return "BLOCKED"
    if state in RUNNING or running:
        return "RUNNING"
    if state in {"COMPLETED", "IN_REVIEW"} and result and result["status"] == "PASS":
        return "PASS"
    if state in {"QUEUED", "STOPPED", "CANCELLED", "COMPLETED", "IN_REVIEW"}:
        return "IDLE"
    return "BLOCKED"  # unknown is visible uncertainty, never success


# Activity uses the native paginated index, never provider RPC or execution writers.
ACTIVITY_LIMIT, ACTIVITY_TEXT_LIMIT = 40, 16384
UUID_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def activity_text(value):
    if not isinstance(value, str):
        return ""
    # Redact before truncating; incomplete PEM blocks must not leak a prefix.
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                   "[REDACTED]", value, flags=re.S)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b", "[REDACTED]", value)
    value = re.sub(r"(?i)\b(?:access[_-]?token|api[_-]?key|password|secret|token)\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+",
                   "[REDACTED]", value)
    return safe_text(value, len(value)).encode("utf-8")[:ACTIVITY_TEXT_LIMIT].decode("utf-8", errors="ignore")


@contextlib.contextmanager
def activity_database(path):
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        steps = [0]
        def budget():
            steps[0] += 1000
            return steps[0] > 250000
        conn.set_progress_handler(budget, 1000)
        conn.execute("BEGIN")
        yield conn


class ActivityReader:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def encode(scope, mode, position):
        return base64.urlsafe_b64encode(json.dumps([scope, mode, *position]).encode()).decode().rstrip("=")

    @staticmethod
    def decode(value, scope, mode):
        try:
            if not isinstance(value, str) or len(value) > 1024:
                raise ValueError()
            row = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
            if (not isinstance(row, list) or len(row) != 4 or row[:2] != [scope, mode]
                    or type(row[2]) is not int or not 0 <= row[2] < 2**63
                    or not isinstance(row[3], str) or len(row[3]) > 256):
                raise ValueError()
            return row[2], row[3]
        except (ValueError, TypeError, UnicodeError):
            raise APIError(409, "ACTIVITY_CURSOR_RESET_REQUIRED") from None

    def read(self, task_ref, execution, after=None, before=None):
        eref, turn = execution["execution_ref"], execution.get("turn_id")
        route = object_json(execution.get("routing_identity_json"))
        conversation, host, provider = (route.get(k) for k in ("conversation", "host", "provider"))
        thread = conversation.get("binding") if isinstance(conversation, dict) else None
        page = dict(schema_version="1", task_ref=task_ref, execution_ref=eref,
                    observed_at=now(), source="CODEX_NATIVE_HISTORY", availability="UNAVAILABLE",
                    reason=None, items=[], next_cursor=None, older_cursor=None, has_more=False)
        if (not isinstance(thread, str) or not re.fullmatch(UUID_PATTERN, thread)
                or not isinstance(turn, str) or not re.fullmatch(UUID_PATTERN, turn)):
            return dict(page, reason="EXACT_TURN_UNAVAILABLE")
        if not isinstance(host, dict) or host.get("stable_identifier") != "p620":
            return dict(page, reason="NATIVE_HOST_UNAVAILABLE")
        if not isinstance(provider, dict) or provider.get("stable_identifier") not in {"codex", "codex_app_server"}:
            return dict(page, reason="PROVIDER_UNSUPPORTED")
        try:
            index = self.root / "state_5.sqlite"
            history = self.root / "thread_history_1.sqlite"
            with activity_database(index) as conn:
                metadata = conn.execute("SELECT history_mode FROM threads WHERE id=?", (thread,)).fetchone()
                if metadata is None:
                    return dict(page, reason="NATIVE_THREAD_UNAVAILABLE")
                if metadata[0] != "paginated":
                    return dict(page, reason="NATIVE_HISTORY_MODE_UNSUPPORTED")
            # Cursor cannot cross executions, turns or a replaced source database.
            identity = [task_ref, eref, thread, turn, str(self.root), str(history.stat().st_ino)]
            scope = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
            position = self.decode(after or before, scope, "after" if after else "before") if after or before else None
            with activity_database(history) as conn:
                exists = conn.execute("SELECT 1 FROM thread_turns WHERE thread_id=? AND turn_id=?", (thread, turn)).fetchone()
                if not exists:
                    return dict(page, reason="NATIVE_TURN_UNAVAILABLE")
                high = conn.execute("SELECT updated_at_ordinal,item_id FROM thread_items WHERE thread_id=? AND turn_id=? "
                                    "ORDER BY updated_at_ordinal DESC,item_id DESC LIMIT 1", (thread, turn)).fetchone()
                high = tuple(high) if high else (0, "")
                if after and position > high:
                    raise APIError(409, "ACTIVITY_CURSOR_RESET_REQUIRED")
                predicate = "thread_id=? AND turn_id=? AND item_type='agentMessage' AND json_valid(item_json)"
                args = [thread, turn]
                predicate += " AND coalesce(json_extract(item_json,'$.phase'),'') IN ('','commentary','final_answer')"
                predicate += " AND coalesce(json_extract(item_json,'$.channel'),'') IN ('','commentary','final')"
                if position:
                    key = "updated_at_ordinal" if after else "rollout_ordinal"
                    predicate += f" AND ({key},item_id) {'>' if after else '<'} (?,?)"
                    args.extend(position)
                order = "updated_at_ordinal ASC,item_id ASC" if after else "rollout_ordinal DESC,item_id DESC"
                rows = conn.execute("SELECT item_id,rollout_ordinal,updated_at_ordinal,created_at_ms,"
                    "substr(json_extract(item_json,'$.text'),1,?) AS text,"
                    "length(CAST(json_extract(item_json,'$.text') AS BLOB)) AS byte_count,"
                    "json_extract(item_json,'$.phase') AS phase FROM thread_items WHERE " + predicate +
                    " ORDER BY " + order + " LIMIT ?", [ACTIVITY_TEXT_LIMIT + 1, *args, ACTIVITY_LIMIT + 1]).fetchall()
            more = len(rows) > ACTIVITY_LIMIT
            rows = rows[:ACTIVITY_LIMIT]
            next_position = (rows[-1]["updated_at_ordinal"], rows[-1]["item_id"]) if after and more else high
            older_position = (rows[-1]["rollout_ordinal"], rows[-1]["item_id"]) if rows and not after else None
            items = []
            for row in rows:
                if not isinstance(row["text"], str) or not row["text"].strip():
                    continue
                items.append(dict(id=row["item_id"], ordinal=row["rollout_ordinal"], revision=row["updated_at_ordinal"],
                                  timestamp_ms=row["created_at_ms"], kind="result" if row["phase"] == "final_answer" else "feedback",
                                  text=activity_text(row["text"]), truncated=(row["byte_count"] or 0) > ACTIVITY_TEXT_LIMIT))
            items.sort(key=lambda row: (row["ordinal"], row["id"]))
            return dict(page, availability="AVAILABLE", items=items, has_more=more,
                        next_cursor=self.encode(scope, "after", next_position),
                        older_cursor=self.encode(scope, "before", older_position) if more and older_position else None)
        except (OSError, sqlite3.Error) as error:
            reason = "NATIVE_HISTORY_UNAVAILABLE"
            if isinstance(error, sqlite3.OperationalError) and "interrupted" in str(error):
                reason = "NATIVE_HISTORY_BUDGET_EXHAUSTED"
            return dict(page, reason=reason)


class ObserverStore:
    """Read-only repository adapter; each request gets a coherent SQLite snapshot."""
    def __init__(self, path, native_home=None):
        self.path = Path(path).expanduser().resolve()
        self.activity_reader = ActivityReader(native_home or Path.home() / ".codex")

    @contextlib.contextmanager
    def snapshot(self):
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            # Bound worst-case legacy scans without changing/indexing the authority DB.
            budget = [0]
            def deadline():
                budget[0] += 1
                return int(budget[0] > 10000)
            conn.set_progress_handler(deadline, 1000)
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    def health(self):
        with self.snapshot() as conn:
            conn.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()
        return {"schema_version": VERSION, "status": "OK", "read_only": True,
                "observed_at": now(), "authority_liveness": "UNKNOWN",
                "auth_mode": "OBSERVER_BEARER", "events_transport": "POLLING"}

    @staticmethod
    def task_row(conn, task_ref):
        row = conn.execute(f"SELECT {TASK_COLUMNS} FROM tasks WHERE task_id=?",
                           (task_ref,)).fetchone()
        if row is None:
            raise APIError(404, "TASK_NOT_FOUND")
        return dict(row)

    @staticmethod
    def event_cursor(task_ref, cursor):
        return hashlib.sha256(task_ref.encode()).hexdigest()[:16] + "." + str(cursor)

    @classmethod
    def parse_cursor(cls, task_ref, after):
        if after is None:
            return 0
        match = re.fullmatch(r"([0-9a-f]{16})\.([0-9]{1,19})", after)
        if not match or match[1] != cls.event_cursor(task_ref, 0).split(".")[0]:
            raise APIError(400, "INVALID_CURSOR")
        value = int(match[2])
        if value > 9223372036854775807:
            raise APIError(400, "INVALID_CURSOR")
        return value

    def events(self, conn, task_ref, after=None, recent=False):
        cursor = self.parse_cursor(task_ref, after)
        table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='v2_events'").fetchone()
        if not table:
            if cursor:
                raise APIError(409, "CURSOR_RESET_REQUIRED")
            return {"items": [], "next_cursor": self.event_cursor(task_ref, 0),
                    "has_more": False, "coverage": "UNAVAILABLE"}
        high = conn.execute("SELECT coalesce(max(cursor),0) FROM v2_events").fetchone()[0]
        if cursor > high:
            raise APIError(409, "CURSOR_RESET_REQUIRED")
        # Read the existing append-only facts only. Never synthesize history from polls.
        direction = "DESC" if recent else "ASC"
        limit = 20 if recent else EVENT_LIMIT
        names = sorted(EVENT_TYPES)
        rows = conn.execute(
            "SELECT cursor,event_id,event_type,occurred_at,recorded_at,"
            "json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,"
            "'$.source_execution_ref') AS execution_ref "
            "FROM v2_events WHERE cursor>? AND cursor<=? "
            "AND json_extract(CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END,"
            "'$.source_task_id')=? AND event_type IN (" +
            ",".join("?" for _ in names) + ") ORDER BY cursor " + direction + " LIMIT ?",
            (cursor, high, task_ref, *names, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [{"cursor": self.event_cursor(task_ref, r["cursor"]),
                  "event_ref": safe_text(r["event_id"]), "kind": r["event_type"],
                  "execution_ref": safe_text(r["execution_ref"]),
                  "occurred_at": timestamp(r["occurred_at"]),
                  "recorded_at": timestamp(r["recorded_at"])} for r in rows]
        next_value = rows[-1]["cursor"] if more and not recent else high
        return {"items": items, "next_cursor": self.event_cursor(task_ref, next_value),
                "has_more": more, "coverage": "PARTIAL"}  # shadow history is optional

    def project(self, conn, task, observed):
        ref = task["task_id"]
        row = conn.execute(f"SELECT {EXEC_COLUMNS},NULL AS released_at FROM executions "
                           "WHERE task_id=?", (ref,)).fetchone()
        active = row is not None
        if row is None:
            row = conn.execute(f"SELECT {EXEC_COLUMNS},released_at FROM execution_history "
                               "WHERE task_id=? ORDER BY acquired_at DESC,execution_ref DESC LIMIT 1",
                               (ref,)).fetchone()
            # Never attach an old execution to a new queued/continued task projection.
            if row and (not task["turn_id"] or row["turn_id"] != task["turn_id"]):
                row = None
        execution = dict(row) if row else {}
        eref = execution.get("execution_ref")
        result = None
        if eref and execution.get("turn_id") and execution["turn_id"] == task["turn_id"]:
            row = conn.execute(
                "SELECT status,summary,validation,blockers,next_state,received_at "
                "FROM execution_results WHERE task_id=? AND execution_ref=? AND turn_id=?",
                (ref, eref, execution["turn_id"])).fetchone()
            if row:
                result = {k: safe_text(row[k], 4096) for k in
                          ("status", "summary", "validation", "blockers", "next_state")}
                result.update(execution_ref=eref, received_at=timestamp(row["received_at"]),
                              changed_files=None, text_truncated=any(
                                  len(row[k]) > 4096 for k in ("summary", "validation", "blockers")),
                              redaction="PATHS_AND_RAW_RESULT_OMITTED")
        route = object_json(execution.get("routing_identity_json"))
        routing = {}
        for key in ("host", "surface", "provider", "transport"):
            part = route.get(key)
            part = part if isinstance(part, dict) else {}
            routing[key] = {"identifier": safe_text(part.get("stable_identifier")),
                            "status": safe_text(part.get("status")) or "UNKNOWN"}
        policy = object_json(execution.get("execution_policy_json"))
        classes = policy.get("operation_classes")
        classes = [safe_text(x) for x in classes[:16] if isinstance(x, str)] if isinstance(classes, list) else None
        reasoning = None
        if eref:
            prepared = conn.execute(
                "SELECT reasoning_effort FROM prepared_executions "
                "WHERE resulting_execution_ref=? AND resulting_task_id=? "
                "ORDER BY created_at DESC LIMIT 1", (eref, ref)).fetchone()
            reasoning = safe_text(prepared[0]) if prepared else None
        ops = []
        if eref:
            ops = [dict(r) for r in conn.execute(
                f"SELECT {OP_COLUMNS} FROM host_executions WHERE task_id=? AND execution_ref=? "
                "ORDER BY started_at DESC,host_execution_ref DESC LIMIT ?",
                (ref, eref, OP_LIMIT + 1))]
        operations_more = len(ops) > OP_LIMIT
        ops = ops[:OP_LIMIT]
        for op in ops:
            for key in op:
                if isinstance(op[key], str):
                    op[key] = safe_text(op[key])
            op["timed_out"] = bool(op["timed_out"])
            op["started_at"] = timestamp(op["started_at"])
            op["completed_at"] = timestamp(op["completed_at"])
        state = safe_text(task["execution_state"]) or "UNKNOWN"
        stage = safe_text(task["current_stage"]) or "UNKNOWN"
        end = timestamp(execution.get("released_at"))
        if end is None and result:
            end = result["received_at"]
        if end is None and active and state not in TERMINAL:
            end = observed
        started = timestamp(execution.get("acquired_at"))
        blocker = safe_text(task["current_blocker"], 2048)
        return {
            "schema_version": VERSION, "task_ref": ref, "execution_ref": eref,
            "project": safe_text(task["project_alias"]), "title": safe_text(task["title"], 512),
            "host": safe_text(task["host"]), "state": state, "stage": stage,
            "execution_state": safe_text(execution.get("execution_state")) or "UNKNOWN",
            "execution_stage": safe_text(execution.get("stage")) or "UNKNOWN",
            "model": {"logical": safe_text(execution.get("logical_model")),
                      "resolved": safe_text(execution.get("resolved_model"))},
            "reasoning": reasoning,
            "current_activity": {"kind": "PERSISTED_STAGE", "label": stage,
                                 "observed_at": timestamp(task["last_progress_at"])},
            "blocker": {"code": "PERSISTED_BLOCKER", "message": blocker} if blocker else None,
            "timestamps": {"created_at": timestamp(task["created_at"]),
                           "updated_at": timestamp(task["updated_at"]),
                           "last_progress_at": timestamp(task["last_progress_at"]),
                           "started_at": started, "completed_at": timestamp(end) if state in TERMINAL else None,
                           "observed_at": observed},
            "elapsed_seconds": elapsed(started, end),
            "codex_running": bool(task["codex_running"]),
            "retry_required": bool(task["retry_required"]),
            "mutation_boundary": {"observer_read_only": True, "allowed_actions": [],
                                  "execution_operation_classes": classes,
                                  "execution_surface": safe_text(policy.get("execution_surface")),
                                  "authority_status": "PERSISTED" if classes is not None else "UNKNOWN"},
            "routing": routing, "phases": [], "progress_percent": None,
            "progress_basis": "NO_PERSISTED_DENOMINATOR",
            "recent_events": self.events(conn, ref, recent=True),
            "host_operations": ops, "host_operations_has_more": operations_more,
            "final_result": result, "artifacts": [],
            "artifacts_status": "NO_SAFE_ARTIFACT_REGISTRY",
            "menu_state": menu_state(state, task["codex_running"], task["retry_required"], result),
        }

    def task(self, task_ref):
        with self.snapshot() as conn:
            return self.project(conn, self.task_row(conn, task_ref), now())

    def list_tasks(self, state, offset=0):
        with self.snapshot() as conn:
            # Active means unresolved task state, not the lifetime status column.
            terminal = ("COMPLETED", "IN_REVIEW", "CANCELLED", "STOPPED")
            predicate = "NOT IN" if state == "active" else "IN"
            rows = conn.execute(
                f"SELECT {TASK_COLUMNS} FROM tasks WHERE execution_state {predicate} (?,?,?,?) "
                "ORDER BY updated_at DESC,task_id DESC LIMIT ? OFFSET ?",
                (*terminal, TASK_LIMIT + 1, offset)).fetchall()
            observed = now()
            return {"schema_version": VERSION, "observed_at": observed,
                    "items": [self.project(conn, dict(r), observed) for r in rows[:TASK_LIMIT]],
                    "next_offset": offset + TASK_LIMIT if len(rows) > TASK_LIMIT else None,
                    "has_more": len(rows) > TASK_LIMIT}

    def task_activity(self, task_ref, execution_ref, after=None, before=None):
        with self.snapshot() as conn:
            projection = self.project(conn, self.task_row(conn, task_ref), now())
            if projection["execution_ref"] != execution_ref:
                raise APIError(409, "ACTIVITY_EXECUTION_CHANGED")
            rows = conn.execute(
                "SELECT execution_ref,turn_id,routing_identity_json FROM executions WHERE task_id=? AND execution_ref=? "
                "UNION ALL SELECT execution_ref,turn_id,routing_identity_json FROM execution_history WHERE task_id=? AND execution_ref=?",
                (task_ref, execution_ref, task_ref, execution_ref)).fetchall()
            if len(rows) != 1:
                raise APIError(409, "ACTIVITY_EXECUTION_CHANGED")
            execution = dict(rows[0])
        return self.activity_reader.read(task_ref, execution, after, before)

    def task_events(self, task_ref, after):
        with self.snapshot() as conn:
            self.task_row(conn, task_ref)
            return {"schema_version": VERSION, "task_ref": task_ref, "observed_at": now(),
                    **self.events(conn, task_ref, after)}


class ObserverAPI:
    def __init__(self, store, credential):
        self.store, self.credential = store, credential

    def request(self, method, target, authorization):
        token = self.credential()
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise APIError(503, "AUTH_UNAVAILABLE")
        expected = ("Bearer " + token).encode()
        if not isinstance(authorization, str) or not hmac.compare_digest(
                hashlib.sha256(authorization.encode()).digest(), hashlib.sha256(expected).digest()):
            raise APIError(401, "UNAUTHORIZED")
        if method != "GET":
            raise APIError(405, "READ_ONLY")
        if len(target) > 2048:
            raise APIError(400, "INVALID_REQUEST")
        url = urlsplit(target)
        if url.scheme or url.netloc or url.fragment or "%" in url.path:
            raise APIError(400, "INVALID_REQUEST")
        try:
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True,
                             max_num_fields=4)
        except ValueError:
            raise APIError(400, "INVALID_QUERY") from None
        if any(len(v) != 1 for v in query.values()):
            raise APIError(400, "INVALID_QUERY")
        if url.path == "/v1/health" and not query:
            return self.store.health()
        if url.path == "/v1/tasks":
            if set(query) - {"state", "offset"} or query.get("state") not in (["active"], ["recent"]):
                raise APIError(400, "INVALID_QUERY")
            raw_offset = query.get("offset", ["0"])[0]
            if not re.fullmatch(r"[0-9]{1,6}", raw_offset) or int(raw_offset) > 100000:
                raise APIError(400, "INVALID_QUERY")
            return self.store.list_tasks(query["state"][0], int(raw_offset))
        activity = re.fullmatch(r"/v1/tasks/([A-Za-z0-9_-]{1,128})/activity", url.path)
        if activity:
            eref = query.get("execution_ref", [""])[0]
            if (set(query) - {"execution_ref", "after", "before"} or ("after" in query and "before" in query)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", eref)
                    or any(not query[k][0] for k in ("after", "before") if k in query)):
                raise APIError(400, "INVALID_QUERY")
            return self.store.task_activity(activity[1], eref, query.get("after", [None])[0], query.get("before", [None])[0])
        match = re.fullmatch(r"/v1/tasks/([A-Za-z0-9_-]{1,128})(/events)?", url.path)
        if match:
            if match[2] and not set(query) - {"after"}:
                return self.store.task_events(match[1], query.get("after", [None])[0])
            if not match[2] and not query:
                return self.store.task(match[1])
            raise APIError(400, "INVALID_QUERY")
        raise APIError(404, "NOT_FOUND")


class ObserverHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, port, api, *, audit_methods=False):
        self.api = api
        self.audit_methods = audit_methods
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(("127.0.0.1", port), ObserverHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never emit request data, authorization, tracebacks or source payloads.


class ObserverHandler(BaseHTTPRequestHandler):
    server_version = "CLINXObserver/1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.respond
        raise AttributeError(name)

    def respond(self):
        status = 200
        try:
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1:
                raise APIError(401, "UNAUTHORIZED")
            if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
                raise APIError(400, "BODY_NOT_ALLOWED")
            payload = self.server.api.request(self.command, self.path, auth[0])
        except APIError as error:
            status, payload = error.status, {"schema_version": VERSION, "error": error.code}
        except (sqlite3.Error, OSError):
            status, payload = 503, {"schema_version": VERSION, "error": "SOURCE_UNAVAILABLE"}
        except Exception:
            status, payload = 503, {"schema_version": VERSION, "error": "PROJECTION_UNAVAILABLE"}
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()
        token = self.server.api.credential()
        if token:
            body = body.replace(token.encode(), b"[REDACTED]")
        if len(body) > MAX_RESPONSE:
            status, body = 503, b'{"schema_version":"1","error":"RESPONSE_BOUND_EXCEEDED"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if status == 405:
            self.send_header("Allow", "GET")
        self.end_headers()
        if self.server.audit_methods:
            method = self.command if self.command in {
                "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"
            } else "OTHER"
            print(json.dumps({"observer_http_method": method, "status": status}), flush=True)
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    do_GET = respond


def main():
    token = os.environ.get("CLINX_OBSERVER_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
        raise SystemExit("CLINX_OBSERVER_TOKEN must be a 32..256 character URL-safe credential")
    path = os.environ.get("CLINX_OBSERVER_DB")
    if not path:
        raise SystemExit("CLINX_OBSERVER_DB must identify the existing canonical registry")
    store = ObserverStore(path)
    store.health()  # Fail closed; never initialize a missing registry.
    server = ObserverHTTPServer(int(os.environ.get("CLINX_OBSERVER_PORT", "8766")),
                                ObserverAPI(store, lambda: os.environ.get("CLINX_OBSERVER_TOKEN")),
                                audit_methods=os.environ.get("CLINX_OBSERVER_AUDIT_METHODS") == "1")
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
