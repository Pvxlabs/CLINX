"""User-owned native reads for the existing Centre collector, never execution.

The Centre keeps its single durable stream and cannot open the operator's home.
Checkpoints are signed read cursors, not native or business state mutations.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from network_observation import compact
from node_protocol import NodeProtocolError
from observation_source import CodexObservationSource


class NativeCollectorBridge:
    def __init__(self, directory):
        self.directory = directory
        self.secret = os.urandom(32)
        self.source = None

    def scope(self):
        d = self.directory
        if d is None or not d.local_identity or not d.native_home:
            raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Local native observation unavailable')
        node = d.local_identity['node_id']
        scope = d._scope(node)
        if scope is None:
            raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Current native read grant required')
        if self.source is None:
            self.source = CodexObservationSource(d.native_home, node, native_writer_observation=True)
        return scope

    def binding(self, tid, scope):
        # A signed cursor crosses JSON; normalize tuples before comparison so
        # an unchanged grant remains equal after decoding its checkpoint.
        return json.loads(compact([tid, self.source.generation, scope.as_dict()]))

    def inventory(self, *, after=None, audit=False):
        scope = self.scope()
        if type(audit) is not bool:
            raise NodeProtocolError('INVALID_QUERY', 'Invalid audit mode')
        if after is not None:
            valid = (isinstance(after, str) and len(after) <= 128) if audit else (
                isinstance(after, list) and len(after) == 2 and type(after[0]) is int
                and after[0] >= 0 and isinstance(after[1], str) and len(after[1]) <= 128)
            if not valid:
                raise NodeProtocolError('INVALID_QUERY', 'Invalid native read position')
        rows, last, coverage = self.source.inventory(scope, after, audit=audit)
        if self.scope().as_dict() != scope.as_dict():
            raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Native grants changed during read')
        return dict(read_only=True, rows=rows, after=last, coverage=coverage,
                    generation=self.source.generation)

    def sample(self, tid, checkpoint=None):
        scope = self.scope()
        metadata = self.source.history.metadata(tid)
        if not metadata:
            raise NodeProtocolError('NATIVE_THREAD_NOT_FOUND', 'Exact native thread absent')
        if not scope.allows_project(metadata['cwd']):
            raise NodeProtocolError('PROJECT_SCOPE_DENIED', 'Native project outside read grants')
        state = {}
        binding = self.binding(tid, scope)
        if checkpoint is not None:
            try:
                if not isinstance(checkpoint, str) or len(checkpoint) > 12000:
                    raise ValueError()
                wire, signature = checkpoint.split('.')
                raw = base64.urlsafe_b64decode(wire + '=' * (-len(wire) % 4))
                if not hmac.compare_digest(hmac.new(self.secret, raw, hashlib.sha256).hexdigest(), signature):
                    raise ValueError()
                saved = json.loads(raw)
                if saved['binding'] != binding or saved['expires'] < time.time():
                    raise ValueError()
                state = saved['state']
                if not isinstance(state, dict):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise NodeProtocolError('NATIVE_COLLECTOR_CURSOR_RESET', 'Native checkpoint must be reread') from None
        events, state = self.source.sample(metadata, state)
        if self.scope().as_dict() != scope.as_dict() or self.binding(tid, scope) != binding:
            raise NodeProtocolError('SHARING_SCOPE_DENIED', 'Native source or grants changed during read')
        for event in events:
            event['title'] = event['title'][:256]
        raw = compact(dict(binding=binding, state=state, expires=time.time() + 3600)).encode()
        checkpoint = base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + hmac.new(
            self.secret, raw, hashlib.sha256).hexdigest()
        if len(checkpoint) > 12000:
            raise NodeProtocolError('NATIVE_HISTORY_BUDGET_EXHAUSTED', 'Native checkpoint budget exceeded')
        return dict(read_only=True, thread_id=tid, project=metadata['cwd'], events=events,
                    checkpoint=checkpoint, user_message_observed=bool(state.get('user_message_observed')))


class RemoteNativeSource:
    """Fixed loopback read endpoint; no caller-controlled URL, path or native root."""
    def __init__(self, credential_file):
        self.credential_file = Path(credential_file)
        self.unavailable_until = 0

    @classmethod
    def from_env(cls):
        value = os.environ.get('CLINX_NATIVE_OBSERVER_TOKEN_FILE')
        return cls(value) if value else None

    def read(self, path, params=None):
        # One failed source must not multiply its timeout by the number of
        # sessions in a collector tick and starve the managed projection.
        if time.monotonic() < self.unavailable_until:
            raise NodeProtocolError('NATIVE_OBSERVER_UNAVAILABLE', 'Native read unavailable')
        try:
            token = self.credential_file.read_text().strip()
            request = Request('http://127.0.0.1:8766/v2/native-collector/' + path + '?' +
                              urlencode(params or {}), headers={'Authorization': 'Bearer ' + token})
            with urlopen(request, timeout=8) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError()
            result = json.loads(data)
            if not isinstance(result, dict) or result.get('read_only') is not True:
                raise ValueError()
            return result
        except HTTPError as exc:
            code = 'NATIVE_OBSERVER_UNAVAILABLE'
            try:
                payload = json.loads(exc.read(4096))
                value = payload.get('error') if isinstance(payload, dict) else None
                if value in {'NATIVE_COLLECTOR_CURSOR_RESET', 'SHARING_SCOPE_DENIED',
                             'PROJECT_SCOPE_DENIED', 'NATIVE_THREAD_NOT_FOUND'}:
                    code = value
            except (ValueError, OSError):
                pass
            if code == 'NATIVE_OBSERVER_UNAVAILABLE':
                self.unavailable_until = time.monotonic() + 10
            raise NodeProtocolError(code, 'Native read refused') from None
        except (OSError, URLError, ValueError):
            self.unavailable_until = time.monotonic() + 10
            raise NodeProtocolError('NATIVE_OBSERVER_UNAVAILABLE', 'Native read unavailable') from None

    def inventory(self, scope, after=None, *, audit=False):
        params = dict(audit=int(audit))
        if after is None and not audit:
            # New work is discovered immediately; the independent rotating
            # audit backfills all older indexed sessions in bounded pages.
            after = [max(0, int(time.time()) - 300), '']
        if after is not None:
            params['after'] = compact(after)
        result = self.read('inventory', params)
        rows = result['rows']
        if not isinstance(rows, list) or len(rows) > 16:
            raise NodeProtocolError('INVALID_OBSERVATION', 'Native inventory budget exceeded')
        # Both the source user's grant and the Centre's current grant apply.
        return [r for r in rows if scope.allows_project(r['cwd'])], result['after'], result['coverage']

    def native_turns(self, metadata, state):
        tid = metadata['id']
        from thread_identity import parse_selector
        parse_selector(thread_id=tid)
        params = {'checkpoint': state['native_checkpoint']} if state.get('native_checkpoint') else {}
        try:
            result = self.read('sample/' + tid, params)
        except NodeProtocolError as exc:
            if exc.code != 'NATIVE_COLLECTOR_CURSOR_RESET' or not params:
                raise
            # A restarted read service has a new cursor key. Re-read only; never
            # infer a completion, resend input, or reconstruct native history.
            result = self.read('sample/' + tid)
        if result.get('thread_id') != tid or result.get('project') != metadata['cwd']:
            raise NodeProtocolError('NATIVE_OBSERVATION_IDENTITY_CONFLICT', 'Native read identity differs')
        events = result['events']
        if not isinstance(events, list) or not 1 <= len(events) <= 72:
            raise NodeProtocolError('INVALID_OBSERVATION', 'Native turn budget exceeded')
        if any(e.get('native_thread_id') != tid or e.get('project') != metadata['cwd']
               or e.get('task_ref') is not None or e.get('binding_evidence') != 'THREAD_UNBOUND'
               or e.get('turn', {}).get('execution_ref') is not None for e in events):
            raise NodeProtocolError('NATIVE_OBSERVATION_IDENTITY_CONFLICT', 'Native observation cannot assert authority')
        state['native_checkpoint'] = result['checkpoint']
        state['user_message_observed'] = result.get('user_message_observed') is True
        return [e['turn'] for e in events], events[-1]['coverage']
