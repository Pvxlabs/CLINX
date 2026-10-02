# Host delivery failure reconciliation

`thread/resume` in the supported Provider contract does not accept
`dynamicTools`. CLINX now keeps that fact explicit: a Host-success/provider
failure stores the complete `item/completed` item and SHA-256, while the durable
delivery remains `FAILED`. Automatic maintenance is limited to a known
non-mutating Host operation such as registered `GIT/status`. The first later execution on the same Task creates a
successor Provider thread with the Host schema and records
`DYNAMIC_TOOL_SCHEMA_UPGRADE` in the existing conversation lineage. It never
resumes the predecessor with a different tool schema.

The maintenance path is deliberately separate from execution start and never
invokes Host or Provider:

```text
python3 bridge.py recover-execution exec_<id> --reconcile-delivery \
  --tool-call-id <call-id> --delivery-proof-json '<validated identity/summary JSON>'
```

The proof must identify the exact task/thread/turn, Host receipt, operation
class/capability/operation/argv, the authoritative `mutating` property, and the
persisted Provider failure-item hash. CLINX reads the Host receipt and Provider
item from its own ledger, compares every field under an SQLite CAS, writes the
existing audit path, and then releases the old execution while preserving the
original `BLOCKED` result and `delivery_state=FAILED`. A second call returns
`ALREADY_APPLIED`; it cannot execute the recorded call again. Unknown Host
completion, missing Provider item, mutating/unknown operation facts, competing
owners, stale leases, wrong thread, or hash mismatch are rejected.

This source change does not activate the shared runtime or reconcile any ORION
execution. The installed release and runtime source must be read back and
activated by the outer maintenance owner.

## Control-release handoff

Source commit: `b66cdf4b331955d6b5fb180b74c79478e1b426d0` on `main`, pushed to
the registered `origin`. The previous source commit is
`563e77cffc696d97b30be732cd4397550fb86e00`; restoring that commit is the source
rollback boundary. No shared service restart or runtime activation is part of
this handoff.
