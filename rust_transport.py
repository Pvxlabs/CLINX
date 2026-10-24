"""Existing native transport driven only by durable Rust send permits.

This process has the dedicated evidence UID. It owns no business database and
makes no admission, outcome, lease-release or finalization decisions.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import threading

from app_server import AppServerError, AppServerRemoteError
from native_provider import (
    cancel_native_turn,
    endpoints,
    existing_client,
)
from rust_authority import RustAuthorityError
from rust_integration import Commands, stable_key


def desktop_client(cfg, thread_id, cwd, *, host_tools=False):
    """One configured Desktop endpoint; no Provider discovery or fallback."""
    configured = endpoints(cfg)
    if len(configured) != 1:
        raise RustAuthorityError('EXACT_DESKTOP_ENDPOINT_REQUIRED')
    client = existing_client(configured[0], cfg.app_server.request_timeout_seconds, read_only=False)
    client.shared_desktop_daemon = True
    if not host_tools:
        client.shared_native_writer = True
        if thread_id:
            client.native_forwarding_thread_id = thread_id
        else:
            client.native_creation_cwd = cwd
    return client


class RustDispatcher:
    def __init__(self, cfg, commands=None):
        self.cfg = cfg
        self.commands = commands or Commands.from_env()
        self.host_runners = {}

    def initialize(self, client):
        client.initialize(client_name=self.cfg.app_server.client_name,
                          client_title=self.cfg.app_server.client_title,
                          client_version=self.cfg.app_server.client_version)

    def dispatch(self, execution_ref, *, resume=False):
        take = (self.commands.call('resume_dispatch', dict(execution_ref=execution_ref)) if resume else
                self.commands.call('take_dispatch', dict(execution_ref=execution_ref),
                                   key=stable_key('take', execution_ref)))
        if 'ticket' not in take:
            return take
        scope = {k: take[k] for k in ('execution_ref', 'epoch', 'ticket')}
        p = take['prepared']
        cwd = p['workspace_contract']['cwd']
        tid = take['thread_id']
        host_tools = p.get('execution_policy', {}).get('execution_surface') == 'HOST_EXECUTOR'
        production = not self.commands.client.request('health', {}, request_id='dispatch-runtime-identity')['qualification']
        # Every execution surface uses the configured Desktop daemon. The
        # logical endpoint remains stable for Rust receipt identity; deployment
        # publishes that alias to the Desktop socket, never another daemon.
        factory = desktop_client(self.cfg, tid, cwd, host_tools=host_tools)
        if host_tools:
            from host_executor import HostExecutor
            from rust_host import configure, host_policy
            dynamic_tools = [HostExecutor.dynamic_tool_spec(self.cfg.host_executor, host_policy(p))]
        recovered_empty = False
        with factory as client:
            self.initialize(client)
            if tid and host_tools and take.get('successor_required'):
                tid, thread = self.successor(client, scope, p, dynamic_tools)
            elif tid:
                try:
                    thread = client.thread_read(tid)
                except AppServerRemoteError as read_error:
                    handoff = take.get('handoff') or {}
                    if read_error.error.get('code') != -32600 or read_error.error.get('message') != 'thread not loaded: ' + tid:
                        raise
                    if host_tools and handoff.get('state') == 'COMMITTED':
                        try:
                            client.thread_turns_list(tid, limit=1, items_view='notLoaded')
                        except AppServerRemoteError as turns_error:
                            self.commands.call('successor_recover_empty', dict(**scope, thread_id=tid,
                                endpoint=client.provider_endpoint, read_error=read_error.error,
                                turns_error=turns_error.error))
                            tid, thread = self.successor(client, scope, p, dynamic_tools)
                        else:
                            thread = client.thread_resume(tid)
                    else:
                        thread = client.thread_resume(tid)
                else:
                    state = thread.get('status', {}).get('type')
                    if state in ('notLoaded', 'unloaded'):
                        thread = client.thread_resume(tid)
                    elif host_tools:
                        if state != 'idle':
                            raise RustAuthorityError('NATIVE_WRITER_NOT_IDLE')
                        exact_empty_handoff = (take.get('handoff') or {}).get('state') == 'COMMITTED' and (take.get('handoff') or {}).get('successor_thread_id') == tid
                        recovered_empty = exact_empty_handoff
                        if not recovered_empty:
                            thread = client.thread_resume(tid)
            else:
                thread = (client.thread_start(cwd=cwd, model=p['model'], ephemeral=False, dynamic_tools=dynamic_tools, thread_source='user')
                          if host_tools else client.thread_start(cwd=cwd, model=p['model'], ephemeral=False, thread_source='user'))
                tid = thread['id']
            allowed_states = ('idle',) if host_tools else ('idle', 'active')
            if thread.get('id') != tid or thread.get('status', {}).get('type') not in allowed_states:
                raise RustAuthorityError('NATIVE_WRITER_NOT_IDLE_OR_IDENTITY_CONFLICT')
            # Live native cwd selects execution context for both native and Host tasks.
            cwd = thread.get('cwd')
            bound = self.commands.call('bind_thread', dict(**scope, endpoint=client.provider_endpoint,
                thread_id=tid, cwd=cwd))
            if bound.get('workspace_contract'):
                p = dict(p, workspace_contract=bound['workspace_contract'])
            self.publish_title(client, thread, p)
            grant = self.commands.call('authorize_send', scope, key=stable_key('send', execution_ref))
            if grant.get('send_permitted') is not True:
                return grant
            client.native_forwarding_request_id = execution_ref
            if production:
                client.rust_authority_request_id = execution_ref
            if host_tools:
                self.host_runners[execution_ref] = configure(client, self.cfg, self.commands, p,
                    execution_ref, tid, self.recover_input)
                client.rust_authority_request_id = execution_ref
            # Shared-native admission and exact clientUserMessageId confirmation
            # remain the existing app-server transport's responsibility.
            turn = client.turn_start(tid, p['prompt'], cwd=cwd, model=p['model'],
                reasoning_effort=p['reasoning_effort'], full_access=not host_tools,
                **(dict(network_access=False, writable_roots=[cwd]) if host_tools else {}))
            if host_tools:
                if recovered_empty:
                    client.attach_dynamic_tool_turn(tid, turn.turn_id)
                    client.thread_resume(tid)
                client.configure_completion_handoff(thread_id=tid, turn_id=turn.turn_id,
                    handler=lambda _message: self.host_runners.pop(execution_ref, None))
                client.supervise_turn(tid, turn.turn_id)
        return self.recover_input(execution_ref)

    @staticmethod
    def publish_title(client, thread, prepared):
        # Native title publication also notifies Desktop's thread list. Never
        # overwrite a title already set by the user or replay model input.
        title = prepared.get('title')
        if thread.get('name') is None and isinstance(title, str) and 0 < len(title) <= 200:
            try:
                client.thread_name_set(thread['id'], title)
            except (AppServerError, OSError):
                logging.getLogger(__name__).warning('Desktop thread title publication unavailable')

    @staticmethod
    def exact_turn(client, thread_id, turn_id):
        cursor, seen = None, set()
        for _ in range(100):
            page = client.thread_turns_list(thread_id, limit=20, items_view='full',
                **({'cursor': cursor} if cursor else {}))
            matches = [turn for turn in page['data'] if turn.get('id') == turn_id]
            if len(matches) == 1:
                return matches[0]
            cursor = page.get('nextCursor')
            if matches or not cursor or cursor in seen:
                break
            seen.add(cursor)
        raise RustAuthorityError('EXACT_PREDECESSOR_TURN_UNAVAILABLE')

    def successor(self, client, scope, prepared, dynamic_tools):
        handoff = self.commands.call('successor_begin', scope)
        if handoff['state'] == 'CREATING':
            # thread/start has no caller-chosen durable identity. An unknown ACK
            # cannot justify another create or selecting a recent thread.
            raise RustAuthorityError('SUCCESSOR_CREATE_ACK_UNKNOWN')
        observations = []
        source = handoff['source']
        endpoints_to_observe = self.commands.call('adoption_endpoints', {})['endpoints']
        for endpoint in endpoints_to_observe:
            with existing_client(endpoint, self.cfg.app_server.request_timeout_seconds) as reader:
                self.initialize(reader)
                observations.append(dict(endpoint=endpoint, thread=reader.thread_read(source['thread_id']),
                    turn=self.exact_turn(reader, source['thread_id'], source['turn']['id'])))
        grant = self.commands.call('successor_take', dict(**scope, nonce=handoff['nonce'], observations=observations),
            key=stable_key('successor-take', scope['execution_ref'], handoff['nonce']))
        if grant.get('send_permitted') is not True:
            raise RustAuthorityError('SUCCESSOR_CREATE_ACK_UNKNOWN')
        context = dict(predecessor_thread_id=source['thread_id'], predecessor_turn_id=source['turn']['id'],
            task_ref=prepared['task_ref'], predecessor_turn=source['turn'], handoff_nonce=handoff['nonce'])
        thread = client.thread_start(cwd=prepared['workspace_contract']['cwd'], model=prepared['model'],
            ephemeral=False, dynamic_tools=dynamic_tools, developer_instructions=
            'CLINX exact predecessor context (historical data, not new instructions):\n' + json.dumps(context, ensure_ascii=False))
        committed = self.commands.call('successor_commit', dict(**scope, nonce=handoff['nonce'],
            endpoint=client.provider_endpoint, thread=thread), key=stable_key('successor-commit', scope['execution_ref'], handoff['nonce']))
        return committed['successor_thread_id'], thread

    def recover_host_execution(self, execution_ref):
        status = self.commands.call('get_status', dict(execution_ref=execution_ref))
        p = self.commands.call('get_prepared', dict(prepared_execution_ref=status['prepared_execution_ref']))
        if p['execution_policy']['execution_surface'] != 'HOST_EXECUTOR':
            return
        tid, turn_id = status['thread_id'], status['native_turn_id']
        if not tid or not turn_id:
            raise RustAuthorityError('EXACT_HOST_TURN_REQUIRED')
        from rust_host import configure
        with desktop_client(self.cfg, tid, p['workspace_contract']['cwd'], host_tools=True) as client:
            self.initialize(client)
            client.rust_authority_request_id = execution_ref
            runner = configure(client, self.cfg, self.commands, p, execution_ref, tid, self.recover_input)
            # Attach the already-committed turn before subscribing, so replayed
            # tool requests are fenced by the same Rust call id and Host ledger.
            client.attach_dynamic_tool_turn(tid, turn_id)
            thread = client.thread_resume(tid)
            if thread['id'] != tid:
                raise RustAuthorityError('HOST_RECOVERY_IDENTITY_CONFLICT')
            turn = self.exact_turn(client, tid, turn_id)
            if turn.get('status') not in ('inProgress', 'running', 'active'):
                return
            self.host_runners[execution_ref] = runner
            client.configure_completion_handoff(thread_id=tid, turn_id=turn_id,
                handler=lambda _message: self.host_runners.pop(execution_ref, None))
            client.supervise_turn(tid, turn_id)

    def recover_input(self, execution_ref):
        request = self.commands.call('input_begin', dict(execution_ref=execution_ref))
        tid = request['thread_id']
        matches = []
        with existing_client(request['endpoint'], self.cfg.app_server.request_timeout_seconds) as client:
            self.initialize(client)
            thread = client.thread_read(tid)
            # Exact user-message identity binds recovery to this execution. A
            # later cwd change cannot invalidate a historical input receipt.
            if thread.get('id') != tid:
                raise RustAuthorityError('NATIVE_INPUT_THREAD_CONFLICT')
            turns = client.thread_turns_list(tid, limit=5, items_view='notLoaded')['data']
            for turn in turns:
                cursor, seen = None, set()
                for _ in range(5):
                    page = client.thread_items_list(tid, turn_id=turn['id'], limit=100, sort_direction='asc',
                                                    **({'cursor': cursor} if cursor else {}))
                    for record in page['data']:
                        item = record.get('item', {})
                        if (record.get('turnId') == turn['id'] and item.get('type') == 'userMessage'
                                and item.get('clientId') == request['client_message_id']):
                            matches.append((turn['id'], item))
                    cursor = page.get('nextCursor')
                    if not cursor:
                        break
                    if cursor in seen:
                        raise RustAuthorityError('NATIVE_INPUT_SCAN_INCOMPLETE')
                    seen.add(cursor)
                else:
                    raise RustAuthorityError('NATIVE_INPUT_SCAN_INCOMPLETE')
        if len(matches) != 1:
            raise RustAuthorityError('NATIVE_INPUT_IDENTITY_UNPROVEN')
        turn_id, item = matches[0]
        return self.commands.call('input_commit', dict(execution_ref=execution_ref, nonce=request['nonce'],
            endpoint=request['endpoint'], thread_id=tid, turn_id=turn_id, item=item))

    def reconcile_execution(self, execution_ref):
        challenge = self.commands.call('observe_begin', dict(execution_ref=execution_ref))
        if 'nonce' not in challenge:
            return challenge
        tid, turn_id = challenge['thread_id'], challenge['turn_id']
        observations, exact_turns = [], []
        for endpoint in challenge['endpoints']:
            row = dict(endpoint=endpoint, state='UNKNOWN', thread_id=tid, turn_id=None, turn_status=None)
            try:
                with existing_client(endpoint, self.cfg.app_server.request_timeout_seconds) as client:
                    self.initialize(client)
                    thread = client.thread_read(tid)
                    if thread.get('id') != tid:
                        raise RustAuthorityError('THREAD_IDENTITY_CONFLICT')
                    status = thread.get('status', {})
                    row['state'] = status.get('type', 'UNKNOWN') if isinstance(status, dict) else status
                    cursor, seen, turn = None, set(), None
                    for _ in range(5):
                        page = client.thread_turns_list(tid, limit=20, items_view='full',
                                                        **({'cursor': cursor} if cursor else {}))
                        turn = next((t for t in page['data'] if t.get('id') == turn_id), None)
                        cursor = page.get('nextCursor')
                        if turn or not cursor or cursor in seen:
                            break
                        seen.add(cursor)
                    if turn:
                        row.update(turn_id=turn_id, turn_status=turn.get('status'))
                        exact_turns.append(turn)
            except (AppServerError, RustAuthorityError, OSError, ValueError, KeyError):
                row['state'] = 'UNKNOWN'
            observations.append(row)
        # Recover the exact original Host ACK from native history; never replay
        # a Host process or resend its response after transport loss.
        rows = self.commands.call('host_rows', dict(execution_ref=execution_ref))['rows']
        if exact_turns and all(t == exact_turns[0] for t in exact_turns):
            for item in exact_turns[0].get('items', []):
                unresolved = next((r for r in rows if r['call_id'] == item.get('id')
                                   and r.get('call_state') == 'RUNNING'
                                   and r.get('delivery_state') == 'FAILED'), None)
                if unresolved and item.get('success') is False:
                    # The authority validates the complete original native
                    # receipt and the narrowly defined pre-spawn guard.
                    try:
                        self.commands.call('host_reconcile_rejected', dict(execution_ref=execution_ref,
                            call_id=item['id'], endpoint=challenge['endpoints'][0], turn=exact_turns[0]),
                            key=stable_key('host-pre-spawn-reconciliation', execution_ref, item['id']))
                    except RustAuthorityError:
                        pass
                    else:
                        continue
                if any(r['call_id'] == item.get('id') and r['delivery_state'] != 'DELIVERED' for r in rows):
                    self.commands.call('host_ack', dict(execution_ref=execution_ref, call_id=item['id'],
                        message=dict(method='item/completed', params=dict(threadId=tid, turnId=turn_id, item=item))))
        # Conflicting terminal payloads cannot be used to release an allocation.
        turn = exact_turns[0] if exact_turns and all(t == exact_turns[0] for t in exact_turns) else {}
        return self.commands.call('observe_commit', dict(execution_ref=execution_ref, nonce=challenge['nonce'],
                                                         observations=observations, turn=turn))

    def adopt(self, adoption_ref):
        challenge = self.commands.call('adoption_begin', dict(adoption_ref=adoption_ref))
        if 'nonce' not in challenge:
            return challenge
        observations = []
        for endpoint in challenge['endpoints']:
            with existing_client(endpoint, self.cfg.app_server.request_timeout_seconds) as client:
                self.initialize(client)
                observed = dict(endpoint=endpoint, thread=client.thread_read(challenge['thread_id']))
                if challenge.get('exact_turn_id'):
                    observed['turn'] = self.exact_turn(client, challenge['thread_id'], challenge['exact_turn_id'])
                observations.append(observed)
        return self.commands.call('adoption_commit', dict(adoption_ref=adoption_ref,
            nonce=challenge['nonce'], observations=observations))

    def tick(self):
        results = []
        work = self.commands.call('work')
        for adoption in work.get('adoptions', []):
            try:
                result = self.adopt(adoption['adoption_ref'])
                results.append(dict(adoption_ref=adoption['adoption_ref'], status=result['adoption_status']))
            except (AppServerError, RustAuthorityError, OSError, ValueError, KeyError) as exc:
                results.append(dict(adoption_ref=adoption['adoption_ref'], status='BLOCKED', error=str(exc)[:120]))
        for e in work['executions']:
            eid = e['execution_ref']
            try:
                if e['dispatch_state'] == 'READY' and not e['cancel_requested']:
                    result = self.dispatch(eid)
                elif e['dispatch_state'] == 'TAKEN' and not e['cancel_requested']:
                    result = self.dispatch(eid, resume=True)
                elif e['dispatch_state'] == 'SENDING':
                    result = self.recover_input(eid)
                elif e['native_turn_id']:
                    if eid not in self.host_runners and not e['cancel_requested']:
                        self.recover_host_execution(eid)
                    if e['cancel_requested']:
                        if eid in self.host_runners:
                            self.host_runners[eid].cancel_execution(eid)
                        try:
                            grant = self.commands.call('take_cancel', dict(execution_ref=eid),
                                                       key=stable_key('cancel', eid))
                            if grant.get('send_permitted'):
                                cancel_native_turn(self.cfg, grant['thread_id'], grant['turn_id'])
                        except RustAuthorityError as exc:
                            if str(exc) != 'CANCEL_NOT_AVAILABLE':
                                raise
                    result = self.reconcile_execution(eid)
                else:
                    result = self.commands.call('fail_before_send', dict(execution_ref=eid,
                        reason='TRANSPORT_RESTART_BEFORE_SEND'), key=stable_key('pre-send-recovery', eid))
                results.append(dict(execution_ref=eid, status=result.get('status', 'PENDING')))
            except (AppServerError, RustAuthorityError, OSError, ValueError, KeyError) as exc:
                if str(exc) == 'SUCCESSOR_CREATE_ACK_UNKNOWN':
                    results.append(dict(execution_ref=eid, status='BLOCKED', error=str(exc)))
                    continue
                # Rust checks its durable send boundary. A failed Python RPC is
                # never accepted as proof that a Provider turn was not sent.
                try:
                    reason = str(exc).split(':', 1)[0] if isinstance(exc, RustAuthorityError) else type(exc).__name__
                    if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', reason):
                        reason = type(exc).__name__
                    closed = self.commands.call('fail_before_send', dict(execution_ref=eid,
                        reason=reason), key=stable_key('pre-send-failure', eid))
                    results.append(dict(execution_ref=eid, status=closed['status']))
                    continue
                except RustAuthorityError:
                    pass
                results.append(dict(execution_ref=eid, status='BLOCKED', error=str(exc).split(':')[0][:120]))
        return results


def run(cfg, *, once=False):
    dispatcher = RustDispatcher(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        print(json.dumps(dict(authority='RUST', results=dispatcher.tick())), flush=True)
        if once:
            return 0
        stop.wait(2)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    from pathlib import Path

    from bridge import BridgeConfig
    return run(BridgeConfig.load(Path(args.config)), once=args.once)


if __name__ == '__main__':
    raise SystemExit(main())
