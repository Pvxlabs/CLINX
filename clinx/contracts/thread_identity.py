"""Exact, read-only Thread identity over existing CLINX and Codex records.

No TaskRegistry construction, reconciliation, provider startup or discovery.
Indexes are installed by the normal registry initialization migration, never here.
"""
from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit, unquote, quote


ID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class ThreadLookupError(ValueError):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


def parse_selector(*, thread_id=None, codex_uri=None, task_ref=None, query=None):
    if (thread_id is not None and codex_uri is not None) or task_ref is not None or query is not None:
        raise ThreadLookupError("IDENTITY_SELECTOR_CONFLICT", "Thread selector cannot be combined with task_ref/query or another thread selector")
    if codex_uri is not None:
        return _parse_uri(codex_uri)[0]
    if not isinstance(thread_id, str) or not re.fullmatch(ID_PATTERN, thread_id):
        raise ThreadLookupError("INVALID_THREAD_ID", "Expected a canonical hyphenated Codex UUID (not restricted to v4)")
    return thread_id


def _parse_uri(uri):
    error = ThreadLookupError('INVALID_CODEX_THREAD_URI', 'Expected codex://threads/<UUID> with at most one hostId parameter')
    if not isinstance(uri, str) or any(ord(c) <= 32 or ord(c) == 127 for c in uri):
        raise error
    try:
        parts = urlsplit(uri)
    except ValueError:
        raise error from None
    if (not uri.startswith('codex://threads/') or parts.netloc != 'threads'
            or parts.fragment or '#' in uri or not re.fullmatch('/' + ID_PATTERN, parts.path)):
        raise error
    host_id = None
    if '?' in uri:
        if not parts.query or '&' in parts.query or not parts.query.startswith('hostId='):
            raise error
        encoded = parts.query[len('hostId='):]
        if not encoded or re.search(r'%(?![0-9A-Fa-f]{2})', encoded):
            raise error
        try:
            host_id = unquote(encoded, encoding='utf-8', errors='strict')
        except UnicodeError:
            raise error from None
        # One decoding only. No plus-as-space, double encoding, userinfo or address syntax.
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', host_id) or '%' in host_id:
            raise error
    return parts.path[1:], host_id


def resolve_selector(cfg, *, host=None, **selectors):
    """Resolve only configured host identities, never a URI-supplied SSH address."""
    from clinx.contracts.execution_semantics import normalize_host
    tid = parse_selector(**selectors)
    host_id = _parse_uri(selectors['codex_uri'])[1] if selectors.get('codex_uri') is not None else None
    routes = {}
    desktop = {}
    for workspace in cfg.workspaces:
        canonical = normalize_host(workspace.host or workspace.alias).stable_identifier
        for alias in (workspace.alias, workspace.host, canonical, 'workstation-' + canonical):
            if alias:
                routes.setdefault(alias.casefold(), set()).add(canonical)
        for alias in getattr(workspace, 'codex_host_ids', ()):
            desktop.setdefault(alias, set()).add(canonical)
    router = getattr(cfg, 'node_router', None)
    if router is not None:
        for record in router.registry.list_nodes(user_scope=router.user_scope):
            if not record.trusted or not record.authorized:
                continue
            for alias in (record.node_id, record.display_name, *record.route_ids):
                if alias:
                    routes.setdefault(alias.casefold(), set()).add(record.node_id)
            for alias in record.route_ids:
                desktop.setdefault(alias, set()).add(record.node_id)
    def unique(values, code):
        if not values:
            raise ThreadLookupError(code, 'Host is not an authorized configured route')
        if len(values) != 1:
            raise ThreadLookupError('THREAD_HOST_CONFLICT', 'Host has multiple configured routes')
        return next(iter(values))
    explicit = unique(routes.get(host.casefold()), 'UNKNOWN_THREAD_HOST') if isinstance(host, str) else None
    if host is not None and not isinstance(host, str):
        raise ThreadLookupError('UNKNOWN_THREAD_HOST', 'Host must be a configured route name')
    routed = unique(desktop.get(host_id), 'UNKNOWN_CODEX_HOST_ID') if host_id is not None else None
    if routed and explicit and routed != explicit:
        raise ThreadLookupError('THREAD_HOST_CONFLICT', 'URI hostId conflicts with explicit host')
    resolved = routed or explicit or unique(routes.get(cfg.runtime_host.casefold()), 'UNKNOWN_THREAD_HOST')
    return tid, resolved, host_id


def canonical_codex_uri(thread_id, *, cfg=None, host=None, host_id=None):
    """Format an exact native identity; a URI is never visibility evidence.

    Desktop host IDs are configured aliases, not inferred SSH addresses. Do
    not choose between ambiguous aliases or fabricate a remote route.
    """
    if not isinstance(thread_id, str) or not re.fullmatch(ID_PATTERN, thread_id):
        return None
    if cfg is not None and host_id is None:
        from clinx.contracts.execution_semantics import normalize_host
        canonical = normalize_host(host or cfg.runtime_host).stable_identifier
        aliases = {alias for workspace in cfg.workspaces
                   if normalize_host(workspace.host or workspace.alias).stable_identifier == canonical
                   for alias in getattr(workspace, 'codex_host_ids', ())}
        if len(aliases) == 1:
            host_id = next(iter(aliases))
    uri = 'codex://threads/' + thread_id
    if host_id is not None:
        uri += '?hostId=' + quote(host_id, safe='')
    _parse_uri(uri)
    return uri


@contextlib.contextmanager
def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def rows(conn, sql, args):
    result = [dict(r) for r in conn.execute(sql + " LIMIT 257", args)]
    if len(result) > 256:
        raise ThreadLookupError("THREAD_LOOKUP_UNAVAILABLE", "Exact thread relation exceeds bounded lookup limit (256)")
    return result


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def native_display_state(native_status, provider_observation=None):
    """Project one native turn for presentation without changing CLINX state.

    A live exact active owner outranks persisted history.  Unknown, offline and
    ``notLoaded`` observations never turn a proved terminal turn back into
    running; owner conflicts remain UNKNOWN.  The returned value is deliberately
    separate from ``execution_state`` because an unbound native thread has no
    managed execution to complete.
    """
    from clinx.provider.native_history import normalize_native_status

    native_state = normalize_native_status((native_status or {}).get('status')
                                           if isinstance(native_status, dict) else None)
    observation = provider_observation if isinstance(provider_observation, dict) else {}
    if observation.get('ownership_conflict'):
        return 'UNKNOWN'
    rows = observation.get('observations') or []
    active_owner = any(
        normalize_native_status(row.get('state')) == 'RUNNING'
        and normalize_native_status(row.get('turn_status')) == 'RUNNING'
        for row in rows if isinstance(row, dict)
    )
    if active_owner:
        return 'RUNNING'
    if native_state in {'RUNNING', 'COMPLETED', 'FAILED', 'TIMED_OUT',
                        'CANCELLED', 'INTERRUPTED', 'DISCONNECTED'}:
        return native_state
    return 'UNKNOWN'
