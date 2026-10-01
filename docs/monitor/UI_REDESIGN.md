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

The Figma **design** file `OgzTpC5hnbctXciUVN6wri` began as that documentation shell only
(principles + before/after spec). It now carries the full screen set — `Live / Healthy`
(node `5:3841`), `Active` (`5:4657`), `Blocked` (`5:5531`), `Failed` (`5:6312`), `Completed`,
`Stale`, `Offline`, `Empty`, `Synthetic Acceptance`, `Settings`, `Dark Mode` and `Compact
Window` — each a complete `MonitorWindow` render (1440×900 desktop, 1100×720 / 900×640
compact), plus a published `RuntimeStatus` component set (4 states × 2 themes). Those pages were
hand-tuned *after* the Make implementation, so for the header, content insets, row, badge,
sidebar count and runtime-status details recorded in §6 they are the current authority. The Make
file remains the source for the original screen inventory in §2 and the token values in §3.

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
| 12 Synthetic Acceptance | `long`, synthetic | `RuntimeStatus` (synthetic), `ScenarioSelector`, purple window edge, long-content fixture |
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
inspector min 520, panel radius 10, blocker radius 8, control radius 5, window minimum 900×600,
content bottom inset 40, runtime status inset 8 / 12, sidebar count inset 18, stage badge 16,
search field 210 wide / 150 in Compact Window.

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

## 4b. Icons

Figma source of truth: page **17 “App Icon”** of the Make file — `src/monitor/AppIcon.tsx`
plus its toolbar use in `MonitorWindow.tsx` (`<AppIcon size={24} flat />`), and the menu bar
glyph in the same file.

**App mark.** An open ring observing one execution — “watched, never touched”. 1024 grid:
824 tile at (100,100) with a 185 corner radius, a graphite gradient (#2B2C31 → #0D0E10) and a
top edge highlight; the ring is radius 210 with a 66 stroke centred at (474,500), its 59.7°
opening on the right; the execution dot (r 64) sits at (506,500) in `--st-running` indigo.
The drop shadow is dropped below 64px.

| Where | Implementation |
| --- | --- |
| Window toolbar mark | `AppMark.swift` — `AppMark(size: 24, flat: true).padding(-3)`, exactly the design's usage |
| `.app` bundle icon | `Scripts/make-app-icon.swift` renders the same geometry at 16…1024 into an `.iconset`, `iconutil` packs `AppIcon.icns`; `Scripts/build-app.sh` generates it into `Contents/Resources` and `Info.plist` sets `CFBundleIconFile=AppIcon` |

**Menu bar glyph.** `AppIcon.tsx` also specifies an 18×18pt template menu bar glyph with six
states (live · blocked · failed · stale · offline · synthetic), a state priority of
*Offline › Failed › Blocked › Stale › Live*, “synthetic always shows the diamond”, and the
rule that only the Blocked/Failed dot is drawn in colour while the ring and dot stay
template. The design's own Principles page still lists “Menu bar extra with attention count”
under **Future suggestions — NOT in Phase 2 UI**, so no menu bar extra is shipped in this
change; the glyph spec is recorded here so it can be implemented in one step when the design
promotes it.

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
   than `List`: the design needs an exact 54px row, a custom selection fill and selection that
   survives refresh by execution ID. `↑`/`↓` are implemented with `onMoveCommand` on the
   focusable list.
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

## 6. Finalized page details

The hand-tuned `MonitorWindow` renders in the design file encode the rules below. Every value
was read from those pages, not from the older Make source.

| Rule | Figma | Implementation |
| --- | --- | --- |
| Header carries no product title | `Header` (`5:4014`) holds only the traffic lights, the search field and two tool buttons | `MonitorRootView.toolbarContent` keeps the app mark, the scenario picker, refresh and settings — no title container |
| Search field aligns with the content edge | `SearchField` at `x = 209`; the content panel starts at `208` plus its 1pt border | The field is the first row of the content column, so the alignment is structural at any sidebar width — desktop column or compact rail |
| Content bottom inset | Panels end at `y = 860` of a 900-tall window → 40pt, unchanged on `Stale`, `Offline` and `Compact Window` | `DS.Metric.contentBottomInset` on both panels |
| Runtime status | `RuntimeStatus` docked bottom-right with `fills: 0`, `strokes: 0` — no card, no capsule | `RuntimeStatus`: dot + environment + connection, transparent, no background and no border |
| Row accent strip | No `TaskRow` on any page has a leading edge rect | `ExecutionRowView` draws no attention overlay |
| Stage badge | 16pt tall, 8pt mono label, 6pt horizontal padding, hugged width, hairline stroke, no fill | `StageBadge.Metrics` |
| Row trailing metadata | 9pt, one step below the row's own `meta` | `DS.Font.rowTrailing` |
| Sidebar counts | `Text:align` right-aligned, count right edge 18pt inside the 188pt `SidebarItem`, plain 11pt text, identical for all five views | `DS.Metric.sidebarCountTrailingInset` plus one shared `countView` |

`Blocked` and `Failed` counts take their status colour and nothing else — no badge, no tinted
fill, no minimum width, no padding — so neither a selection background nor a status colour can
move the digits off the shared column.
