# covr — design spec (covr.sidebar 0.2, as implemented)

Minimal, attention-first herdr sidebar. Every space and every agent is one line; state is carried by glyph shape and stoplight colour; text gets a second line only when it changes a decision. Implemented as a herdr plugin (`plugin/`) — a Python daemon that pushes display tokens, one `agent.view.set` view, and a sidebar layout block in herdr's `config.toml` that is installed once and never rewritten by options.

## 1. Agents section

### Row anatomy
```
× api-wt-auth              2m      first row: glyph · label [· tag] [· kind] … age (pinned right)
  ↳ run: rm -rf build/?            second row, blocked only: why it waits (red, bold)
✓ herdr                    4m
  ↳ fix tab rename bug             second row, done-unseen only: what finished (teal)
☾ web · docs refresh      39m      stale idle: gray moon, whole row dim
```
- The first row is ONE composed token (`$head`): glyph + label + optional parts, padded with U+2800 (braille blank — not trimmed by herdr) so the age sits flush right, one column before the divider. The daemon reads the live sidebar width (the user's dragged width from herdr's client prefs, else `ui.sidebar_width`) every tick.
- Consequence: the whole first row takes the state colour (herdr has one style per token, and separate tokens always join with ` · `).
- Long text truncates at a word boundary with `…`; the age is never truncated.

### States (stoplight, catppuccin-latte)
| state | glyph | colour | notes |
|---|---|---|---|
| blocked | × | `#d20f39` red, bold | + `↳ reason` row |
| done-unseen | ✓ | `#179299` teal (herdr's native done) | + `↳ what finished` row |
| working | ◐ | `#c27c0e` yellow (latte yellow darkened for legibility) | |
| idle | ○ | `#40a02b` green | |
| idle-stale (> `stale_after`) | ☾ | `#9ca0b0` gray, dim | sinks to the bottom of the idle tier |
| unknown | · | `#9ca0b0` | |
| pinned (any state) | + ` ★` after the label | row colour | pinned rows lead |

### Ordering (default, `group_by = "none"`)
`agent.view.set` sort: **pin** (pinned first) → **attention** (blocked > done-unseen > working > idle) → **rank** (freshest first).
- `rank` = zero-padded inverse of the time the agent ENTERED its current state. It only changes on a real state change, so rows never move between ticks (fixes the swap-and-revert flicker caused by sorting on a ticking age).
- Ages: time in current state. For agents already running when the daemon starts, the start is taken from the newest Claude transcript in the agent's project dir that mentions its session title (mtime); after that the daemon observes transitions itself. Persisted across daemon restarts.

### Grouping
**`group_by = "project"`** (the space is the project):
- Groups are ordered by their **highest-ranked agent** under the current sort (pin → attention → freshness); a project holding a blocked agent comes first. Pinned spaces lead all groups.
- Within a group, agents keep the current sort.
- The project name appears once, on the group's first agent (`✓ web · Checkout flow`); the other agents are indented two cells under it and identified by their task (`☾   docs refresh`).
- View label: `by project`.

**`group_by = "kind"`**: groups by agent kind (claude, codex, …), ordered by each group's most urgent agent; the kind label appears once on the first agent of each group and a dim `───` rule closes each group. View label: `by kind`.

Mechanism for both: the daemon pushes `grp` = group index (`0001`…); view sort = `grp` → `pin` → `attention` → `rank`.

### Identity without the agent kind
- Default label = the space; the kind (`claude`) is never shown unless `show_kind` asks for it.
- **Disambiguation**: when two agents in the same space would render identically (same visible glyph), both get a 1–2 word task tag (`☾ web · docs refresh` / `☾ web · Login bug`). Tags never cut words.
- `label = "task"` swaps the space for the agent's task title.
- Task titles come from herdr's `terminal_title_stripped` (Claude Code sets it to the session title).

### Views
`view = triage | needs me | here+` — none / `status in [blocked, done]` / current space + anything needing you. The Agents header shows the active label (`triage`, `by project`, `by project · needs me`, …).

## 2. Spaces section
- Row: `state_icon workspace · [!N] · branch git_status · [±] · [★]` — herdr-native glyphs and stoplight colours; `!N` (red, bold) on a parent whose worktree children hold N blocked agents (herdr's own rollup ignores children); `±` (peach) = uncommitted changes (`git status --porcelain`, every 30 s); `★` (yellow) = pinned.
- **Sorting** (`space_sort`), applied by moving spaces with `workspace.move_block` (herdr has no Spaces sort API):
  | mode | order |
  |---|---|
  | `manual` (default) | your own order — the daemon never moves anything unless a space is pinned |
  | `alpha` | label, case-insensitive |
  | `recent` | most recently focused first (the daemon records focus each tick) |
  | `activity` | most urgent agent first (tier only, manual order breaks ties — moves only on tier changes) |
  Pinned spaces always lead. Worktree children always move with their parent. Your manual order is snapshotted before the first reorder and restored when you switch back to `manual` (verified round-trip).
- Space numbers (`prefix+1..9`) follow the displayed order, so non-manual sorts renumber spaces.

## 3. Pinning
- `pin-agent` pins/unpins the focused agent (pane); `pin-space` pins/unpins the current space. Stored in `~/.local/state/herdr/plugins/covr.sidebar/pins.json`; stale pane ids are dropped automatically.
- Pinned agents lead the Agents list (within their group when grouped); pinned spaces lead the Spaces list and the project groups.

## 4. Options
File: `~/.config/herdr/plugins/config/covr.sidebar/config.toml` (actions rewrite it; the daemon picks changes up within one tick).
| option | values (default **bold**) |
|---|---|
| `group_by` | **none** · project · kind |
| `space_sort` | **manual** · alpha · recent · activity |
| `label` | **space** · task |
| `show_tab` | **named** (only tabs you renamed; auto-numbered hidden) · always · never — shown as `space · tab`; when a row is too long the space name is shortened first |
| `show_kind` | **never** · auto (only when >1 kind is live) · always |
| `show_task` | **attention** (blocked reason + done summary) · all · never |
| `disambiguate` | **true** · false |
| `stale_after` | **1h** |
| `view` | **triage** · needs me · here+ |
| `tick_seconds` | **5** |
| `layout` | **auto** (light themes get latte colours, others mocha) · light · dark. Used by `install-layout`. |

## 5. Keys (in herdr `config.toml`, `# >>> covr.sidebar keys` block)
| key | action |
|---|---|
| `prefix+comma` | **settings popup** (the `covr.sidebar.settings` action): every option, ←/→ to change it live, `s` to start or stop the daemon |
| `prefix+a` | cycle view |
| `prefix+o` | cycle grouping (none → project → kind) |
| `prefix+s` | cycle space sort |
| `prefix+m` | pin / unpin focused agent |
| `prefix+y` | pin / unpin current space |
| `prefix+i` | cycle show-kind |
| `prefix+t` | toggle label space / task |

## 6. Implementation
- `plugin/herdr-plugin.toml`:
  - `[[startup]]` runs `covrd.py startup`. It starts the session's daemon, or, if one survived a server restart or live handoff, makes it re-push everything.
  - `[[events]]` (agent status/detected, pane closed, workspace created, worktree opened) poke the daemon.
  - Actions: the ones above plus `start`, `stop`, `settings`, `install-layout` and `uninstall-layout`. The settings popup is a `[[panes]]` popup entrypoint.
- `plugin/bin/covrd.py` is the daemon. Every tick, or when poked, it reads `agent.list` and `workspace.list` over the socket, diffs the tokens, pushes only the changes with `herdr pane|workspace report-metadata --source covr`, sets the view, and moves spaces per `space_sort`. Its lifecycle:
  - **One daemon per herdr session.** Its state lives in `$HERDR_PLUGIN_STATE_DIR/s/<hash of the socket>/`. An `flock` held for the daemon's lifetime makes concurrent spawns safe. Pins are shared by all sessions.
  - **Resync.** If herdr no longer shows tokens the daemon pushed (after a server restart), it re-pushes every token and the view.
  - **Exit.** It exits after 60 s without a reachable socket, and it clears its tokens and exits once the plugin is disabled or unlinked.
  - **Options** are read without `tomllib` on Python older than 3.11.
- `plugin/bin/layout.py` and `plugin/layouts/{latte,mocha}.toml`: the sidebar block, managed between the `# >>> covr.sidebar (covr)` and `# <<< covr.sidebar` markers.
  - Install is idempotent, and uninstall removes exactly what install added.
  - A config that would define our tables twice is refused, and nothing is written.
  - If `server reload-config` fails, the old file is restored.
- `tests/e2e/run.sh`: the lifecycle suite, run against an isolated herdr.

## 7. Known limits
- If the daemon stops, agent rows are empty (their text is plugin tokens). `herdr plugin action invoke covr.sidebar.start` or a herdr restart brings it back.
- The first row is single-colour (herdr cannot style part of a token).
- Right-pinning depends on the width the daemon reads; a drag is picked up within one tick.
- Non-manual space sorts physically reorder (and renumber) spaces.
- The Spaces/Agents split is user-owned (drag once).
