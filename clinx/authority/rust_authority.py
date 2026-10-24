"""Bounded IPC client for the native authority, with no database access/fallback.

Callers must supply V1 commands produced by a qualified domain adapter. Legacy
Python Owner control requests are deliberately not translated to unchecked SQL.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import struct
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
        if operation not in {"claim", "commit", "renew", "read_commit", "health"} and operation not in {
            "business." + op for op in (
                "prepare", "start", "cancel", "get_status", "get_prepared", "list_tasks", "list_projects", "read_receipt",
                "work", "take_dispatch", "resume_dispatch", "successor_begin", "successor_take", "successor_commit", "successor_recover_empty",
                "effective_policy", "register_git_target", "revoke_git_target", "resolve_git_target", "adoption_endpoints", "bind_thread", "authorize_send", "fail_before_send",
                "observation_inventory", "observation_turns", "adopt", "adoption_begin", "adoption_commit", "prepare_policy", "apply_policy",
                "host_receive", "host_take", "host_begin", "host_complete", "host_sent", "host_failed", "host_ack", "host_rows", "host_reconcile_rejected",
                "input_begin", "input_commit", "observe_begin", "observe_commit", "take_cancel", "outbox", "ack_outbox",
            )
        }:
            raise RustAuthorityError("UNSUPPORTED_OPERATION")
        if not isinstance(request_id, str) or not 0 < len(request_id) <= 128:
            raise RustAuthorityError("INVALID_REQUEST_ID")
        message = dict(protocol_version=PROTOCOL, request_id=request_id,
                       operation=operation, payload=payload)
        wire = json.dumps(message, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode() + b"\n"
        if len(wire) > MAX_FRAME:
            raise RustAuthorityError("REQUEST_TOO_LARGE")
        sent = False
        try:
            info = self.endpoint.lstat()
            parent = self.endpoint.parent.lstat()
            if (not self.endpoint.is_absolute() or not stat.S_ISSOCK(info.st_mode)
                    or info.st_uid != self.authority_uid or info.st_mode & 0o007
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != self.authority_uid
                    or parent.st_mode & 0o022):
                raise RustAuthorityError("AUTHORITY_ENDPOINT_UNTRUSTED")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(self.timeout)
                peer.connect(os.fspath(self.endpoint))
                _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != self.authority_uid:
                    raise RustAuthorityError("AUTHORITY_PEER_UNTRUSTED")
                sent = True  # Includes partial sends. Never retry an unknown write.
                peer.sendall(wire)
                with peer.makefile("rb") as reader:
                    frame = reader.readline(MAX_FRAME + 1)
                if len(frame) > MAX_FRAME or not frame.endswith(b"\n"):
                    raise RustAuthorityError("DELIVERY_UNKNOWN")
                result = json.loads(frame)
                if (not isinstance(result, dict) or result.get("protocol_version") != PROTOCOL
                        or result.get("request_id") != request_id
                        or ("result" in result) == ("error" in result)):
                    raise RustAuthorityError("DELIVERY_UNKNOWN")
                if "error" in result:
                    raise RustAuthorityError(result["error"])
                if not isinstance(result["result"], dict):
                    raise RustAuthorityError("DELIVERY_UNKNOWN")
                return result["result"]
        except (OSError, ValueError) as error:
            raise RustAuthorityError("DELIVERY_UNKNOWN" if sent else "AUTHORITY_UNAVAILABLE") from error
