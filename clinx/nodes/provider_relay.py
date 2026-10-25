"""Paired Provider protocol relay; no task DB, finalizer, input retry or Owner.

The local gateway accepts only the existing Transport UID. Every remote chunk
uses current mTLS trust and the owner/centre scope intersection. Provider JSON
is read from the configured existing daemon, never synthesized from node status.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import socketserver
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

from clinx.nodes.node_protocol import NODE_PROTOCOL_VERSION, NodeProtocolError

MAX_REQUEST = 1_048_576
MAX_RESPONSE = 16 * 1024 * 1024
CHUNK = 48 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def fail(code):
    raise NodeProtocolError(code, 'Provider relay contract rejected')


class NodeProviderRelay:
    """Disposable upload/reply buffers. Losing a buffer never replays a call."""
    def __init__(self, cfg, node_id):
        self.cfg, self.node_id = cfg, node_id
        self.lock = threading.Lock()
        self.buffers = {}

    def handle(self, request, scope):
        operation = request.get('operation')
        context = request.get('context')
        if (not isinstance(context, dict) or context.get('host') != self.node_id
                or context.get('user_scope') != scope.user_scope
                or not scope.allows('execution.authority')
                or not scope.allows_project(context.get('cwd', ''))):
            fail('PROVIDER_SCOPE_DENIED')
        reference = request.get('request_id')
        if not isinstance(reference, str) or len(reference) != 32 or any(c not in '0123456789abcdef' for c in reference):
            fail('INVALID_PROVIDER_REQUEST_ID')
        context_hash = hashlib.sha256(encoded(context)).hexdigest()
        with self.lock:
            now = time.monotonic()
            self.buffers = {k: v for k, v in self.buffers.items() if v['deadline'] > now}
            row = self.buffers.get(reference)
            if operation == 'provider.upload':
                index, count, digest = request.get('index'), request.get('count'), request.get('digest')
                if (type(index) is not int or type(count) is not int or not 0 <= index < count <= 22
                        or not isinstance(digest, str) or len(digest) != 64):
                    fail('INVALID_PROVIDER_CHUNK')
                try:
                    chunk = base64.b64decode(request['data'], validate=True)
                except (ValueError, KeyError):
                    fail('INVALID_PROVIDER_CHUNK')
                if len(chunk) > CHUNK:
                    fail('INVALID_PROVIDER_CHUNK')
                if row is None:
                    if index != 0 or len(self.buffers) >= 4096 or sum(v['state'] != 'CONSUMED' for v in self.buffers.values()) >= 16:
                        fail('PROVIDER_BUFFER_UNAVAILABLE')
                    row = dict(context_hash=context_hash, count=count, digest=digest, data=bytearray(),
                               next_index=0, state='UPLOADING', deadline=now + 120)
                    self.buffers[reference] = row
                if (row['context_hash'] != context_hash or row['digest'] != digest or row['count'] != count
                        or row['state'] != 'UPLOADING' or index != row['next_index']):
                    fail('PROVIDER_REQUEST_IDENTITY_CONFLICT')
                row['data'].extend(chunk)
                row['next_index'] += 1
                if len(row['data']) > MAX_REQUEST:
                    fail('PROVIDER_REQUEST_TOO_LARGE')
                if index + 1 < count:
                    return dict(request_id=reference, next_index=row['next_index'])
                if hashlib.sha256(row['data']).hexdigest() != digest:
                    fail('PROVIDER_CHUNK_DIGEST_CONFLICT')
                data = bytes(row.pop('data'))
                # Seal before any Provider I/O, including partial sends. A
                # duplicate final chunk or reconnect can only read the reply.
                row['state'] = 'IN_FLIGHT'
            elif operation == 'provider.read':
                if row is None or row['context_hash'] != context_hash:
                    fail('PROVIDER_REPLY_UNAVAILABLE')
                if row['state'] != 'READY':
                    fail('PROVIDER_DELIVERY_UNKNOWN')
                index = request.get('index')
                if type(index) is not int or not 0 <= index * CHUNK < max(1, len(row['reply'])):
                    fail('INVALID_PROVIDER_CHUNK')
                result = dict(request_id=reference, index=index, size=len(row['reply']), digest=row['reply_digest'],
                              data=base64.b64encode(row['reply'][index*CHUNK:(index+1)*CHUNK]).decode())
                if (index + 1) * CHUNK >= len(row['reply']):
                    row.pop('reply')
                    row['state'] = 'CONSUMED'
                return result
            else:
                fail('UNKNOWN_PROVIDER_OPERATION')
        try:
            body = json.loads(data)
            result = self.exchange(context, body)
            reply = encoded(dict(result=result))
        except Exception as exc:
            # Raw Provider protocol errors are not absence, non-dispatch or
            # terminal proof. The Rust worker keeps its durable uncertainty.
            reply = encoded(dict(error=getattr(exc, 'code', 'PROVIDER_DELIVERY_UNKNOWN')))
        if len(reply) > MAX_RESPONSE:
            reply = encoded(dict(error='PROVIDER_RESPONSE_TOO_LARGE'))
        with self.lock:
            row['reply'], row['reply_digest'], row['state'] = reply, hashlib.sha256(reply).hexdigest(), 'READY'
        return dict(request_id=reference, accepted=True, size=len(reply), digest=row['reply_digest'])

    def exchange(self, context, body):
        # The existing paired channel checks current owner/Centre grants above.
        # Rust validates exact method/input identity before opening the Provider;
        # this process transports opaque bytes and never retries an unknown call.
        configured = []
        for value in (self.cfg.app_server.local_socket,
                      getattr(self.cfg.app_server, 'native_socket', None)):
            if value:
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / path
                if str(path) not in configured:
                    configured.append(str(path))
        if len(configured) != 1:
            fail('EXACT_DESKTOP_ENDPOINT_REQUIRED')
        root = Path(__file__).resolve().parents[2]
        explicit = os.environ.get('CLINX_PROVIDER_BINARY')
        candidates = ([Path(explicit)] if explicit else
                      [root / 'clinx-provider', root.parent / 'clinx-provider',
                       root.parent / 'Helpers/clinx-provider'])
        binary = next((p for p in candidates if p.is_absolute() and p.is_file()
                       and os.access(p, os.X_OK)), None)
        if binary is None:
            fail('RUST_PROVIDER_BINARY_REQUIRED')
        timeout = self.cfg.app_server.request_timeout_seconds if hasattr(
            self.cfg.app_server, 'request_timeout_seconds') else 3
        try:
            result = subprocess.run([str(binary), '--exchange', configured[0], str(timeout)],
                input=encoded(dict(context=context, body=body)), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=3 * timeout + 5, check=False)
            if result.returncode or len(result.stdout) > MAX_RESPONSE + 4096:
                fail('PROVIDER_DELIVERY_UNKNOWN')
            reply = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            fail('PROVIDER_DELIVERY_UNKNOWN')
        if not isinstance(reply, dict) or ('error' in reply) == ('result' in reply):
            fail('PROVIDER_DELIVERY_UNKNOWN')
        if 'error' in reply:
            fail(reply['error'])
        if not isinstance(reply['result'], dict):
            fail('PROVIDER_DELIVERY_UNKNOWN')
        return reply['result']


def paired_exchange(tls, registry, approvals, node_id, context, body):
    """One upload, one sealed submission, bounded readback; no automatic retry."""
    from clinx.nodes.node_runtime import intersect_scope
    record = registry.get(node_id)
    peer = tls.peer(node_id)
    scope = intersect_scope(registry.scope(node_id, record.user_scope) if record else None, approvals.get(node_id))
    if (record is None or not record.authorized or record.stale or record.state == 'OFFLINE'
            or record.public_key_fingerprint != peer['fingerprint'] or scope is None or not scope.allows('execution.authority')
            or context.get('host') != node_id or not scope.allows_project(context.get('cwd', ''))):
        fail('PROVIDER_SCOPE_DENIED')
    principal = context.get('node_principal')
    if principal and principal != dict(node_id=node_id, user_scope=scope.user_scope, fingerprint=peer['fingerprint']):
        fail('PROVIDER_PRINCIPAL_CONFLICT')
    context = dict(context, user_scope=scope.user_scope)
    wire = encoded(body)
    if not 0 < len(wire) <= MAX_REQUEST:
        fail('PROVIDER_REQUEST_TOO_LARGE')
    reference = uuid.uuid4().hex
    count = (len(wire) + CHUNK - 1) // CHUNK
    digest = hashlib.sha256(wire).hexdigest()
    def call(operation, **fields):
        # Recheck revocation on every chunk, not merely session creation.
        current = intersect_scope(registry.scope(node_id, record.user_scope), approvals.get(node_id))
        if current != scope:
            fail('PROVIDER_SCOPE_CHANGED')
        result = tls.client(node_id, record.endpoint, timeout=35).call(dict(operation=operation,
            protocol_version=NODE_PROTOCOL_VERSION, node_id=node_id, context=context,
            request_id=reference, **fields))
        if result.get('error_code'):
            fail(result['error_code'])
        if result.get('request_id') != reference:
            fail('PROVIDER_RESPONSE_IDENTITY_CONFLICT')
        return result
    reply = None
    for index in range(count):
        reply = call('provider.upload', index=index, count=count, digest=digest,
            data=base64.b64encode(wire[index*CHUNK:(index+1)*CHUNK]).decode())
    if reply.get('accepted') is not True or type(reply.get('size')) is not int or not 0 < reply['size'] <= MAX_RESPONSE:
        fail('PROVIDER_DELIVERY_UNKNOWN')
    data = bytearray()
    for index in range((reply['size'] + CHUNK - 1)//CHUNK):
        part = call('provider.read', index=index)
        if part.get('index') != index or part.get('size') != reply['size'] or part.get('digest') != reply['digest']:
            fail('PROVIDER_RESPONSE_IDENTITY_CONFLICT')
        chunk = base64.b64decode(part['data'], validate=True)
        if len(chunk) > CHUNK:
            fail('PROVIDER_RESPONSE_TOO_LARGE')
        data.extend(chunk)
    if len(data) != reply['size'] or hashlib.sha256(data).hexdigest() != reply['digest']:
        fail('PROVIDER_CHUNK_DIGEST_CONFLICT')
    return json.loads(data)


class ProviderGateway:
    def __init__(self, endpoint, transport_uid, tls, registry, approvals, *, observer_uid=None,
                 socket_gid=None):
        endpoint = Path(endpoint)
        parent = endpoint.parent.lstat()
        if (not endpoint.is_absolute() or endpoint.exists() or endpoint.is_symlink()
                or not endpoint.parent.is_dir() or parent.st_uid != os.getuid() or parent.st_mode & 0o022):
            fail('PROVIDER_GATEWAY_PATH_INVALID')
        if socket_gid is not None and os.geteuid() != 0 and socket_gid not in {os.getegid(), *os.getgroups()}:
            fail('PROVIDER_GATEWAY_GROUP_UNAVAILABLE')
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                # Linux credentials protect only this Centre-local IPC socket.
                # The Mac hop is authenticated by existing paired mTLS.
                _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != transport_uid and uid != observer_uid:
                    return
                self.request.settimeout(40)
                line = self.rfile.readline(MAX_REQUEST + 1)
                try:
                    if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
                        fail('PROVIDER_REQUEST_TOO_LARGE')
                    request = json.loads(line)
                    if uid != transport_uid and request.get('method') != 'workspace/inspect':
                        fail('PROVIDER_TRANSPORT_UID_REQUIRED')
                    result = paired_exchange(tls, registry, approvals, request['context']['host'],
                        request['context'], dict(method=request['method'], params=request['params']))
                except Exception as exc:
                    result = dict(error=getattr(exc, 'code', 'PROVIDER_DELIVERY_UNKNOWN'))
                self.wfile.write(encoded(result) + b'\n')
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(str(endpoint), Handler)
        self.endpoint = endpoint
        if socket_gid is not None:
            # Use an existing shared IPC group; peer UID checks still decide
            # which operations may be called. Never alter account membership.
            os.chown(endpoint, -1, socket_gid)
        endpoint.chmod(0o660)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.endpoint.unlink()


def gateway_request(endpoint, expected_uid, context, method, params):
    """Read-only observer IPC; remote node identity still comes from mTLS."""
    import stat
    endpoint = Path(endpoint)
    info, parent = endpoint.lstat(), endpoint.parent.lstat()
    if (not endpoint.is_absolute() or not stat.S_ISSOCK(info.st_mode) or info.st_uid != expected_uid
            or info.st_mode & 7 or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != expected_uid or parent.st_mode & 0o022):
        fail('PROVIDER_GATEWAY_UNTRUSTED')
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(40)
        peer.connect(str(endpoint))
        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != expected_uid:
            fail('PROVIDER_GATEWAY_UNTRUSTED')
        peer.sendall(encoded(dict(context=context, method=method, params=params)) + b'\n')
        with peer.makefile('rb') as reader:
            line = reader.readline(MAX_RESPONSE + 1)
    if len(line) > MAX_RESPONSE or not line.endswith(b'\n'):
        fail('PROVIDER_DELIVERY_UNKNOWN')
    reply = json.loads(line)
    if reply.get('error'):
        fail(reply['error'])
    if not isinstance(reply.get('result'), dict):
        fail('PROVIDER_DELIVERY_UNKNOWN')
    return reply['result']
