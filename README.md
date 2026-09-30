# covr for herdr

A minimal, attention-first Spaces/Agents sidebar for [herdr](https://herdr.dev). Every agent is one line, state is carried by glyph shape and stoplight colour, and a second line appears only when it changes a decision (why it's blocked, what just finished).

```
× api · auth-fix           2m
  ↳ run: rm -rf build/?
✓ herdr                    4m
  ↳ fix tab rename bug
◐ web · checkout          12m
○ infra                    1h
☾ web · docs refresh      39m
```

| state | glyph |
|---|---|
| blocked | `×` + reason |
| done, unseen | `✓` + what finished |
| working | `◐` |
| idle | `○` |
| idle longer than `stale_after` | `☾` (dimmed) |

Features: attention-first sorting that doesn't flicker, the age pinned to the right edge, optional grouping by project or agent kind, sorting for Spaces (manual, alpha, recent, activity), pinned agents and spaces, and a settings popup.

> **Status: early (0.3).** It's used daily on Linux. The lifecycle is covered by an end-to-end suite (`tests/e2e/run.sh`): herdr restarts, a server that goes away, several named sessions, disabling the plugin, the settings popup, and layout install/uninstall. Known limits are in [docs/triage-line-spec.md](docs/triage-line-spec.md#7-known-limits).

## Requirements

- herdr 0.9.0 or later (the e2e suite passes on 0.9.0, 0.9.1 and 0.9.3)
- Python 3.8 or later (standard library only)
- Linux or macOS

## Install

```sh
herdr plugin install evanbryant/herdr-covr/plugin
```

Then install the sidebar layout:

```sh
herdr plugin action invoke covr.sidebar.install-layout
```

This adds one marked block to herdr's `config.toml` and reloads it. The colours are picked from your `[theme] name`: catppuccin-latte colours for light themes, mocha for everything else. Set `layout = "light"` or `"dark"` in the plugin options to override that. The action refuses to write anything if you already define `[ui.sidebar.spaces]` or `[ui.sidebar.agents]` yourself. `uninstall-layout` removes the block again. Agent rows are rendered from the plugin's tokens, so they stay empty while the daemon is stopped.

## Keys

Add these to `config.toml`. The key choices are suggestions.

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "covr.sidebar.cycle-view"
description = "covr: cycle view"

[[keys.command]]
key = "prefix+o"
type = "plugin_action"
command = "covr.sidebar.toggle-group"
description = "covr: cycle grouping"

[[keys.command]]
key = "prefix+m"
type = "plugin_action"
command = "covr.sidebar.pin-agent"
description = "covr: pin agent"
```

```toml
[[keys.command]]
key = "prefix+comma"
type = "plugin_action"
command = "covr.sidebar.settings"
description = "covr: settings"
```

All actions: `settings`, `cycle-view`, `toggle-group`, `cycle-kind`, `toggle-label`, `cycle-space-sort`, `pin-agent`, `pin-space`, `start`, `stop`, `install-layout`, `uninstall-layout` (list them with `herdr plugin action list --plugin covr.sidebar`).

## Options

Options are stored in `$(herdr plugin config-dir covr.sidebar)/config.toml`. Actions and the settings popup edit this file, and the daemon picks up changes within one tick. See [the spec](docs/triage-line-spec.md#4-options) for every option.

## Privacy note

The daemon reads the visible text of blocked panes to show why they're waiting. For agents that were already running when it started, it also estimates how long they've been in their current state by searching Claude Code transcripts under `~/.claude*/projects` for the session title and using the matching file's modification time. Settings live in the plugin config dir and runtime state in herdr's plugin state dir; nothing leaves your machine.

## License

MIT
