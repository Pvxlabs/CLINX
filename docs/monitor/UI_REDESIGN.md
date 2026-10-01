# CLINX Monitor — UI/UX redesign implementation map

Status: implementation landed in `MonitorApp/Sources/CLINXMonitor`; macOS runtime evidence is
recorded separately in `docs/monitor/UI_REDESIGN_EVIDENCE.md`.

## 1. Figma source

| Item | Value |
| --- | --- |
| Design file | `CLINX Monitor UI/UX Redesign` — Figma **Make** |
| File key | `gjtQIi4778Yoh5cvRv5gwS` |
| URL | https://www.figma.com/make/gjtQIi4778Yoh5cvRv5gwS/CLINX-Monitor-UI-UX-Redesign |
| Read through | Figma MCP (`get_design_context`, Make source resources under `src/`) |
| Source files used as authority | `src/index.css` (tokens), `src/monitor/data.ts` (fixture + scenarios), `src/monitor/primitives.tsx` (status system, badges, progress, icons), `src/monitor/MonitorWindow.tsx` (screens, geometry, interaction), `src/docs/Pages.tsx` (IA, tokens, components, Developer Handoff) |

The document shell of that Make file (`App.tsx`, pages 00–16) is **design documentation
navigation**, not product navigation, and is deliberately not reproduced in the app.

Earlier Figma design file `OgzTpC5hnbctXciUVN6wri` contains only that documentation shell
(principles + before/after spec) and no screen layers; it was superseded as the source by the
Make file above.

## 2. Screens → implementation

| Design page | Scenario / view | Implemented by |
| --- | --- | --- |
| 04 Live / Healthy | `healthy` | `MonitorRootView` + `ExecutionListView` (CURRENT tag, no attention group) |
| 05 Active | `running`, view `active` | `ExecutionListView.groupHeader` (“Needs attention” → “Running”) |
| 06 Blocked | `blocked`, view `blocked` | `BlockerPanelView` (BLOCKER), `InspectorView` |
| 07 Failed | `failed`, view `failed` | `BlockerPanelView` (FAILURE), failed timeline row |
| 08 Completed | `running`, view `completed` | `ProgressIndicator` determinate + PASS chip |
| 09 Stale | `stale` | `FreshnessTag`, `ConnectivityStrip`, `MonitorStatus.status(freshness:)` |
| 10 Offline | `offline` | `ConnectivityStrip` (“Observer unavailable”, last known data stays) |
| 11 Empty | `empty` | `EmptyStateView`, list empty state |
| 12 Synthetic Acceptance | `long`, synthetic | `EnvironmentBadge`, `ScenarioSelector`, purple window edge, long-content fixture |
| 13 Settings | — | `MonitorSettingsView` (Settings scene) |
| 14 Dark Mode | any, dark | `DS.Palette` dynamic tokens |
| 15 Compact Window | 1100×720 / 900×640 | `MonitorRootView` metrics (`rail`, `listWidth`, `inspectorStacked`) |
| 16 Developer Handoff | — | keyboard commands, truncation rules, scroll behaviour |

## 3. Design tokens

`DesignTokens.swift` is the single token layer. Values are the Make file's `:root` /
`[data-theme='dark']` variables.

| Token | Light | Dark |
| --- | --- | --- |
| `--bg` / surface | `#FCFCFC` | `#0F1011` |
| `--canvas`, `--sidebar` | `#F4F4F5` | `#08090A` |
| `--surface-2` | `#F4F4F5` | `#17181A` |
| `--border` | `#E4E4E7` | `#23252A` |
| `--divider` | `#EFEFF1` | `#1B1C1F` |
| `--text-1 / 2 / 3` | `#1B1B1F` / `#62626B` / `#8F8F98` | `#F7F8F8` / `#8A8F98` / `#62666D` |
| `--selection` | `#ECECEF` | `#1C1D21` |
| `--selection-strong` / `--focus` | `#5E6AD2` | `#7C84E8` |
| `--hover` | `#F1F1F3` | `#16171A` |
| `--st-running` | `#5E6AD2` | `#7C84E8` |
| `--st-success` | `#3D9A64` | `#4CB782` |
| `--st-warning` | `#D27B1E` | `#F2994A` |
| `--st-error` | `#D4483E` | `#EB5757` |
| `--st-stale` | `#B8962A` | `#D8B443` |
| `--st-unknown` | `#9A9AA3` | `#6B6F76` |
| `--synthetic` | `#9B59D0` | `#B07BE3` |

Metrics: toolbar 52 (native macOS unified toolbar), task row 54, list header 40, group header
32, pagination 26, connectivity strip 28, sidebar 200 / rail 52, list 384 · 340 · 300,
inspector min 520, panel radius 10, blocker radius 8, control radius 5, attention edge 2,
window minimum 900×600.

Typography: system font (SF Pro) for UI; SF Mono (`.monospaced`) for IDs, stages, codes and
timestamps; tabular figures on every duration, count and timestamp.

## 4. Status system

One seven-state system (`StatusSystem.swift`) is shared by row, pill, sidebar and inspector;
each state has a unique glyph so state never depends on colour alone.

| State | Glyph | Colour token | Observer mapping |
| --- | --- | --- | --- |
| RUNNING | spinner arc | `--st-running` | `CODEX_RUNNING`/claimed/dispatching/… or persisted `codex_running` |
| BLOCKED | octagon | `--st-warning` | `BLOCKED`, `RECOVERY_REQUIRED`, `TRANSPORT_UNCERTAIN`, `retry_required`, exact result `BLOCKED` |
| FAILED | square ✕ | `--st-error` | `FAILED`, exact result `FAILED`, host operation exit ≠ 0 |
| COMPLETED | circle ✓ | `--st-success` | `COMPLETED`, `IN_REVIEW` |
| CANCELLED | slash | `--st-unknown` | `CANCELLED`, `STOPPED` |
| STALE | triangle | `--st-stale` | any non-terminal state while authority ≠ live |
| UNKNOWN | dashed ring | `--st-unknown` | anything unrecognised — never inferred as success |

Freshness is a property of the snapshot: `CURRENT`, `STALE`, `LAST KNOWN` are always labelled
(list tag, inspector tag, connectivity strip). Offline keeps the last snapshot visible.

## 5. Deliberate adaptations (design → macOS)

1. **Fonts.** The Make file names Inter with an SF Pro fallback and `'SF Mono', ui-monospace,
   'JetBrains Mono'` for mono. The app ships no web fonts: UI text uses the macOS system font
   and technical text uses SF Mono. This is the design's own stack minus the web fallbacks.
2. **Row height 54.** The Make implementation renders 52px rows while the principles page, the
   IA page and the tokens page all specify 54px. The documented figure was implemented.
3. **Toolbar.** The design mock draws a 48px in-canvas header; the handoff and IA pages specify
   a 52px unified toolbar, which is what the native macOS toolbar provides.
4. **Window chrome.** Traffic-light dots and the window shadow belong to the Figma mock frame;
   the real window supplies them. The synthetic purple edge is drawn at the top of the content
   area, directly below the native title bar.
5. **Settings.** The design renders an in-window modal; the handoff calls for the native
   Settings scene, so ⌘, opens the standard macOS settings window with the design's three
   panes (Connection / Appearance / Shortcuts).
6. **List container.** Rows are rendered in a `ScrollView` + `LazyVStack(pinnedViews:)` rather
   than `List`: the design needs an exact 54px row, a 2px attention edge, custom selection fill
   and selection that survives refresh by execution ID. `↑`/`↓` are implemented with
   `onMoveCommand` on the focusable list.
7. **Timeline evidence.** The Observer wire model carries no per-event payload, so timeline rows
   show the canonical `event_ref` as mono metadata. The evidence disclosure (collapsed by
   default) exists on the blocker/failure panel, where the canonical blocker message is the
   evidence.
8. **Progress.** “Progress unavailable” is the label whenever no canonical denominator exists;
   the reason (“No canonical progress denominator is available.”) lives in the tooltip. The
   legacy raw string is gone from the codebase.
9. **Credential display.** Settings shows a fixed mask with no characters of the stored secret.
10. **Test connection** performs a read-only `GET /v1/health` through the same client; it is not
    an execution control.
