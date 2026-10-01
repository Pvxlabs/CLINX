# Host trusted workspace paths

P620 declares its development roots once in
`bridge.toml`, `[host_executor].trusted_workspace_roots`. Runtime validation,
the dynamic tool contract and `clinx_get_capabilities` publish/use that same
configuration. Current roots are `/home/pvxlabs/dev` and `/data/artifacts`.

`HOST_FILESYSTEM.path_read` and `development_command` accept absolute paths and
paths relative to the sealed task cwd. `Path.resolve()` must place every checked
operand within a configured root, including after `..` or symlink traversal.
New worktrees, detached checkouts and artifact directories require no additional
registered target. An installation without configured roots retains the previous
project-only boundary. Marker operations retain their project-local namespace.

The registered task cwd remains the execution identity and lease anchor. Structured
Git `head`, `status`, `fetch_origin`, `remote_main_head`, `ahead_behind` and
`push_current_branch` retain the task's registered repository/origin/branch.
Development `git -C <trusted path>` supports cross-checkout inspection; Git
network commands and configuration overrides remain rejected there.

Trusted path access does not grant production, SSH, network, sudo or business
authority. Unix permission denial is preserved. The existing operation classes,
production intent, registered remote commands, leases and delivery/replay guards
still apply. Paths under `/etc`, `/root`, `/var/lib`, `.ssh` or `.aws` outside the
allowlisted roots are denied by root membership, not a directory blacklist.
Configured production mutation entrypoints also remain unavailable through
`development_command`, including interpreter operands; use their registered
operation and separately sealed production authority.

Development execution is trusted-user execution with argv and `shell=False`,
not an OS filesystem sandbox for arbitrary program internals. Argument checks
cannot constrain a Python program's computed paths or prevent concurrent filesystem
changes after validation. Do not run untrusted programs through this surface.
No root privilege or new credentials are provisioned by this contract.

Validation: `python3 -m pytest -q test_trusted_workspace_paths.py
test_host_contract_v2.py test_tool_delivery.py test_m13b.py` plus the independent
formal connector smoke after the outer operator switches runtime source.
