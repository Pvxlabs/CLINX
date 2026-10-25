"""Codex JSON-RPC client for remaining observation and Node relay paths.

Rust owns process/Unix transports, WebSocket/JSONL, request validation,
writer admission, native-input evidence and subscription lifecycle. Python
keeps the existing DTOs, exception identity, signatures and explicit injected
IO seams used by qualification. No Desktop IPC or thread discovery is done
here; callers supply the exact durable thread identity.
"""

from __future__ import annotations

import dataclasses
import os
import select
import subprocess
import time
import threading
import uuid
from typing import Any, Callable, Protocol


class AppServerError(RuntimeError):
    """Base error for transport, protocol, and app-server failures."""


class AppServerTransportError(AppServerError):
    pass


class AppServerProtocolError(AppServerError):
    pass


class AppServerSharedWriterError(AppServerProtocolError):
    code = "NATIVE_SHARED_WRITER_NOT_QUALIFIED"
    side_effect = "NONE"


class AppServerSandboxPolicyError(AppServerProtocolError):
    """The requested per-turn sandbox policy could not be honored."""


class AppServerRemoteError(AppServerError):
    def __init__(self, method: str, error: dict[str, Any]):
        message = error.get("message", "unknown app-server error")
        code = error.get("code")
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"{method} failed{suffix}: {message}")
        self.method = method
        self.error = error


class AppServerWriterConflict(AppServerRemoteError):
    """An explicit resume rejection, before any turn/start request."""
    code = "NATIVE_ACTIVE_WRITER"
    side_effect = "NONE"


def _process_group_id(process: Any) -> int | None:
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return None


def _terminate_process_group(process: Any) -> int | None:
    """Terminate a proxy wrapper and return its group id for final cleanup."""
    pgid = _process_group_id(process)
    if pgid is not None:
        try:
            os.killpg(pgid, 15)
            return pgid
        except (OSError, ProcessLookupError):
            pass
    terminate = getattr(process, "terminate", None)
    if terminate:
        terminate()
    return pgid


def _kill_process_group(process: Any, pgid: int | None = None) -> None:
    pgid = pgid if pgid is not None else _process_group_id(process)
    if pgid is not None:
        try:
            os.killpg(pgid, 9)
            return
        except (OSError, ProcessLookupError):
            pass
    kill = getattr(process, "kill", None)
    if kill:
        kill()


class JSONRPCTransport(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...

    def receive(self, timeout_seconds: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


def transport_lifecycle_state(transport: Any) -> str:
    """Encode local observations; Rust decides lifecycle availability."""
    missing = object()
    def callback(event):
        args = event['arguments']['arguments']
        target = getattr(transport, '_process', None) if args.get('process') else transport
        value = getattr(target, args['name'], missing)
        is_callable = callable(value)
        try:
            if args.get('call') and is_callable:
                value = value()
        except Exception:
            return {'result': {'failed': True}}
        return {'result': dict(present=value is not missing, callable=is_callable, none=value is None,
                value=value if type(value) in (str, bool, int, float) else None)}
    return _provider_value(_provider_with_callback('api.lifecycle', callback))


class ProcessByteTransport:
    """Rust-owned process; custom popen is an explicit injected IO seam."""
    _text = False

    def __init__(self, command, *, timeout_seconds=30.0, popen=subprocess.Popen):
        if not command:
            raise AppServerTransportError('app-server command is required')
        self.command, self.timeout_seconds = command, timeout_seconds
        self._popen, self._process, self._session = popen, None, None

    def connect(self):
        if self._session is not None or self._process is not None:
            return
        if self._popen is subprocess.Popen:
            self._session = _provider_value(_provider_call('wire.process_connect', command=self.command))
            return
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      shell=False, text=self._text, bufsize=1 if self._text else 0, start_new_session=True)
        if self._text:
            kwargs['encoding'] = 'utf-8'
        try:
            self._process = self._popen(list(self.command), **kwargs)
        except OSError as exc:
            raise AppServerTransportError(f'failed to start app-server transport: {exc}') from exc

    def send_bytes(self, payload):
        if self._session is not None:
            return _provider_value(_provider_call('wire.send_bytes', process_session=self._session, payload=payload.hex()))
        if self._process is None or self._process.stdin is None:
            raise AppServerTransportError('app-server transport is not connected')
        try:
            self._process.stdin.write(payload.decode('utf-8') if self._text else payload)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerTransportError(f'failed to write app-server bytes: {exc}') from exc

    def receive_bytes(self, length, timeout_seconds):
        if self._session is not None:
            return bytes.fromhex(_provider_value(_provider_call('wire.receive_bytes', process_session=self._session, length=length, timeout=timeout_seconds)))
        if length < 0:
            raise AppServerTransportError('byte read length must not be negative')
        if not length:
            return b''
        if self._process is None or self._process.stdout is None:
            raise AppServerTransportError('app-server transport is not connected')
        stream, result, deadline = self._process.stdout, bytearray(), time.monotonic() + timeout_seconds
        while len(result) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTransportError(f'timed out waiting for {length} app-server bytes')
            try:
                fd = stream.fileno()
            except (AttributeError, OSError):
                fd = None
            if fd is not None:
                if not select.select([fd], [], [], remaining)[0]:
                    raise AppServerTransportError(f'timed out waiting for {length} app-server bytes')
                chunk = os.read(fd, length - len(result))
            else:
                chunk = stream.read(length - len(result))
            if not chunk:
                code = self._process.poll()
                raise AppServerTransportError('app-server byte transport closed' + (f' (exit {code})' if code is not None else ''))
            result.extend(chunk.encode('utf-8') if isinstance(chunk, str) else chunk)
        return bytes(result)

    def transport_lifecycle_state(self):
        if self._session is not None:
            return _provider_value(_provider_call('wire.process_lifecycle', process_session=self._session))
        if self._process is None:
            return 'unavailable'
        return 'available' if self._process.poll() is None else 'unavailable'

    def close(self):
        session, self._session = self._session, None
        if session is not None:
            _provider_value(_provider_call('wire.process_close', process_session=session))
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        pgid = _terminate_process_group(process)
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _kill_process_group(process, pgid)


def _wire_operation(transport, operation, **arguments):
    session = getattr(transport, '_session', None)
    if session is not None:
        return _provider_value(_provider_call('wire.' + operation, process_session=session, **arguments))
    errors = []
    def callback(event):
        call = event['arguments']
        payload = call['arguments']
        try:
            if call['operation'] == 'send_bytes':
                value = transport.send_bytes(bytes.fromhex(payload['payload']))
            else:
                value = transport.receive_bytes(payload['length'], payload['timeout']).hex()
            return {'result': value}
        except Exception as exc:
            errors.append(exc)
            return {'error': {'kind': 'protocol' if isinstance(exc, AppServerProtocolError) else 'transport', 'message': str(exc)}}
    result = _provider_with_callback('wire.' + operation, callback, **arguments)
    if result.get('error') is not None and errors:
        raise errors[-1]
    return _provider_value(result)


class ProcessStdioTransport(ProcessByteTransport):
    """Rust JSONL codec; the public API preserves explicitly injected streams."""
    _text = True

    def send(self, message):
        return _wire_operation(self, 'send_line', message=message)

    def receive(self, timeout_seconds):
        return _wire_operation(self, 'receive_line', timeout=timeout_seconds)


class WebSocketStdioTransport:
    """Rust WebSocket codec over a Rust process or explicitly injected bytes."""
    _MAX_FRAME_BYTES = 16 * 1024 * 1024

    def __init__(self, command, *, timeout_seconds=30.0, popen=subprocess.Popen, websocket_path='/'):
        if not websocket_path.startswith('/'):
            raise AppServerTransportError("websocket path must start with '/'")
        self.command, self.timeout_seconds, self.websocket_path = command, timeout_seconds, websocket_path
        self._byte_transport = ProcessByteTransport(command, timeout_seconds=timeout_seconds, popen=popen)
        self._connected = False

    def connect(self):
        if not self._connected:
            self._byte_transport.connect()
            _wire_operation(self._byte_transport, 'handshake', path=self.websocket_path, timeout=self.timeout_seconds)
            self._connected = True

    def _read_http_header(self):
        return bytes.fromhex(_wire_operation(self._byte_transport, 'header', timeout=self.timeout_seconds))

    def send(self, message):
        if not self._connected:
            raise AppServerTransportError('app-server websocket is not connected')
        return _wire_operation(self._byte_transport, 'send', message=message)

    def _send_frame(self, payload, *, opcode):
        return _wire_operation(self._byte_transport, 'send_frame', payload=payload.hex(), opcode=opcode)

    def _receive_frame(self, timeout_seconds):
        op, payload, fin = _wire_operation(self._byte_transport, 'frame', timeout=timeout_seconds)
        return op, bytes.fromhex(payload), fin

    def receive(self, timeout_seconds):
        return _wire_operation(self._byte_transport, 'receive', timeout=timeout_seconds)

    def close(self):
        self._connected = False
        self._byte_transport.close()


class SSHStdioTransport(WebSocketStdioTransport):
    """Run a configured remote app-server command over an SSH stdio channel."""

    def __init__(
        self,
        ssh_alias: str,
        remote_command: tuple[str, ...],
        *,
        ssh_binary: str = "ssh",
        ssh_args: tuple[str, ...] = ("-T",),
        timeout_seconds: float = 30.0,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ):
        if not ssh_alias:
            raise AppServerTransportError("SSH alias is required")
        if not remote_command:
            raise AppServerTransportError("remote app-server command is required")
        self.ssh_alias = ssh_alias
        self.remote_command = remote_command
        self.ssh_binary = ssh_binary
        self.ssh_args = ssh_args
        super().__init__(
            (ssh_binary, *ssh_args, ssh_alias, *remote_command),
            timeout_seconds=timeout_seconds,
            popen=popen,
        )


class LocalStdioTransport(WebSocketStdioTransport):
    """Run the local app-server proxy command over a stdio byte tunnel."""


# A single private Rust worker multiplexes existing-provider connections. It
# never restarts a failed connection or retries an unknown request.
_provider_lock = threading.Lock()
_provider_worker = None
_provider_callbacks = {}


def _provider_callback(event):
    args = event['arguments']
    with _provider_lock:
        client = _provider_callbacks.get(args['token'])
        if client is None:
            raise AppServerTransportError('PROVIDER_CALLBACK_UNAVAILABLE')
    if callable(client):
        return client(event)
    with _provider_lock:
        if args['operation'] == 'consume_creation':
            expected = args['arguments']['cwd']
            if getattr(client, 'native_creation_cwd', None) != expected:
                return {'error': {'kind': 'shared_writer', 'message': 'Native creation permit already consumed'}}
            # A lost Rust response must not leave a second creation permit.
            client.native_creation_cwd = None
            return {'result': None}
    try:
        operation, payload = args['operation'], args['arguments']
        if operation == 'send':
            client.transport.send(payload['message'])
            value = None
        elif operation == 'receive':
            value = client._receive(payload['timeout'])
        elif operation == 'mark':
            mark = getattr(client.transport, 'mark_request_result', None)
            if callable(mark):
                mark(payload['id'], payload['outcome'])
            value = None
        else:
            raise AppServerProtocolError('PROVIDER_CALLBACK_INVALID')
        return {'result': value}
    except Exception as exc:
        client._provider_callback_error = exc
        kind = 'protocol' if isinstance(exc, AppServerProtocolError) else 'transport'
        return {'error': {'kind': kind, 'message': str(exc)}}


def _provider_call(operation, **arguments):
    global _provider_worker
    from clinx.authority.rust_authority import RustAuthorityError
    with _provider_lock:
        if _provider_worker is None:
            import atexit
            from clinx.nodes.network_worker import Worker
            _provider_worker = Worker(callback=_provider_callback)
            atexit.register(_provider_worker.close)
        worker = _provider_worker
    try:
        return worker.call('provider.client.' + operation,
            _wait_timeout=max(125, float(arguments.get('timeout', 0)) + 5), **arguments)
    except RustAuthorityError as exc:
        if str(exc) == 'PROVIDER_SOCKET_NOT_PRESENT':
            raise FileNotFoundError(str(exc)) from exc
        if str(exc) == 'PROVIDER_SOCKET_PERMISSION_DENIED':
            raise PermissionError(str(exc)) from exc
        kind = AppServerProtocolError if str(exc) in {
            'PROVIDER_HANDSHAKE_INVALID', 'PROVIDER_FRAME_INVALID',
            'PROVIDER_PROTOCOL_INVALID', 'PROVIDER_MESSAGE_TOO_LARGE',
        } else AppServerTransportError
        raise kind(str(exc)) from exc
    except (OSError, TimeoutError) as exc:
        raise AppServerTransportError(str(exc)) from exc


def _provider_with_callback(operation, callback, **arguments):
    token = str(uuid.uuid4())
    with _provider_lock:
        _provider_callbacks[token] = callback
    try:
        return _provider_call(operation, callback_token=token, **arguments)
    finally:
        with _provider_lock:
            _provider_callbacks.pop(token, None)


def _provider_value(result, method='transport'):
    error = result.get('error')
    if error is not None:
        if error['kind'] == 'remote':
            raise AppServerRemoteError(method, error['remote'])
        cls = {'protocol': AppServerProtocolError, 'shared_writer': AppServerSharedWriterError}.get(
            error['kind'], AppServerTransportError)
        raise cls(error['message'])
    return result['value']


class UnixSocketTransport(WebSocketStdioTransport):
    """Existing daemon socket owned by Rust; no process or fallback endpoint.

    The byte surface only supports injected socket fixtures from the original
    protocol qualification. Normal connections never construct a Python socket.
    """
    class Bytes:
        def __init__(self):
            self.sock = None

        def receive_bytes(self, size, timeout_seconds):
            data, deadline = bytearray(), time.monotonic() + timeout_seconds
            while len(data) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerTransportError('existing provider receive timed out')
                self.sock.settimeout(remaining)
                try:
                    chunk = self.sock.recv(size - len(data))
                except TimeoutError as exc:
                    raise AppServerTransportError('existing provider receive timed out') from exc
                if not chunk:
                    raise AppServerTransportError('existing provider disconnected')
                data.extend(chunk)
            return bytes(data)

        def close(self):
            if self.sock is not None:
                self.sock.close()
                self.sock = None

    def __init__(self, path, *, timeout_seconds=3):
        self.path, self.timeout_seconds = str(path), timeout_seconds
        self.command, self._session, self._connected = (), None, False
        self._byte_transport = self.Bytes()

    def connect(self):
        if self._session is None:
            self._session = _provider_call('connect', endpoint=self.path, timeout=self.timeout_seconds)['session']
            self._connected = True

    def send(self, message):
        if self._session is None:
            raise AppServerTransportError('app-server transport is not connected')
        _provider_value(_provider_call('send', session=self._session, message=message, timeout=self.timeout_seconds))

    def receive(self, timeout_seconds):
        if self._byte_transport.sock is not None:
            return super().receive(timeout_seconds)
        if self._session is None:
            raise AppServerTransportError('app-server transport is not connected')
        return _provider_value(_provider_call('receive', session=self._session, timeout=timeout_seconds))

    def transport_lifecycle_state(self):
        if self._session is None:
            return 'unavailable'
        return _provider_call('lifecycle', session=self._session)

    def local_peer_uid(self):
        if self._session is None:
            raise AppServerTransportError('app-server transport is not connected')
        return _provider_call('peer_uid', session=self._session)

    def close(self):
        session, self._session, self._connected = self._session, None, False
        self._byte_transport.close()
        if session is not None:
            try:
                _provider_call('close', session=session)
            except AppServerError:
                pass


@dataclasses.dataclass(frozen=True)
class InitializeInfo:
    server_name: str | None
    server_version: str | None
    user_agent: str | None


@dataclasses.dataclass(frozen=True)
class TurnStartInfo:
    turn_id: str
    model: str | None
    reasoning_effort: str | None


@dataclasses.dataclass(frozen=True)
class ModelCapability:
    """The executable provider model and its accepted reasoning values."""

    model_id: str
    reasoning_efforts: tuple[str, ...]
    default_reasoning_effort: str | None


class ModelCapabilityError(AppServerProtocolError):
    pass


def _parse_reasoning_efforts(value: Any, *, model_id: str) -> tuple[str, ...]:
    return tuple(_provider_api(None, 'efforts', value=value, model_id=model_id))


def _provider_api(client, operation, **arguments):
    """Preserve Python API/exception objects around the Rust request contract."""
    exceptions = []
    def callback(event):
        call = event['arguments']
        op, payload = call['operation'], call['arguments']
        try:
            if op == 'request':
                value = client._request(payload['method'], payload['params'])
            elif op == 'model_list':
                value = client.model_list()
            elif op == 'send':
                client.transport.send(payload['message'])
                value = None
            elif op == 'confirm':
                client._confirm_native_input(**payload)
                value = None
            elif op == 'close':
                client.transport.close()
                value = None
            else:
                raise AppServerProtocolError('PROVIDER_API_CALLBACK_INVALID')
            return {'result': value}
        except Exception as exc:
            exceptions.append(exc)
            error = dict(kind='callback', index=len(exceptions)-1, message=str(exc), **{'class': type(exc).__name__})
            if isinstance(exc, AppServerRemoteError):
                error.update(remote=exc.error, method=exc.method)
            return {'error': error}
    attrs = vars(client) if client is not None else {}
    state = dict(owned_thread_ids=list(attrs.get('_owned_thread_ids', ())),
                 owned_turns=attrs.get('_owned_turns', {}), writer_release=attrs.get('writer_release', {}))
    policy = {name: attrs.get(name) for name in ('native_forwarding_thread_id', 'native_creation_cwd',
              'shared_native_writer', 'read_only_observer', 'rust_authority_request_id', 'native_forwarding_request_id')}
    result = _provider_with_callback('api.' + operation, callback, arguments=arguments, state=state, policy=policy)
    if client is not None:
        state = result['state']
        patch = result['patch']
        owned = attrs.setdefault('_owned_thread_ids', set())
        owned.update(patch['add_threads'])
        owned.difference_update(patch['remove_threads'])
        attrs.setdefault('_owned_turns', {}).update(patch['owned_turns'])
        attrs.setdefault('writer_release', {}).update(patch['writer_release'])
        if 'native_forwarding_thread_id' in state:
            client.native_forwarding_thread_id = state['native_forwarding_thread_id']
    error = result.get('error')
    if error is not None:
        if error['kind'] == 'callback':
            raise exceptions[error['index']]
        if error['kind'] == 'writer_conflict':
            raise AppServerWriterConflict(error['method'], error['remote']) from exceptions[error['index']]
        cls = {'protocol': AppServerProtocolError, 'model': ModelCapabilityError,
               'sandbox': AppServerSandboxPolicyError, 'shared_writer': AppServerSharedWriterError}.get(error['kind'], AppServerTransportError)
        raise cls(error['message']) from (exceptions[-1] if exceptions else None)
    return result['value']


class CodexAppServerClient:
    """Synchronous JSON-RPC client with notification/event draining."""

    def __init__(
        self,
        transport: JSONRPCTransport,
        timeout_seconds: float = 30.0,
        *,
        max_received_events: int = 256,
        max_method_history: int = 256,
    ):
        _provider_api(None, 'validate_capacity', max_received_events=max_received_events, max_method_history=max_method_history)
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.events: list[str] = []
        # Raw notifications are retained only until an explicit adapter or
        # caller drains them.  The existing V1 callers continue to use the
        # method-name list above; this bounded observation surface lets an
        # explicit provider adapter preserve event correlation without making
        # wire dictionaries part of the provider-neutral contract.
        self.received_events: list[dict[str, Any]] = []
        self.max_received_events = max_received_events
        self.max_method_history = max_method_history
        self._received_event_overflow = False
        self.initialize_info: InitializeInfo | None = None
        self.provider_endpoint: str | None = None
        self._owned_thread_ids: set[str] = set()
        self._owned_turns: dict[str, str] = {}
        self.writer_release: dict[str, str] = {}


    def _record_event(self, message: dict[str, Any]) -> None:
        self._protocol('record', message=message)

    def _receive(self, timeout_seconds):
        return self.transport.receive(timeout_seconds)

    def __enter__(self) -> "CodexAppServerClient":
        connect = getattr(self.transport, "connect", None)
        if connect:
            connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        _provider_api(self, 'close')

    def _release_owned_subscription(self, thread_id: str) -> None:
        _provider_api(self, 'release', thread_id=thread_id)


    def _send_server_response(self, request: dict[str, Any]) -> None:
        # Host dynamic tools are owned by Rust. Python observation/relay
        # connections never answer Desktop approvals or dynamic-tool calls.
        self._record_event(request)


    def _protocol(self, operation, **arguments):
        token = str(uuid.uuid4())
        arguments.update(callback_token=token, state=dict(events=self.events,
            received_events=self.received_events, overflow=self._received_event_overflow,
            max_received_events=self.max_received_events, max_method_history=self.max_method_history))
        if isinstance(self.transport, UnixSocketTransport) and self.transport._session is not None:
            arguments['session'] = self.transport._session
        with _provider_lock:
            _provider_callbacks[token] = self
        self._provider_callback_error = None
        try:
            result = _provider_call(operation, **arguments)
            state = result['state']
            self.events[:] = state['events']
            self.received_events[:] = state['received_events']
            self._received_event_overflow = state['overflow']
            try:
                if result.get('error') is not None and self._provider_callback_error is not None:
                    raise self._provider_callback_error
                return _provider_value(result, arguments.get('request', {}).get('method', operation))
            except Exception as exc:
                if result.get('drained_events'):
                    exc.events = tuple(result['drained_events'])
                raise
        finally:
            with _provider_lock:
                _provider_callbacks.pop(token, None)

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request = {'id': str(uuid.uuid4()), 'method': method}
        if params is not None:
            request['params'] = params
        policy = {name: getattr(self, name, None) for name in (
            'native_forwarding_thread_id', 'native_creation_cwd', 'shared_native_writer', 'read_only_observer')}
        policy['workspace_metadata_request'] = getattr(self, '_workspace_metadata_request', None)
        return self._protocol('request', request=request, timeout=self.timeout_seconds, policy=policy)

    def drain_events(self, *, max_events: int = 32, timeout_seconds: float = 0.0) -> list[dict[str, Any]]:
        """Rust drains bounded events without answering tools or approvals."""
        return self._protocol('drain', max_events=max_events, timeout=timeout_seconds)

    def initialize(
        self,
        *,
        client_name: str,
        client_title: str,
        client_version: str,
    ) -> InitializeInfo:
        self.initialize_info = InitializeInfo(**_provider_api(self, 'initialize', client_name=client_name, client_title=client_title, client_version=client_version))
        return self.initialize_info

    def thread_read(self, thread_id: str) -> dict[str, Any]:
        # The default read may include the complete turn history.  Adoption
        # only needs thread identity metadata, so explicitly suppress turns
        # to keep large durable conversations out of the bridge process.
        return _provider_api(self, 'thread_read', thread_id=thread_id)

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        return _provider_api(None, 'bounded_limit', limit=limit)

    @staticmethod
    def _paged_result(result: Any, method: str) -> dict[str, Any]:
        return _provider_api(None, 'paged_result', result=result, method=method)

    def thread_turns_list(
        self,
        thread_id: str,
        *,
        limit: int,
        cursor: str | None = None,
        sort_direction: str = "desc",
        items_view: str = "summary",
    ) -> dict[str, Any]:
        return _provider_api(self, 'thread_turns_list', thread_id=thread_id, limit=limit, cursor=cursor, sort_direction=sort_direction, items_view=items_view)

    def thread_items_list(
        self,
        thread_id: str,
        *,
        turn_id: str | None = None,
        limit: int,
        cursor: str | None = None,
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        return _provider_api(self, 'thread_items_list', thread_id=thread_id, turn_id=turn_id, limit=limit, cursor=cursor, sort_direction=sort_direction)

    def thread_list(self, *, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        return _provider_api(self, 'thread_list', cursor=cursor, limit=limit)

    def thread_loaded_list(self) -> dict[str, Any]:
        return _provider_api(self, 'thread_loaded_list')

    def thread_start(
        self,
        *,
        cwd: str,
        model: str | None = None,
        project_id: str | None = None,
        sandbox: str | None = None,
        ephemeral: bool = False,
        thread_source: str | None = None,
        dynamic_tools: list[dict[str, Any]] | None = None,
        developer_instructions: str | None = None,
    ) -> dict[str, Any]:
        return _provider_api(self, 'thread_start', cwd=cwd, model=model, project_id=project_id, sandbox=sandbox, ephemeral=ephemeral, thread_source=thread_source, dynamic_tools=dynamic_tools, developer_instructions=developer_instructions)

    def thread_resume(
        self, thread_id: str, *, dynamic_tools: list[dict[str, Any]] | None = None,
        developer_instructions: str | None = None
    ) -> dict[str, Any]:
        return _provider_api(self, 'thread_resume', thread_id=thread_id, dynamic_tools=dynamic_tools, developer_instructions=developer_instructions)

    def thread_name_set(self, thread_id: str, name: str) -> None:
        return _provider_api(self, 'thread_name_set', thread_id=thread_id, name=name)

    def thread_inject_items(self, thread_id: str, items: list[dict[str, Any]]) -> None:
        return _provider_api(self, 'thread_inject_items', thread_id=thread_id, items=items)

    def model_list(self) -> dict[str, Any]:
        return _provider_api(self, 'model_list')

    def resolve_model(self, requested: str | None, reasoning_effort: str | None) -> tuple[str, str | None]:
        return tuple(_provider_api(self, 'resolve_model', requested=requested, reasoning_effort=reasoning_effort))

    def turn_start(
        self,
        thread_id: str,
        prompt: str,
        *,
        cwd: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        network_access: bool | None = None,
        writable_roots: list[str] | None = None,
        full_access: bool = False,
    ) -> TurnStartInfo:
        return TurnStartInfo(**_provider_api(self, 'turn_start', thread_id=thread_id, prompt=prompt, cwd=cwd, model=model, reasoning_effort=reasoning_effort, approval_policy=approval_policy, network_access=network_access, writable_roots=writable_roots, full_access=full_access, request_key=str(uuid.uuid4())))

    def _confirm_native_input(self, thread_id, turn, client_id, prompt):
        self.native_forwarding_receipt = self._protocol('confirm', thread_id=thread_id,
            turn=turn, client_id=client_id, prompt=prompt, timeout=self.timeout_seconds)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> bool:
        return _provider_api(self, 'turn_interrupt', thread_id=thread_id, turn_id=turn_id)


def _thread_result(result: Any, method: str) -> dict[str, Any]:
    return _provider_api(None, 'thread_result', result=result, method=method)


def extract_version(value: str | None) -> tuple[int, int, int] | None:
    value = _provider_api(None, 'version', value=value)
    return tuple(int(part) for part in value) if value is not None else None


def versions_compatible(expected: str | None, actual: str | None) -> bool:
    versions = [extract_version(value) for value in (expected, actual)]
    encoded = [list(map(str, value)) if value is not None else None for value in versions]
    return _provider_api(None, 'versions_compatible', expected=encoded[0], actual=encoded[1])
