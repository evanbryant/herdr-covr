# covr: design and behaviour

This is how covr (plugin id `covr.sidebar`, version 0.6.1) draws herdr's sidebar and how its daemon behaves. The README covers installation and a quick tour.

## Agents

<img src="images/covr-light.png" width="330" align="right" alt="covr sidebar">

### Rows

```
agents [9]               triage   header: every open agent, then the view
× ✻ api                      2m   glyph · kind icon · label [· tab] [· tag] [· kind]      age
  ↳ Bash(npm run migrate:up)      blocked only: what it is asking (red)
✓ ◈ web                     <1m
  ↳ Fix checkout redirect         finished and not yet seen: the task (cyan)
◗ ✻ docs                   2h1m   idle past stale_after (asleep): the whole row dims
```

- **Blocked reason:** read from the pane: the command above an approval prompt, otherwise the last question on screen (`?` or `？`, optionally followed by a `[y/N]` choice). A question that wraps is joined back up and shown from its start, cut at a word with `…`. On Claude Code's question form, the question's short header leads: `Deploy: Ship it?`. Only a real form counts (a line of nothing but `☐`/`☒`/`✔` tabs, with the form's `Enter to select · … · Esc to cancel` footer below), so a todo list is never taken for a header.
- **One token:** the first line is a single token, `$head`, holding the glyph, the label, any extra parts and the age.
- **Right-aligned age:** herdr has no alignment, so the daemon pads the line with U+2800 (a blank that herdr does not trim) to the sidebar's current width.
- **One colour per line:** herdr styles a whole token at once, so the whole line takes its state's colour.
- **Truncation:** long labels are cut at a word boundary with `…`. The space name is shortened before the tab name, and the age and a pin's `★` are never cut. No token exceeds herdr's 80-character limit.
- **Tabs:** tab names show only for tabs you renamed (`show_tab = named`); auto-numbered tabs are hidden.
- **Look-alike rows:** when two agents in one space and tab would render the same, each gets one or two words of its task (`web · Docs refresh` / `web · Login bug`).
- **Kind icon:** one mark per agent kind; `kind_icon` places it. Terminals draw a character their font lacks from a fallback font, often wider or higher than a cell: Hack (Warp's default) lacks `✻ ✦ ✧ ☿` and the `✓` state glyph, which is why they can look offset there. DejaVu Sans Mono has every glyph covr uses. Claude's `✻` and Gemini's `✦` are the brands' own marks; the others are plain shapes: codex `◈`, grok `⊘`, cursor `◆`, copilot `◉`, opencode `◫`, amp `▲`, droid `▤`, pi `π`, omp `∏`, qwen `✧`, kimi `◍`, cline `◘`, devin `◇`, hermes `☿`, letta `λ`, kilo `▣`, qodercli `◪`, agy `◢`, kiro `▽`, mastracode `►`, anything else `▫`. None reuses a state glyph. herdr colours a whole token at once and puts an unconfigurable ` · ` between tokens, so the placement trades colour against room:

  | `kind_icon` | row | colour | cost |
  |---|---|---|---|
  | `left` | `✻ · × api            2m` | brand (`$icon` token) | 4 cells |
  | `inline` | `× ✻ api              2m` | the row's state colour (inside `$head`) | 2 cells |
  | `right` (default) | `× api           2m · ✻` | brand (`$icon_r` token) | 4 cells |
  | `off` | `× api                2m` | — | 0 |

  Brand colours are the official ones where a brand has one: Claude `#D97757` (`#C15F3C`, Claude's darker clay, on light layouts), Gemini `#8E75B2`, Qwen `#6950EF`. Brands whose logos are black (OpenAI, Grok, Cursor, Copilot, OpenCode, Kimi, Cline, Pi) and kinds without an official colour use a neutral tone: white-ish on dark layouts, `#7c7f93` on light ones, which also reads when a dark terminal shows through herdr's light theme. The brand colour shows while an agent is live, idle included, so a herdr restart (which restores every agent idle) still shows colour. Asleep rows dim the icon with the rest of the row, and rows of unknown state grey it. `left` and `right` need the current layout (`install-layout`). The 0.5.0 option `show_icon = false` still means `off`. Official logos would need an icon font on every machine, so they are left for a later opt-in mode.
- **Agent count:** the Agents header shows `agents [N]`, N being every open agent whatever the view filters. herdr right-aligns a view's label, so the daemon pads it with U+2800 to put the count beside the word; on a sidebar too narrow for that it falls back to `[N] · <view>`. herdr's Spaces header can't be labelled by a plugin.
- **Agent kind:** hidden by default (`show_kind`). `auto` shows it while more than one kind runs: on every row, or once per group under `group_by = kind`. `always` puts it on every row under any grouping.
- **Task titles** are herdr's `terminal_title_stripped`; Claude Code sets it to the session title.
- **`label = task`** replaces the space name with the task title. Anything that would repeat the title is then left out (the finished line, the `all` second lines, look-alike tags, tab names). The blocked line stays.

### States

| state | glyph | light / dark colour |
|---|:-:|---|
| blocked | `×` | `#d20f39` / `#f38ba8`, bold, plus the reason line |
| finished, not seen | `✓` | `#04a5e5` / `#89dceb`, plus the task line |
| working | `◐` | `#c27c0e` / `#f9e2af` |
| idle | `○` | `#40a02b` / `#a6e3a1` |
| asleep (idle past `stale_after`) | `◗` | `#9ca0b0` / `#7f849c`, dimmed |
| unknown | `·` | `#9ca0b0` / `#7f849c` |
| pinned (any state) | ` ★` after the label | the state's colour |

### Order

The agents list uses one herdr Agent view.
- **Sort:** pinned first, then attention (blocked > finished > working > idle), then the most recent state change.
- **Stable ranking:** an agent's rank is set by the moment it entered its current state, so rows move only when a state changes, never as ages tick.
- **Viewed restarts the timer:** herdr reports a finished agent as `done` until it is viewed, then as `idle`, without counting that as a state change. covr restarts the agent's timer at that moment, however it was viewed, so the idle age and the `stale_after` countdown run from the view, not from the finish.
- **Selected and finished:** herdr's server marks an agent viewed only on an explicit focus. An agent that finishes while it is already the selected pane (for example with the terminal in the background) can therefore stay `done`. After `seen_after` (default 5 s), covr focuses it where it is, which marks it viewed and moves nothing. The daemon wakes at the due time, not at its next tick.
- **Blocked is a snapshot:** what a blocked agent is asking is read once, from the bottom of its pane, and kept until the prompt is answered. herdr reads blocked off the screen, so scrolling a blocked pane up hides the prompt and herdr reports the agent idle (or finished). While the pane is scrolled up, covr keeps the row blocked, with its age and its reason; it lets go once the pane is back at the bottom, or at once if the agent starts working. herdr itself still chimes when the prompt comes back into view. On a herdr that does not report a pane's scroll position, the row follows herdr.
- **Age source:** the age is time in the current state, as the daemon observes it; it remembers start times across restarts. An agent already in its state when the daemon first sees it has no age until its state changes, and so has one that changed while the daemon was stopped or down (the moment it changed is unknown). With `age_source = claude-transcripts` (opt-in), the start time of a Claude Code agent is taken from the newest transcript that mentions the session title; a few agents are looked up per tick, so a large session fills in over a few seconds.

### Grouping

<img src="images/covr-project.png" width="330" align="right" alt="grouped by project">

**By project** (`group_by = project`, where each space is a project):
- A project's rank is its most urgent agent's rank; pinned spaces lead.
- The project name appears on the first agent only. The others are indented and labelled by their task.
- The list header shows `by project`.

**By kind** (`group_by = kind`) groups claude, codex and so on:
- The kind is named once per group, and a dim rule closes each group.
- With only one kind running, neither the name nor the rule is shown.

In both modes the daemon gives each agent a group number, `grp`, and the view sorts by it before everything else.

### Views

| `view` | shows |
|---|---|
| `triage` | everything |
| `needs me` | blocked and finished agents |
| `here+` | the current space, plus anything blocked or finished elsewhere |

<br clear="right">

## Spaces

- **Row:** herdr's own space row, plus three markers:
  - `!N` (red) on a repo whose worktree spaces hold N blocked agents. herdr's own rollup ignores worktrees.
  - `±` (peach) when `git status` finds uncommitted changes. This is checked every 30 s, for all repos at once, without taking git's index lock. A repo's own config can't run commands here: `core.fsmonitor` and filter drivers (`filter.*.clean` / `process`, git-lfs among them) are switched off and submodules aren't entered. Files that use one of those filter drivers are left out of the check (the rest of the repo still counts, whichever subdirectory the pane is in; a driver whose name git can't put in a pathspec, such as `a.b`, is switched off but its files still count), and a repo whose filter driver name contains `=` (which can't be switched off safely) is not checked. All repos share a 2 s budget per check; one that fails or runs out keeps its last mark.
  - `★` for a pinned space.
- **Sorting** (`space_sort`). herdr has no sort setting for spaces, so the daemon moves them with `workspace.move_block`:

  | mode | order |
  |---|---|
  | `manual` (default) | yours; nothing moves unless you pin a space |
  | `alpha` | by name |
  | `recent` | most recently focused first |
  | `activity` | by most urgent agent; your order breaks ties |

  - Pinned spaces lead, and worktree spaces stay under their repo.
  - Your manual order is saved before the first reorder and restored when you switch back.
  - Space numbers (`prefix+1..9`) follow the displayed order.
  - If something keeps moving spaces back (3 re-applied orders within 60 s), sorting pauses for 5 minutes, with one notification.

## Pinning

`pin-agent` pins the focused agent and `pin-space` pins the current space. Pinned items lead their list, or their group when grouped. Pins belong to their herdr session (pane and space ids only mean something there) and are stored with its state. Pins made before 0.6, when one file served every session, are kept by the default session; named sessions start without pins. Pins for agents and spaces that have been gone for a minute are dropped. Pinning a pane that is not an agent does nothing, and says so. Pinning a worktree space marks it and moves its repo's space to the top.

## Options

Options are stored in `$(herdr plugin config-dir covr.sidebar)/config.toml`. The settings popup and actions write this file, and the daemon reads it every tick.
- **Invalid values:** `set` refuses them. In a hand-edited file they are ignored, with one log line and one notification each.
- **Edits keep your file:** a change from the popup or an action rewrites only that option's line (keeping its comment), so comments, the other lines and the file's line endings stay. A file that is not valid TOML is never overwritten: the change is refused with a notification until the file is fixed (until then the sidebar uses the defaults). A UTF-8 byte-order mark is accepted.
- **Old Pythons:** before Python 3.11 (no `tomllib`), the file is read by a small built-in parser. It skips a line it can't read instead of rejecting the file, so there a change is written even when another line is broken (only the changed option's line is touched).

Rows are grouped by the settings popup's sections; the last two options are file-only.

| section | option | values (default first) |
|---|---|---|
| Sidebar | `view` | `triage` · `needs me` · `here+` |
| | `group_by` | `none` · `project` · `kind` |
| | `space_sort` | `manual` · `alpha` · `recent` · `activity` |
| Rows | `label` | `space` · `task` |
| | `show_tab` | `named` · `always` · `never` |
| | `show_kind` | `never` · `auto` (only while more than one kind runs) · `always` |
| | `kind_icon` | `right` · `left` · `inline` · `off` (where the agent kind icon goes; brand-coloured at left and right) |
| | `show_task` | `attention` (blocked reason and finished task) · `all` · `never` |
| | `disambiguate` | `true` · `false` |
| Timing | `stale_after` | `30m`; any duration from `1m` to `30d` |
| | `seen_after` | `5s`; `off`, or any duration from `1s` to `1h` |
| | `age_source` | `observed` · `claude-transcripts` (opt-in: reads transcript files under `~/.claude*/projects`) |
| file only | `tick_seconds` | `5`; from 2 to 60 |
| | `layout` | `auto` · `light` · `dark` (used by `install-layout`) |

## Actions

`settings` opens the popup. The others:
- `cycle-view`, `toggle-group`, `cycle-kind`, `toggle-label` and `cycle-space-sort` step through an option. From the command line, `covrd.py set <option> cycle` also flips the on/off options; options that take a value (durations, `tick_seconds`) need one.
- `pin-agent` and `pin-space` pin and unpin.
- `start` and `stop` control the daemon. After `stop` (or `s` in the popup), hooks no longer start it; only `start` or a herdr start or restart does.
- `install-layout` and `uninstall-layout` manage the layout block.

Bind any of them in herdr's `config.toml` as `type = "plugin_action"`, `command = "covr.sidebar.<action>"`.

## Daemon

`bin/covrd.py` is a Python standard-library daemon, one per herdr session, keyed by the session's socket.

- **Updates:** on every tick (5 s), and immediately when a hook fires. The hooks are agent status and detection, pane focus and close, workspace create, close, rename, focus and reorder, tab rename, and worktree open. Bursts are merged into at most one update per 0.5 s, and a hook costs about 15 ms (`bin/poke.py`). Between ticks the daemon sleeps until the next tick, a hook or a due time; it does not poll (on Windows it checks its wake file every 0.1 s).
- **Diffing:** it reads `agent.list`, `workspace.list` and `tab.list`, and sends only changed tokens over the socket (`pane.report_metadata` / `workspace.report_metadata`, source `covr`), falling back to the `herdr … report-metadata` CLI on a server that lacks those methods. Tokens an earlier daemon left behind are cleared on the first push.
- **Sequencing:** reports carry a strictly increasing `--seq`, so a late write can't undo `stop`. If herdr keeps rejecting the numbers, the daemon switches to plain reports.
- **Recovery:** when herdr stops showing its tokens (a restart or live handoff), it sends all of them and the view again. The `[[startup]]` hook triggers the same resync.
- **Exit:** it exits after 60 s without herdr (a socket that is missing, refuses connections or doesn't answer within 5 s), and it clears its tokens and exits when the plugin is disabled or unlinked. Other errors are logged and retried on the next tick, and a crash's traceback goes to the log.
- **One daemon per session:** a lock held for the daemon's lifetime keeps concurrent starts safe, and a spawn lock makes a burst of hooks start only one process.
- **Code updates:** it restarts itself when its code changes on disk (on Windows by starting a detached successor, never a console window).
- **Sidebar width:** read from this session's herdr client preferences (the width you dragged to), else `ui.sidebar_width`, else 26.
- **State:** stored under the plugin's state directory, in `s/<session hash>/`. Its directories, log, lock and files are readable only by you. Cached transcript lookups are stored as hashes and pruned to live panes, and the log rotates at 256 KB (on Windows the daemon writes it only through short-lived handles, so it can rotate there too).

`bin/layout.py` manages the block between `# >>> covr.sidebar layout` and `# <<< covr.sidebar` in herdr's `config.toml`:
- **Idempotent:** installing twice changes nothing.
- **Clean undo:** uninstalling removes exactly what install added.
- **Conflicts:** it refuses a config that would define the same tables twice.
- **Rollback:** it restores the old file if `herdr server reload-config` fails.
- **Byte for byte:** line endings are kept (the block follows a CRLF file's endings), and a symlinked `config.toml` (stow, chezmoi, home-manager) is written through to its target.
- **Light or dark:** `auto` picks latte colours for herdr's light themes (`catppuccin-latte`, `tokyo-night-day`, `gruvbox-light`, `one-light`, `solarized-light`, `kanagawa-lotus`, `rose-pine-dawn`) and for any theme name with a word like `light`, `day`, `dawn` or `latte`; mocha otherwise. Set `layout` to override.
- **Older blocks:** a block written by an older covr version is recognised. Run `install-layout` again after an upgrade, so the block's colour rules match the current glyphs (0.4.3 changed asleep to `◗`; 0.5.0 added the kind-icon tokens; 0.5.1 made finished cyan).

## Platforms

The same code runs on Linux, macOS and Windows. Only a few mechanisms differ:

| | Linux / macOS | Windows |
|---|---|---|
| herdr's API | Unix socket at `HERDR_SOCKET_PATH` | named pipe `\\.\pipe\<HERDR_SOCKET_PATH>` |
| one daemon per session | `fcntl.flock` on `covrd.lock` | `msvcrt.locking` on `covrd.lock` |
| waking the daemon (hooks, resync, stop) | `SIGUSR1` / `SIGUSR2` / `SIGTERM`, through a wakeup pipe | a word appended to the `wake` file, checked every 0.1 s |
| API timeout | socket timeout, 5 s | the pipe read on a worker thread, 5 s |
| detaching the daemon | new session | detached process group, broken away from the hook's job |
| settings popup | curses | VT escape sequences, keys through `msvcrt` |
| herdr's config / state | `~/.config/herdr` / `~/.local/state/herdr` | `%APPDATA%\herdr` / `%LOCALAPPDATA%\herdr` |

- **Paths:** herdr's own directories are found from the plugin directories herdr passes, so XDG overrides and Windows profile folders need no special handling.
- **Encoding:** every file and subprocess uses UTF-8 explicitly, because Windows Python's default encoding is not UTF-8.
- **Child processes:** on Windows, `git` and `herdr` run without a console window.

## Known limits

- **No daemon, no rows:** if the daemon is not running, agent rows are empty, because their text comes from the daemon's tokens. `start`, or a herdr restart, brings it back.
- **One colour per line**, as herdr can't style part of a token.
- **Resizing:** after you drag the sidebar wider or narrower, alignment catches up within one tick.
- **Sorting moves spaces:** any `space_sort` other than `manual` really moves spaces, which renumbers them.
- **The split** between Spaces and Agents is herdr's; drag it once.
- **Sort labels aren't clickable.** herdr makes the Agents sort label clickable only for its own two sorts. With a plugin view active the label does nothing, and plugins get no sidebar click events. Switch with the actions (bound to keys) or the settings popup.
- **Windows:** herdr's plugin support there is a preview. The settings popup is tested by driving it directly, not inside herdr's popup pane (the test herdr runs without a client window).
