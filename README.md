# covr

[![tests](https://github.com/evanbryant/herdr-covr/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/evanbryant/herdr-covr/actions/workflows/tests.yml)
![herdr](https://img.shields.io/badge/herdr-%E2%89%A5%200.9.0-5c58b2)
![python](https://img.shields.io/badge/python-%E2%89%A5%203.8-3776ab)
![platforms](https://img.shields.io/badge/platforms-linux%20%7C%20macOS%20%7C%20windows-555)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A compact Spaces/Agents sidebar for [herdr](https://herdr.dev). Each agent takes one line, sorted by what needs you. A second line appears only when an agent is blocked (why) or has just finished (what).

| herdr's default sidebar | with covr |
|:---:|:---:|
| <img src="docs/images/herdr-default.png" width="330" alt="herdr's default sidebar: six of nine agents fit, two lines each, no reasons"> | <img src="docs/images/covr-light.png" width="330" alt="covr: all nine agents, blocked and finished first, with the reason and the finished task on a second line"> |
| 6 of 9 agents fit | all 9, with why `api` is blocked and what `web` finished |

Both screenshots show the same session, captured headless from the test sandbox (`tools/screenshots/`).

## Reading a row

| glyph | state | second line |
|:-:|---|---|
| `×` | blocked, waiting for you | what it is asking, taken from the screen |
| `✓` | finished, not viewed yet | the task it finished |
| `◐` | working | — |
| `○` | idle (viewed) | — |
| `◗` | asleep: idle for longer than `stale_after` (30 min), dimmed | — |

- **Kind icon:** one mark per agent type: `✻` claude, `◈` codex, `✦` gemini, `⊘` grok, `◆` cursor, `◫` opencode, and so on for every agent herdr detects. `kind_icon` puts it at the `right` end of the row (the default) or at the `left`, in the brand's colour, dimmed once the agent falls asleep. `inline` puts it just after the glyph in the row's colour, and `off` hides it. The icons need a terminal font that has these symbols, such as DejaVu Sans Mono; with a font that lacks them, the terminal borrows them from another font and they can sit out of line.
- **Count:** the header reads `agents [N]`, every agent you have open.
- **Order:** blocked, then finished, then working, then idle. Within a state, the most recent change comes first.
- **Stable positions:** rows move only when an agent's state changes, never because an age ticked over.
- **Age:** time in the current state, on the right. For an idle agent that means time since you viewed it, so the 30 minutes to asleep also start then.
- **Selected agent:** a finished agent you already have selected turns from `✓` to `○` by itself after 5 s (`seen_after`; set it to `off` to keep herdr's behaviour). Without this, an agent that finishes while your terminal is in the background can stay `✓` until you click away and back.
- **Label:** the space name, plus the tab name if you renamed the tab (`infra · plan`). When two rows would look the same, both get a word or two of their task.
- **Spaces:** keep herdr's own rows, plus `±` for uncommitted changes, `!N` on a repo whose worktrees hold N blocked agents, and `★` for a pinned space.

## Install

Needs herdr 0.9.0+ and Python 3.8+ (standard library only), on Linux, macOS or Windows. On Windows, the `python3` command must start Python: for example `uv python install --default`, or a `python3` alias for the python.org install. herdr itself rates plugins on Windows as a preview.

```sh
herdr plugin install evanbryant/herdr-covr
herdr plugin action invoke covr.sidebar.install-layout
```

`install-layout` adds one marked block to herdr's `config.toml` and reloads it:
- **Colours** follow your `[theme] name`: latte colours for light themes (`kanagawa-lotus`, `rose-pine-dawn` and the other light ones), mocha for the rest. Set `layout` to `light` or `dark` to override.
- **Conflicts:** if you already define `[ui.sidebar.spaces]` or `[ui.sidebar.agents]`, it writes nothing and says why.
- **Your own rows:** to show tokens from other plugins (or your own scripts), put rows in `rows.toml` in covr's config dir (`herdr plugin config-dir covr.sidebar`). It uses herdr's row syntax, and `install-layout` adds the rows after covr's own (agent rows go above the rule that closes a kind group, so they stay with their agent); a reinstall keeps them. A row whose tokens are all empty takes no space. herdr allows 16 rows per section; covr uses 5 agent rows and 1 space row.

  ```toml
  agents = [
    [{ token = "$build", fg = "#9ca0b0" }],
  ]
  spaces = []
  # a token at the end of each agent's own line; covr shortens its line by `width` (+3 for herdr's " · ") to fit it
  agent_line = { tokens = [{ token = "$flag", fg = "#d20f39", bold = true }], width = 2 }
  ```
- **Undo:** `uninstall-layout` removes the block, byte for byte.
- **Upgrades:** run `install-layout` again after updating covr, so the block's colours match the current glyphs and icons.

<img src="docs/images/covr-dark.png" width="330" alt="covr on a dark (catppuccin-mocha) theme">

## Settings

Bind the settings popup to a key in `config.toml`:

```toml
[[keys.command]]
key = "prefix+comma"
type = "plugin_action"
command = "covr.sidebar.settings"
description = "covr: settings"
```

<img src="docs/images/settings.png" alt="the covr settings popup over a herdr session">

Options are grouped into Sidebar, Rows and Timing. ↑/↓ picks an option and ←/→ changes it; the sidebar updates straight away. `s` starts or stops the daemon.

Actions you can bind to keys: `cycle-view`, `toggle-group`, `cycle-kind`, `toggle-label` and `cycle-space-sort` step through those options; `pin-agent`, `pin-space`, `start`, `stop`, `install-layout` and `uninstall-layout` do what they say. `herdr plugin action list --plugin covr.sidebar` lists them.

## Grouping and sorting

`group_by = "project"` lists agents by project. Projects are ordered by their most urgent agent; the project name appears once, and the other agents in it are identified by their task. `group_by = "kind"` groups by agent type (claude, codex, …) instead.

<img src="docs/images/covr-project.png" width="330" alt="covr grouped by project">

Spaces keep your own order by default. `space_sort` can also be `alpha`, `recent` or `activity`, and switching back to `manual` restores your order. Pinned agents and spaces always come first.

All options, with defaults and limits, are in [docs/spec.md](docs/spec.md#options). They live in `$(herdr plugin config-dir covr.sidebar)/config.toml`.

## How it works

herdr plugins can't draw in the sidebar. They can:
- set short text values (tokens) that the sidebar's row layout displays
- choose one sort and filter for the agents list
- reorder spaces

covr's daemon does all three. It runs once per herdr session, updates when herdr reports an event (and every 5 s regardless), and recovers by itself after a herdr restart. It exits when herdr goes away or when you disable the plugin. Details and known limits: [docs/spec.md](docs/spec.md).

**Privacy:**
- For a blocked agent, covr reads the last lines of that pane once to show what it is asking.
- Ages come from what covr observes. An agent that was already running when covr started shows no age until its state changes.
- **Opt-in:** `age_source = "claude-transcripts"` fills those ages in. covr then finds the Claude Code transcript that contains the session title (under `~/.claude*/projects`) and uses the file's modification time. It stores only hashes of those lookups.
- Nothing leaves your machine.

## Tests

```sh
python3 -m unittest discover -s tests/unit     # the sidebar logic, under a second
tests/e2e/run.sh                               # lifecycle against an isolated herdr (needs tmux)
HERDR_BIN=/path/to/herdr PYTHON=python3.8 tests/e2e/run.sh restart sessions   # one herdr/Python, some tests
```

On Windows, `py tests\e2e\windows.py` runs the same lifecycle checks against a headless `herdr.exe`, apart from the popup, which needs a terminal.

Each e2e run gets its own HOME, socket and tmux server (on Windows, its own user profile folders), and never touches your herdr. It covers:
- restarts, a server that disappears, and several sessions
- disabling the plugin, the settings popup, and layout install/uninstall
- bad options, log rotation, and code reloads
- a hostile repo's git config

CI runs the unit tests on Linux, macOS and Windows (Python 3.8–3.13), and the e2e suites on herdr 0.9.0, 0.9.1 and 0.9.3 (Linux), 0.9.3 (macOS) and 0.9.3 (Windows).

## License

MIT
