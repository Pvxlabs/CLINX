# P620 CLINX project workspace policy

The user requested convergence to one CLINX project repository on
2026-10-13 (Asia/Shanghai). On P620, the canonical repository is
`/home/pvxlabs/dev/clinx`.

- Do not create another long-lived CLINX clone, sibling worktree, pilot, or
  reference project under `/home/pvxlabs/dev`.
- Reuse the existing task coordination and workspace lease. Preserve unrelated
  dirty work; do not reset, clean, or overwrite it to start a task.
- If qualification needs a disposable repository, place it in the task-owned
  `/data/artifacts` directory. Record ownership and dependencies, save unique
  logs and receipts, then remove the disposable repository and regenerable
  build outputs when the task ends. Keep source recovery as refs, bundles,
  patches, or compressed snapshots rather than permanent extra checkouts.
- Keep deployment artifacts, canonical databases, credentials, and rollback
  dependencies under their existing runtime contracts. Repository convergence
  is not permission to remove those resources.
- Do not redirect retired `pilot` or `clinx-local-discovery` aliases to the
  canonical repository; their origin, branch, and path identities differ.

See [the convergence record](docs/PROJECT_CONVERGENCE.md) for recovery evidence.
