"""Private transport IO to Rust. No sockets, trust decisions or protocol state."""
from __future__ import annotations

import concurrent.futures
import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from clinx.authority.request_pipe import binary, invoke
from clinx.authority.rust_authority import RustAuthorityError

LIMIT = 18 * 1024 * 1024


@dataclass(frozen=True)
class PairedContext:
    state: Path
    peer_id: str | None = None


def command(operation, **arguments):
    return invoke(dict(mode='network', operation=operation, arguments=arguments),
                  lambda _: (_ for _ in ()).throw(RustAuthorityError('NETWORK_CALLBACK_INVALID')))


class PeerSocket:
    def __init__(self, certificate_hex, peer):
        self.certificate_hex, self.peer = certificate_hex, peer
    def getpeercert(self, binary_form=False):
        if not binary_form:
            raise ValueError('DER certificate required')
        return bytes.fromhex(self.certificate_hex)


class Worker:
    def __init__(self, incoming=None, callback=None):
        self.incoming, self.callback = incoming, callback
        self.child = subprocess.Popen([str(binary()), '--network-worker'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.lock, self.write_lock = threading.Lock(), threading.Lock()
        self.pending, self.sequence = {}, 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _send(self, value):
        wire = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()+b'\n'
        if len(wire) > LIMIT:
            raise ValueError('NODE_WORKER_FRAME_TOO_LARGE')
        with self.write_lock:
            view = memoryview(wire)
            while view:
                n = self.child.stdin.write(view)
                if not n:
                    raise BrokenPipeError('NODE_WORKER_UNAVAILABLE')
                view = view[n:]

    def call(self, rpc_method, *, _wait_timeout=125, **arguments):
        with self.lock:
            self.sequence += 1
            ident = self.sequence
            future = concurrent.futures.Future()
            self.pending[ident] = future
        try:
            self._send(dict(id=ident, operation=rpc_method, arguments=arguments))
            return future.result(timeout=_wait_timeout)
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def _incoming(self, event):
        stage = 'rpc_dispatch'
        try:
            result = self.incoming(event)
            stage = 'rpc_serialize'
            json.dumps(result, allow_nan=False)
        except Exception as exc:
            request = event.get('request', {})
            read = request.get('operation') in {'node.status','session.read','session.status','execution.context','execution.status'}
            result = dict(error_code='NODE_READ_FAILED' if read else 'NODE_REQUEST_FAILED', read_only=read,
                operation_state='READ_FAILED' if read else 'UNKNOWN', side_effect='NONE' if read else 'UNKNOWN',
                retry='READ_ONLY_RETRY' if read else 'RECONCILIATION_REQUIRED',
                failure_stage=stage, error_type=type(exc).__name__)
        try:
            self._send(dict(operation='reply', connection_id=event['connection_id'], result=result))
        except (OSError, ValueError, TypeError):
            pass  # No response is UNKNOWN; never replay a callback.

    def _callback(self, event):
        try:
            result = dict(result=self.callback(event))
        except Exception as exc:
            result = dict(error=getattr(exc, "code", "PROVIDER_DELIVERY_UNKNOWN"))
        try:
            self._send(dict(operation="reply", connection_id=event["connection_id"], result=result))
        except (OSError, ValueError, TypeError):
            pass

    def _read(self):
        try:
            pending = b''
            while True:
                part = self.child.stdout.read(65536)
                if not part:
                    break
                pending += part
                if len(pending) > LIMIT:
                    raise ValueError('NODE_WORKER_FRAME_TOO_LARGE')
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    event = json.loads(line)
                    if event.get('kind') == 'incoming':
                        threading.Thread(target=self._incoming, args=(event,), daemon=True).start()
                    elif event.get('kind') == 'callback':
                        threading.Thread(target=self._callback, args=(event,), daemon=True).start()
                    else:
                        with self.lock:
                            future = self.pending.get(event['id'])
                        if future is None:
                            raise ValueError('NODE_WORKER_RESPONSE_CONFLICT')
                        if 'error' in event:
                            future.set_exception(RustAuthorityError(event['error']))
                        else:
                            future.set_result(event['result'])
        except (OSError, ValueError, KeyError):
            pass
        finally:
            with self.lock:
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(TimeoutError('NODE_WORKER_UNAVAILABLE'))

    def close(self):
        with self.write_lock:
            if not self.child.stdin.closed:
                self.child.stdin.close()
        try:
            self.child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        self.reader.join(timeout=2)
        self.child.stdout.close()


_read_lock = threading.Lock()
_read_worker = None


def read_command(operation, **arguments):
    """Multiplex stateless Rust reads; no retry or Python projection state."""
    global _read_worker
    with _read_lock:
        if _read_worker is None:
            import atexit
            _read_worker = Worker()
            atexit.register(_read_worker.close)
        worker = _read_worker
    return worker.call(operation, **arguments)
