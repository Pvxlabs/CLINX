# Accepted iMac Monitor baseline — 2026-10-02

The user explicitly requested that all App changes from the iMac UI conversation be
preserved when merging repository branches, with the currently accepted experience
as the authority. The preserved commit is `68f35fc` (on top of `d4de74f` and `4816fd9`).

`MonitorApp/` at that commit is the complete accepted App source, tests, build and
stable signing baseline. Preserve the directory's Git tree across backend merges.
Do not reapply the older P620 Monitor dirty patch or infer that older Figma/header
assertions override the user's subsequent acceptance.

This includes the compact shared header and native window buttons, removed search
row, sidebar click-to-dock and hover drawer, opened history and back/forward,
local archive/restore with archive-aware counts, latest-ten Timeline expansion,
40pt bottom inset, one-pixel panel separators, fixed TaskRow/badge/metadata rules,
Activity feedback and its smooth-scrolling correction. The accepted header token
is 38pt; source-only tests expecting a system toolbar, an extra search row, or a
collapsed counter rail are obsolete. Keep the current native traffic-light placement.

Validation at the baseline: 59 Swift tests passed, Release build/stable signing,
real P620-connected Mac screenshots, and user feedback “明显顺畅了”. Activity transport
and state tests passed; actual newly generated message latency remains unmeasured.

The merge includes already committed P620 owner/liveness and Local Discovery history
through `b4ac38d`. It does not authorize a new runtime activation or deletion of
P620 worktrees, pending changes, credentials, or runtime data. Concurrent integration
work must fetch the resulting main and preserve this App baseline.

## Merge verification

- Activity commit: `68f35fc`; iMac baseline branch: `codex/imac-ui-baseline-20261002`.
- Baseline and merged `MonitorApp` tree: `f44c8910c75cb75c59271a17a8f3fe24d03bd804`.
  The entire App directory is unchanged by the integration.
- Fresh merged-tree Swift run: 59 passed, 0 failures.
- P620 isolated merged-tree Python regression: 890 passed, 112 subtests passed,
  6 explicitly opt-in live Provider/Host tests skipped; 15 multiprocessing fork warnings.
  Local Discovery loopback self-tests enabled. No production lifecycle tests started.
- Isolated validation snapshot: `/tmp/clinx-imac-merge.Q5RFRw`;
  final log `pytest-complete.log`. Its test-only bridge.toml routes DB/logs inside the
  snapshot. Initial missing Rust binary/Git history failures are retained in pytest.log;
  the final run followed a locked Rust release build and provision of historical Git objects.
- Source-level Monitor tests now reflect accepted header/navigation/search/bottom-inset
  and Settings-based synthetic selection, replacing outdated layout assertions.
- The parallel P620 integration conversation was successfully notified of this baseline
  after its earlier active-writer rejection cleared. Its uncommitted workspace is preserved.
