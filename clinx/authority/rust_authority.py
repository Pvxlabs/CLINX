"""Bounded IPC client for the native authority, with no database access/fallback.

Callers must supply V1 commands produced by a qualified domain adapter. Legacy
Python Owner control requests are deliberately not translated to unchecked SQL.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

PROTOCOL = "CLINX_RUST_AUTHORITY_V1"
MAX_FRAME = 1_048_576


class RustAuthorityError(RuntimeError):
    pass


class RustAuthorityClient:
    def __init__(self, endpoint: str | Path, authority_uid: int, *, timeout: float = 10):
        self.endpoint = Path(endpoint)
        self.authority_uid = authority_uid
        self.timeout = timeout

    def request(self, operation: str, payload: dict, *, request_id: str) -> dict:
        from clinx.nodes.network_worker import read_command
        return read_command('authority.request', endpoint=str(self.endpoint),
            authority_uid=self.authority_uid, timeout=self.timeout,
            wire_operation=operation, payload=payload, request_id=request_id)


def enabled():
    mode = os.environ.get('CLINX_AUTHORITY', 'python')
    if mode not in {'python', 'rust'}:
        raise RustAuthorityError('INVALID_AUTHORITY_SELECTION')
    return mode == 'rust'


class Commands:
    def __init__(self, client):
        self.client = client

    @classmethod
    def from_env(cls):
        try:
            endpoint = os.environ['CLINX_RUST_SOCKET']
            uid = int(os.environ['CLINX_RUST_UID'])
        except (KeyError, ValueError) as exc:
            raise RustAuthorityError('RUST_AUTHORITY_CONFIG_REQUIRED') from exc
        result = cls(RustAuthorityClient(endpoint, uid))
        health = result.client.request('health', {}, request_id='health')
        if health.get('business_commands') is not True:
            raise RustAuthorityError('RUST_BUSINESS_AUTHORITY_REQUIRED')
        return result

    def call(self, operation, payload=None, *, key=None):
        key = key or uuid.uuid4().hex
        try:
            return self.client.request('business.' + operation, payload or {}, request_id=key)
        except RustAuthorityError as exc:
            # Preserve the receipt identity on an unknown response. No retry and
            # no fallback to any old registry/Owner operation.
            exc.operation_id = key
            raise
