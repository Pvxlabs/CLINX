"""Centre read projections from Rust; native history stays on its existing host."""
from pathlib import Path

from observation_source import CodexObservationSource, order
from rust_authority import RustAuthorityError
from rust_integration import Commands


class RustObservationSource(CodexObservationSource):
    def __init__(self, root, node_id, owner_host=None, provider_config=None, commands=None, native_source=None):
        self.rust_only = root is None
        self.native_source = native_source
        self.managed_inventory_unavailable = False
        super().__init__(root or Path('/nonexistent'), node_id, owner_host=owner_host, provider_config=provider_config)
        self.commands = commands or Commands.from_env()

    @property
    def generation(self):
        if self.rust_only:
            return self.commands.client.request('health', {}, request_id='observation-generation')['authority_id'][:32]
        return super().generation

    def inventory(self, scope, after=None, *, audit=False):
        if self.native_source:
            from node_protocol import NodeProtocolError
            try:
                return self.native_source.inventory(scope, after, audit=audit)
            except NodeProtocolError as exc:
                # Native source outages must remain visible without hiding the
                # independent Rust managed projection or manufacturing history.
                return [], after, exc.code
        if self.rust_only:
            return [], None, 'RUST_MANAGED_ONLY'
        return super().inventory(scope, after, audit=audit)

    def native_turns(self, metadata, state):
        if self.native_source and metadata.get('history_mode') != 'canonical':
            from node_protocol import NodeProtocolError
            try:
                turns, coverage = self.native_source.native_turns(metadata, state)
                state.pop('native_observation_error', None)
                return turns, coverage
            except NodeProtocolError as exc:
                state['native_observation_error'] = exc.code
                return [], exc.code
        return super().native_turns(metadata, state)

    def canonical_inventory(self, scope, after=''):
        try:
            tasks = self.commands.call('observation_inventory', dict(host=self.owner_host, after=after))['tasks']
        except RustAuthorityError as exc:
            if str(exc) != 'RESPONSE_CAPACITY_EXCEEDED':
                raise
            # Independent native reads must not stall behind an oversized managed
            # projection. Retry on the next normal tick without authority writes, and
            # explicitly report incomplete managed coverage on sampled events.
            self.managed_inventory_unavailable = True
            return []
        self.managed_inventory_unavailable = False
        return [dict(id=t['thread_id'], cwd=t['workspace_contract']['cwd'], name=t['title'],
                     history_mode='canonical', rollout_path='') for t in tasks
                if scope.allows_project(t['workspace_contract']['cwd'])]

    def canonical_turns(self, tid, project, state):
        # Recent and paginated older pages share the same authoritative read API.
        def page(before):
            return self.commands.call('observation_turns', dict(host=self.owner_host, thread_id=tid,
                                                                before=before))['executions']
        try:
            recent = page('~')
            older = page(state.get('rust_before', '~'))
        except RustAuthorityError as exc:
            if str(exc) != 'RESPONSE_CAPACITY_EXCEEDED':
                raise
            state['rust_projection_unavailable'] = True
            return []
        state.pop('rust_projection_unavailable', None)
        if older:
            state['rust_before'] = older[-1]['id']
        state['rust_history_complete'] = len(older) < 16
        rows = {r['id']: r for r in recent + older}
        return [dict(task_ref=e['task_ref'], execution_ref=e['execution_ref'], turn_id=e['turn_id'],
            ordinal=order(e['created_at']), execution_state=e['status'], native_state='UNKNOWN',
            result_processing='RECEIVED' if e['result'] else 'PENDING', business_result=None,
            summary=(e['result'] or {}).get('response_text'), artifacts=[], started_at=e['created_at'],
            completed_at=None) for e in rows.values()]

    def sample(self, metadata, state):
        events, state = super().sample(metadata, state)
        if state.get('rust_history_complete') is False:
            for event in events:
                event['coverage'] = 'HISTORY_BACKFILL'
        if self.managed_inventory_unavailable or state.get('rust_projection_unavailable'):
            for event in events:
                event['coverage'] = 'RUST_MANAGED_PROJECTION_UNAVAILABLE'
        state['active'] = state.get('active', False) or any(
            e['turn'].get('execution_state') in ('ADMITTED', 'RUNNING', 'RECOVERY_REQUIRED', 'FINALIZING') for e in events)
        return events, state
