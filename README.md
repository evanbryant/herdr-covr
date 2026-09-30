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

> **Status: early (0.1).** It's used daily on Linux with herdr 0.9.3. Known gaps are listed in [docs/triage-line-spec.md](docs/triage-line-spec.md#7-known-limits); lifecycle hardening (herdr restarts, multiple named sessions, disabling the plugin) is in progress.

## Requirements

- herdr 0.9.3 or later
- Python 3.11 or later (the plugin uses the standard library only)
- Linux or macOS

## Install

```sh
herdr plugin install evanbryant/herdr-covr/plugin
```

Then add the sidebar layout: copy the contents of [`plugin/sidebar-latte.toml`](plugin/sidebar-latte.toml) into `~/.config/herdr/config.toml` and run `herdr server reload-config`. The colours are tuned for catppuccin-latte. Agent rows are rendered from the plugin's tokens, so they stay empty while the daemon is stopped.

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

All actions: `cycle-view`, `toggle-group`, `cycle-kind`, `toggle-label`, `cycle-space-sort`, `pin-agent`, `pin-space`, `start`, `stop` (list them with `herdr plugin action list --plugin covr.sidebar`). The settings popup is `plugin/bin/settings.py`.

## Options

Options are stored in `$(herdr plugin config-dir covr.sidebar)/config.toml`. Actions and the settings popup edit this file, and the daemon picks up changes within one tick. See [the spec](docs/triage-line-spec.md#4-options) for every option.

## Privacy note

The daemon reads the visible text of blocked panes to show why they're waiting. For agents that were already running when it started, it also estimates how long they've been in their current state by searching Claude Code transcripts under `~/.claude*/projects` for the session title and using the matching file's modification time. Settings live in the plugin config dir and runtime state in herdr's plugin state dir; nothing leaves your machine.

## License

MIT
