CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY, user_scope TEXT NOT NULL,
    public_key_fingerprint TEXT NOT NULL, protocol_version TEXT NOT NULL,
    providers_json TEXT NOT NULL, capabilities_json TEXT NOT NULL,
    route_ids_json TEXT NOT NULL, display_name TEXT NOT NULL,
    endpoint TEXT, state TEXT NOT NULL, last_seen TEXT,
    stale_after_seconds INTEGER NOT NULL, trusted INTEGER NOT NULL,
    authorized INTEGER NOT NULL, version_compatible INTEGER NOT NULL,
    identity_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sharing_scopes (
    node_id TEXT NOT NULL, user_scope TEXT NOT NULL,
    read_sessions INTEGER NOT NULL, execute_tasks INTEGER NOT NULL,
    providers_json TEXT NOT NULL, revoked INTEGER NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(node_id, user_scope)
);
CREATE TABLE IF NOT EXISTS observation_projects (
    node_id TEXT NOT NULL, user_scope TEXT NOT NULL, projects_json TEXT NOT NULL,
    PRIMARY KEY(node_id,user_scope)
);
CREATE TABLE IF NOT EXISTS thread_index (
    node_id TEXT NOT NULL, user_scope TEXT NOT NULL, provider TEXT NOT NULL,
    native_thread_id TEXT NOT NULL, source TEXT NOT NULL, source_version TEXT NOT NULL,
    last_seen TEXT NOT NULL, status_json TEXT NOT NULL,
    PRIMARY KEY(node_id, user_scope, provider, native_thread_id, source)
);
CREATE INDEX IF NOT EXISTS idx_thread_exact ON thread_index(user_scope, provider, native_thread_id);
CREATE TABLE IF NOT EXISTS request_ledger (
    node_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT,
    updated_at TEXT NOT NULL, PRIMARY KEY(node_id, request_id)
);
CREATE TABLE IF NOT EXISTS reference_routes (
    reference TEXT PRIMARY KEY, node_id TEXT NOT NULL, user_scope TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_fences (
    node_id TEXT NOT NULL, user_scope TEXT NOT NULL, provider TEXT NOT NULL,
    native_thread_id TEXT NOT NULL, fence INTEGER NOT NULL,
    owner_execution TEXT, PRIMARY KEY(node_id, user_scope, provider, native_thread_id)
);
