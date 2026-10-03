"""Bounded, exact reads of the current Unix user's native Codex history.

No registry, migrations, provider startup, subscriptions or writer claims. The
existing Codex index is the authority; legacy files are a bounded compatibility
source, not a second index. Cursors are scoped to host, thread and source.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
import sqlite3

from thread_identity import ThreadLookupError, readonly

SCAN_BYTES = 512 * 1024
SQL_STEPS = 250_000

# Native turn status is a provider fact.  Keep the original value in the
# payload and derive a small, stable vocabulary for read-plane presentation.
# This vocabulary never changes a managed execution_state.
_NATIVE_STATUS_ALIASES = {
    'inprogress': 'RUNNING',
    'running': 'RUNNING',
    'active': 'RUNNING',
    'started': 'RUNNING',
    'processing': 'RUNNING',
    'completed': 'COMPLETED',
    'succeeded': 'COMPLETED',
    'success': 'COMPLETED',
    'failed': 'FAILED',
    'error': 'FAILED',
    'errored': 'FAILED',
    'systemerror': 'FAILED',
    'timeout': 'TIMED_OUT',
    'timedout': 'TIMED_OUT',
    'cancelled': 'CANCELLED',
    'canceled': 'CANCELLED',
    'interrupted': 'INTERRUPTED',
    'aborted': 'INTERRUPTED',
    'disconnected': 'DISCONNECTED',
    'connectionlost': 'DISCONNECTED',
    'connectionclosed': 'DISCONNECTED',
}
_NATIVE_TERMINAL_STATES = frozenset({
    'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'INTERRUPTED', 'DISCONNECTED',
})


def normalize_native_status(value):
    """Return a presentation token without treating missing data as active."""
    if isinstance(value, dict):
        value = value.get('type') or value.get('status') or value.get('state')
    key = str(value or '').replace('_', '').replace('-', '').casefold()
    return _NATIVE_STATUS_ALIASES.get(key, 'UNKNOWN')


def native_status_is_terminal(value):
    return normalize_native_status(value) in _NATIVE_TERMINAL_STATES


def cursor_encode(tid, host, mode, before):
    return base64.urlsafe_b64encode(json.dumps([1, tid, host, mode, before]).encode()).decode().rstrip('=')


def cursor_decode(cursor, tid, host, mode):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        if value[:4] != [1, tid, host, mode] or len(value) != 5 or type(value[4]) is not int or value[4] < 0:
            raise ValueError()
        return value[4]
    except (ValueError, TypeError):
        raise ThreadLookupError('INVALID_CONTEXT_CURSOR', 'Cursor does not match the exact host/thread/source') from None


def _budget_sql(conn):
    steps = 0
    def progress():
        nonlocal steps
        steps += 1000
        return int(steps > SQL_STEPS)
    conn.set_progress_handler(progress, 1000)


def message(item):
    """Strict display allowlist; never recursively walk arbitrary protocol data."""
    kind = item.get('type')
    if item.get('channel') in ('analysis', 'reasoning') or item.get('phase') in ('analysis', 'reasoning'):
        return None
    if kind == 'agentMessage':
        role, text = 'assistant', item.get('text')
    elif kind == 'userMessage' or (kind == 'message' and item.get('role') in ('user', 'assistant')):
        role = 'user' if kind == 'userMessage' else item['role']
        content = item.get('content', [])
        text = '\n'.join(c['text'] for c in content if isinstance(c, dict)
                         and c.get('type') in ('text', 'input_text', 'output_text')
                         and isinstance(c.get('text'), str)) if isinstance(content, list) else None
    else:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    from bridge import _context_text
    text = re.sub(r'(?i)\b(authorization\s*[:=]\s*bearer\s+|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,})\b', '[REDACTED]', text)
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[REDACTED]', text, flags=re.S)
    return role, _context_text(text, len(text))


def context_result(tid, entries, *, source, max_bytes, turns, next_cursor=None, incomplete=False, evidence=None):
    # Entries arrive oldest first. Reserve space for both roles so a long final
    # answer cannot evict the user's request (or vice versa).
    latest = {}
    for row in entries:
        parsed = message(row['item'])
        if parsed:
            role, text = parsed
            latest[role] = (text, row.get('turn_id'), row.get('timestamp'))
    budget = max_bytes // max(len(latest), 1)
    texts, truncated, refs = {}, incomplete, {}
    for role, (value, turn_id, timestamp) in latest.items():
        data = value.encode('utf-8')
        texts[role] = data[:budget].decode('utf-8', errors='ignore')
        truncated |= len(data) > budget
        refs[role] = {'turn_id': turn_id, 'timestamp': timestamp}
    return dict(context_status='AVAILABLE' if texts else 'CONTEXT_UNAVAILABLE',
                context_source=source, context_scope='THREAD_RECENT', context_range='cursor-page',
                context_truncated=bool(truncated or next_cursor), next_cursor=next_cursor,
                last_user_intent=texts.get('user'), last_codex_result=texts.get('assistant'),
                provenance={'queried_thread_id': tid, 'context_turn_refs': turns,
                            'message_refs': refs, 'execution_attribution': 'NOT_CLAIMED', **(evidence or {})})


class NativeHistory:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def metadata(self, tid):
        path = self.root / 'state_5.sqlite'
        if not path.exists():
            raise ThreadLookupError('NATIVE_INDEX_UNAVAILABLE', 'Native state index is unavailable')
        if not os.access(path, os.R_OK):
            raise ThreadLookupError('NATIVE_ACCESS_DENIED', 'Native index permission denied')
        try:
            with readonly(path) as c:
                columns = {r[1] for r in c.execute('PRAGMA table_info(threads)')}
                if not {'id', 'cwd', 'rollout_path'} <= columns:
                    raise ThreadLookupError('NATIVE_SCHEMA_UNSUPPORTED', 'Native threads index lacks identity columns')
                selected = ['id', 'cwd', 'rollout_path'] + [
                    f'substr({k},1,512) AS {k}' if k in ('name', 'title') else k for k in (
                    'name', 'title', 'history_mode', 'archived', 'created_at', 'updated_at',
                    'cli_version', 'project_id') if k in columns]
                r = c.execute('SELECT ' + ','.join(selected) + ' FROM threads WHERE id=?', (tid,)).fetchone()
                return dict(r) if r else None
        except PermissionError:
            raise ThreadLookupError('NATIVE_ACCESS_DENIED', 'Native index permission denied') from None
        except sqlite3.Error:
            raise ThreadLookupError('NATIVE_INDEX_UNAVAILABLE', 'Native index cannot be read in read-only mode') from None

    def context(self, tid, host, metadata, recent_turns, max_bytes, *, anchor=None, cursor=None):
        if metadata.get('history_mode') == 'paginated':
            return self._paginated(tid, host, metadata, recent_turns, max_bytes, anchor, cursor)
        return self._legacy(tid, host, metadata, recent_turns, max_bytes, anchor, cursor)

    def identity(self, tid, metadata):
        """Validate a durable native session header for explicit adoption only."""
        with self._path(tid, metadata).open('rb') as stream:
            header = json.loads(stream.readline(131072))
        meta = header.get('payload', {})
        if (header.get('type') != 'session_meta' or meta.get('id') != tid
                or Path(meta.get('cwd', '')).resolve() != Path(metadata['cwd']).resolve()):
            raise ThreadLookupError('NATIVE_IDENTITY_CONFLICT', 'Native session identity disagrees with index')
        return {'id': tid, 'sessionId': meta['id'], 'cwd': metadata['cwd'],
                'projectId': metadata.get('project_id'), 'ephemeral': False,
                'status': {'type': 'UNKNOWN'}, 'cliVersion': metadata.get('cli_version') or meta.get('cli_version'),
                'name': metadata.get('name') or metadata.get('title'),
                'identity_source': 'CODEX_STATE_AND_SESSION_HEADER'}

    def status(self, tid, metadata):
        base = {
            'source': 'NATIVE_PERSISTED_HISTORY',
            'status': None,
            'state': 'UNKNOWN',
            'terminal': False,
        }
        if metadata.get('history_mode') != 'paginated':
            return dict(base, reason='NATIVE_HISTORY_MODE_UNSUPPORTED')
        try:
            with readonly(self.root / 'thread_history_1.sqlite') as conn:
                row = conn.execute('SELECT turn_id,status,started_at,completed_at FROM thread_turns '
                    'WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1', (tid,)).fetchone()
                if row is None:
                    return dict(base, reason='NATIVE_TURN_UNAVAILABLE')
                value = dict(row)
                state = normalize_native_status(value.get('status'))
                return dict(base, **value, state=state,
                            terminal=native_status_is_terminal(value.get('status')))
        except (OSError, sqlite3.Error):
            return dict(base, reason='NATIVE_HISTORY_UNAVAILABLE')

    def _paginated(self, tid, host, metadata, recent_turns, max_bytes, anchor, cursor):
        before = cursor_decode(cursor, tid, host, 'paginated')
        history_path = self.root / 'thread_history_1.sqlite'
        if not history_path.exists():
            raise ThreadLookupError('NATIVE_HISTORY_UNAVAILABLE', 'Native paginated history index is missing')
        if not os.access(history_path, os.R_OK):
            raise ThreadLookupError('NATIVE_ACCESS_DENIED', 'Native history permission denied')
        try:
            with readonly(history_path) as c:
                _budget_sql(c)
                predicate, args = 'thread_id=?', [tid]
                if anchor:
                    predicate += ' AND turn_id=?'; args.append(anchor)
                if before is not None:
                    predicate += ' AND rollout_ordinal<?'; args.append(before)
                turns = [dict(r) for r in c.execute(
                    'SELECT turn_id,rollout_ordinal,status,started_at,completed_at FROM thread_turns WHERE '
                    + predicate + ' ORDER BY rollout_ordinal DESC LIMIT ?', (*args, recent_turns + 1))]
                more = len(turns) > recent_turns
                turns = turns[:recent_turns]
                entries = []
                content_parts_truncated = False
                # Query only display types and extract bounded fields inside SQLite.
                # Tool/reasoning JSON never crosses the read-plane boundary.
                for turn in reversed(turns):
                    for kind in ('userMessage', 'agentMessage'):
                        rows = c.execute(
                            "SELECT item_id,rollout_ordinal,created_at_ms,"
                            "substr(json_extract(item_json,'$.text'),1,?) AS text,"
                            "substr(json_extract(item_json,'$.phase'),1,32) AS phase,"
                            "substr(json_extract(item_json,'$.channel'),1,32) AS channel,"
                            "json_array_length(item_json,'$.content') AS content_parts,"
                            "CASE WHEN item_type='userMessage' THEN (SELECT substr(group_concat(substr(json_extract(value,'$.text'),1,?), char(10)),1,?) "
                            "FROM (SELECT value FROM json_each(item_json,'$.content') LIMIT 32) WHERE json_extract(value,'$.type') IN ('text','input_text')) END AS content "
                            "FROM thread_items WHERE thread_id=? AND turn_id=? AND item_type=? "
                            "AND coalesce(json_extract(item_json,'$.phase'),'') NOT IN ('analysis','reasoning') "
                            "AND coalesce(json_extract(item_json,'$.channel'),'') NOT IN ('analysis','reasoning') "
                            "ORDER BY rollout_ordinal DESC LIMIT 1", (max_bytes + 1, max_bytes + 1, max_bytes + 1, tid, turn['turn_id'], kind))
                        for row in rows:
                            content_parts_truncated |= (row['content_parts'] or 0) > 32
                            entries.append({'item': {'type': kind, 'text': row['text'],
                                'phase': row['phase'], 'channel': row['channel'],
                                'content': [{'type': 'text', 'text': row['content'] or ''}]},
                                'turn_id': turn['turn_id'], 'timestamp': row['created_at_ms']})
                projection = c.execute('SELECT next_rollout_byte_offset FROM thread_history_projection_state WHERE thread_id=?', (tid,)).fetchone()
                offset = projection[0] if projection else None
        except sqlite3.OperationalError as exc:
            code = 'NATIVE_HISTORY_BUDGET_EXHAUSTED' if 'interrupted' in str(exc) else 'NATIVE_SCHEMA_UNSUPPORTED'
            raise ThreadLookupError(code, 'Native history unavailable within the bounded read contract') from None
        except (sqlite3.Error, ValueError):
            raise ThreadLookupError('NATIVE_SCHEMA_UNSUPPORTED', 'Native paginated history is incompatible') from None
        path = self._path(tid, metadata)
        size = path.stat().st_size if path and path.exists() else None
        incomplete = offset is None or size is None or offset < size
        return context_result(tid, entries, source='CODEX_NATIVE_HISTORY', max_bytes=max_bytes,
            turns=[t['turn_id'] for t in reversed(turns)],
            next_cursor=cursor_encode(tid, host, 'paginated', turns[-1]['rollout_ordinal']) if more else None,
            incomplete=incomplete or content_parts_truncated, evidence={'indexed_source': 'CODEX_THREAD_HISTORY_PRIMARY_KEY',
                'native_turns': turns, 'projection_byte_offset': offset, 'rollout_bytes': size,
                'projection_incomplete': incomplete, 'sql_step_budget': SQL_STEPS,
                'body_byte_budget': max_bytes, 'content_parts_limit': 32,
                'content_parts_truncated': content_parts_truncated})

    def _path(self, tid, metadata):
        path = Path(metadata['rollout_path']).resolve()
        if not path.is_relative_to(self.root) or not path.name.endswith('-' + tid + '.jsonl'):
            raise ThreadLookupError('NATIVE_IDENTITY_CONFLICT', 'Indexed rollout is outside the native history root')
        return path

    def _legacy(self, tid, host, metadata, recent_turns, max_bytes, anchor, cursor):
        before = cursor_decode(cursor, tid, host, 'legacy')
        path = self._path(tid, metadata)
        with path.open('rb') as f:
            header = json.loads(f.readline(131072))
            meta = header.get('payload', {})
            if header.get('type') != 'session_meta' or meta.get('id') != tid or Path(meta.get('cwd', '')).resolve() != Path(metadata['cwd']).resolve():
                raise ThreadLookupError('NATIVE_IDENTITY_CONFLICT', 'Rollout session header disagrees with native index')
            start = f.tell()
            f.seek(0, 2); size = f.tell()
            end = min(before if before is not None else size, size)
            offset = max(start, end - SCAN_BYTES)
            f.seek(offset); data = f.read(end - offset)
        # A page ending inside a huge tool record skips that partial record;
        # the next cursor still makes progress toward earlier display messages.
        next_offset = offset
        page_start = offset
        if offset > start:
            split = data.find(b'\n')
            if split >= 0:
                page_start = offset + split + 1
                next_offset = min(page_start, end - 1)
                data = data[split + 1:]
            else:
                data = b''
        if before is not None and not data.endswith(b'\n'):
            data = data.rsplit(b'\n', 1)[0] if b'\n' in data else b''
        entries, turns, current, turn_offsets = [], [], None, {}
        line_start = page_start
        for raw_line in data.splitlines(keepends=True):
            line = raw_line.rstrip(b'\r\n')
            record_offset = line_start
            line_start += len(raw_line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            payload = row.get('payload', {})
            if row.get('type') == 'turn_context':
                current = payload.get('turn_id')
                if current and current not in turns:
                    turns.append(current)
                    turn_offsets[current] = record_offset
            if row.get('type') == 'response_item' and message(payload):
                entries.append({'item': payload, 'turn_id': current, 'timestamp': row.get('timestamp')})
        allowed = {anchor} if anchor else set(turns[-recent_turns:])
        entries = [r for r in entries if not allowed or r['turn_id'] in allowed]
        if not anchor and len(turns) > recent_turns:
            next_offset = turn_offsets[turns[-recent_turns]]
        return context_result(tid, entries, source='CODEX_LOCAL_SESSION', max_bytes=max_bytes,
            turns=[anchor] if anchor else turns[-recent_turns:],
            next_cursor=cursor_encode(tid, host, 'legacy', next_offset) if next_offset > start else None,
            incomplete=offset > start or len(turns) > recent_turns,
            evidence={'indexed_source': 'CODEX_STATE_THREADS_PRIMARY_KEY', 'source_bytes': end - offset,
                      'scan_byte_budget': SCAN_BYTES, 'body_byte_budget': max_bytes})
