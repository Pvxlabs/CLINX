"""Typed private client for Rust native history; no SQL, parser or read decisions."""

from pathlib import Path

from clinx.authority.rust_authority import RustAuthorityError
from clinx.contracts.thread_identity import ThreadLookupError
from clinx.nodes.network_worker import read_command

SCAN_BYTES = 512 * 1024

LEGACY_REPLY_RETIREMENT = (
    "CLINX_LEGACY_REPLY_FORMAT_RETIRED_V1\n"
    "The reply-format requirements in the earlier CLINX MANAGED EXECUTION CONTRACT are retired: "
    "CLINX no longer owns or constrains the final response format. The CLINX_EXECUTION_RESULT marker "
    "and STATUS, SUMMARY, CHANGED_FILES, VALIDATION, BLOCKERS, NEXT_STATE fields are no longer required. "
    "This only retires that CLINX-generated output template; all other instructions remain in effect."
)


def _read(operation, **arguments):
    try:
        return read_command("history." + operation, **arguments)
    except RustAuthorityError as exc:
        raise ThreadLookupError(str(exc), str(exc)) from None


def model_identifier(value):
    return _read("model", value=value)


def normalize_native_status(value):
    return _read("normalize", value=value)


def native_status_is_terminal(value):
    return normalize_native_status(value) in {
        "COMPLETED",
        "FAILED",
        "TIMED_OUT",
        "CANCELLED",
        "INTERRUPTED",
        "DISCONNECTED",
    }


def message(item):
    value = _read("message", value=item)
    return tuple(value) if value else None


def cursor_encode(tid, host, mode, before):
    return _read("cursor_encode", tid=tid, host=host, mode=mode, before=before)


def cursor_decode(cursor, tid, host, mode):
    return _read("cursor_decode", cursor=cursor, tid=tid, host=host, mode=mode)


class NativeHistory:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _read(self, operation, **arguments):
        return _read(operation, root=str(self.root), **arguments)

    def metadata(self, tid):
        return self._read("metadata", tid=tid)

    def context(
        self, tid, host, metadata, recent_turns, max_bytes, *, anchor=None, cursor=None
    ):
        return self._read(
            "context",
            tid=tid,
            host=host,
            metadata=metadata,
            recent_turns=recent_turns,
            max_bytes=max_bytes,
            anchor=anchor,
            cursor=cursor,
        )

    def identity(self, tid, metadata):
        return self._read("identity", tid=tid, metadata=metadata)

    def legacy_reply_format_pending(self, tid, metadata):
        return self._read("pending", tid=tid, metadata=metadata)

    def status(self, tid, metadata):
        return self._read("status", tid=tid, metadata=metadata)

    def turn_model(self, tid, metadata, turn_id, offset):
        return self._read(
            "turn_model", tid=tid, metadata=metadata, turn_id=turn_id, offset=offset
        )

    def _path(self, tid, metadata):
        return Path(self._read("path", tid=tid, metadata=metadata))

    def projection_thread_id(self, tid, metadata, conn):
        return self._read("projection", tid=tid, metadata=metadata)
