#!/usr/bin/env python3
"""covr settings popup. ↑/↓ select · ←/→ or enter change · s start/stop daemon · q / esc close.

Writes the plugin option file through covrd.write_option and pokes the daemon, so every change shows up
in the sidebar immediately. Drawn with curses on Linux/macOS; Windows Python ships without curses, so there
it draws with VT escape sequences (herdr's popup is a ConPTY terminal) and reads keys with msvcrt.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covrd  # noqa: E402

SECTIONS = [
    ("Sidebar", [
        ("view", "Agent view", ["triage", "needs me", "here+"]),
        ("group_by", "Group agents", ["none", "project", "kind"]),
        ("space_sort", "Sort spaces", ["manual", "alpha", "recent", "activity"]),
    ]),
    ("Rows", [
        ("label", "Row label", ["space", "task"]),
        ("show_tab", "Tab name on rows", ["named", "always", "never"]),
        ("show_kind", "Show agent kind", ["never", "auto", "always"]),
        ("kind_icon", "Agent kind icon (✻ ◈ ✦ …)", ["right", "left", "inline", "off"]),
        ("show_task", "Second-line text", ["attention", "all", "never"]),
        ("disambiguate", "Task tags on look-alike rows", [True, False]),
    ]),
    ("Timing", [
        ("stale_after", "Idle → asleep (◗) after", ["15m", "30m", "1h", "2h", "4h"]),
        ("seen_after", "Selected ✓ → viewed after", ["off", "5s", "15s", "1m"]),
        ("age_source", "Ages of already-running agents", ["observed", "claude-transcripts"]),
    ]),
]
ITEMS = [item for _, items in SECTIONS for item in items]
HELP = {
    "group_by": "project: projects ordered by their best agent · kind: by agent type",
    "space_sort": "manual never moves spaces · other modes reorder (manual order is restored)",
    "view": "needs me = blocked + done · here+ = this space + anything needing you",
    "label": "what identifies an agent row",
    "show_tab": "named = only tabs you renamed (auto-numbered tabs hidden)",
    "show_kind": "auto = only while more than one kind is running · always = on every row",
    "kind_icon": "right/left: own token in brand colour · inline: after the glyph, row colour",
    "show_task": "attention = blocked reason + what finished",
    "disambiguate": "adds 1–2 words of the task when two rows would look identical",
    "stale_after": "idle longer than this dims to ◗ and sinks",
    "seen_after": "a finished agent you already have selected turns from ✓ to ○ after this long",
    "age_source": "observed: no age until a state changes · claude-transcripts: look it up",
}
FOOTER = "↑↓ select   ←→/enter change   s start/stop daemon   q close"


def show(v):
    return "on" if v is True else "off" if v is False else str(v)


def screen(sel, height):
    """The popup's lines as (text, style) with style in bold | dim | reverse | normal; the last line is the footer.
    A popup too short for everything scrolls the options so the selected one stays in view; the help line and
    the footer always show."""
    opt, pins = covrd.options(), covrd.load_pins()
    try:  # count only pins whose agent or space is still open (the daemon drops the rest a minute later)
        agents = {a["pane_id"] for a in covrd.call("agent.list")["agents"]}
        spaces = {w["workspace_id"] for w in covrd.call("workspace.list")["workspaces"]}
        pins = {"agents": [x for x in pins["agents"] if x in agents], "spaces": [x for x in pins["spaces"] if x in spaces]}
    except Exception:
        pass
    status = "daemon running" if covrd.alive() else "daemon STOPPED (s to start)"
    body = [("covr — settings", "bold"),
            (f"{status} · pinned: {len(pins['agents'])} agents, {len(pins['spaces'])} spaces", "dim")]
    i, at = 0, 0
    for title, items in SECTIONS:
        body += [("", "normal"), (title, "bold")]
        for key, label, _ in items:
            if i == sel:
                at = len(body)
            body.append((f"  {label:<31} ‹ {show(opt.get(key))} ›", "reverse" if i == sel else "normal"))
            i += 1
    room = max(1, height - 3)  # footer, help and the blank line above it
    if len(body) > room:
        start = min(max(0, at - room // 2), len(body) - room)
        body = body[start:start + room]
    lines = body + [("", "normal"), (HELP.get(ITEMS[sel][0], ""), "dim")]
    lines = lines[-max(1, height - 1):] if len(lines) > height - 1 else lines
    lines += [("", "normal")] * max(0, height - 1 - len(lines))
    return lines + [(FOOTER, "dim")]


def step(name, vals, cur, key):
    """The value ←/→ moves to. A duration set by hand (45m) moves to the nearest preset in that direction."""
    if cur not in vals and name in ("stale_after", "seen_after") and covrd.validate(name, cur)[1] is None:
        secs = lambda v: 0 if v == "off" else covrd.seconds(v)
        if key == "left":
            lower = [v for v in vals if secs(v) < secs(cur)]
            return lower[-1] if lower else vals[-1]
        higher = [v for v in vals if secs(v) > secs(cur)]
        return higher[0] if higher else vals[0]
    i = vals.index(cur) if cur in vals else -1
    return vals[(i - 1) % len(vals) if key == "left" else (i + 1) % len(vals)]


def handle(key, sel):
    """Apply one key ('up', 'down', 'left', 'right', 'enter', 's', 'q'). Returns the new selection, or None to close."""
    if key == "q":
        return None
    if key == "up":
        return (sel - 1) % len(ITEMS)
    if key == "down":
        return (sel + 1) % len(ITEMS)
    if key in ("left", "right", "enter"):
        name, _, vals = ITEMS[sel]
        v = step(name, vals, covrd.options().get(name), key)
        try:
            covrd.write_option(name, str(v).lower() if isinstance(v, bool) else v)
        except ValueError as e:
            covrd.notify(f"not changed: {e}")  # e.g. a config.toml that does not parse
        covrd.signal_daemon()
    elif key == "s":
        if covrd.alive():
            covrd.stop()
        else:
            covrd.spawn()
    return sel


def run_curses():
    import curses

    def loop(scr):
        curses.curs_set(0)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        attrs = {"bold": curses.A_BOLD, "dim": curses.A_DIM, "reverse": curses.A_REVERSE, "normal": curses.A_NORMAL}
        keys = {curses.KEY_UP: "up", ord("k"): "up", curses.KEY_DOWN: "down", ord("j"): "down",
                curses.KEY_LEFT: "left", ord("h"): "left", curses.KEY_RIGHT: "right", ord("l"): "right",
                10: "enter", 13: "enter", ord(" "): "enter", ord("s"): "s", ord("q"): "q", 27: "q"}
        sel = 0
        while sel is not None:
            scr.erase()
            h, w = scr.getmaxyx()
            for y, (text, style) in enumerate(screen(sel, h)):
                if text:
                    scr.addnstr(y, 2, text, w - 3, attrs[style])
            scr.refresh()
            k = keys.get(scr.getch())
            if k:
                sel = handle(k, sel)

    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(loop)


def run_windows():
    import msvcrt, shutil
    sgr = {"bold": "\x1b[1m", "dim": "\x1b[2m", "reverse": "\x1b[7m", "normal": ""}
    arrows = {"H": "up", "P": "down", "K": "left", "M": "right"}
    plain = {"k": "up", "j": "down", "h": "left", "l": "right", "\r": "enter", " ": "enter",
             "s": "s", "q": "q", "\x1b": "q", "\x03": "q"}
    out = sys.stdout
    out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide the cursor
    try:
        sel = 0
        while sel is not None:
            size = shutil.get_terminal_size((84, 18))
            body = "".join(f"\x1b[{y + 1};3H\x1b[K{sgr[style]}{text[:size.columns - 3]}\x1b[0m"
                           for y, (text, style) in enumerate(screen(sel, size.lines)))
            out.write("\x1b[2J" + body)
            out.flush()
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):  # arrow and function keys arrive as a prefix plus a code
                k = arrows.get(msvcrt.getwch())
            else:
                k = plain.get(ch)
            if k:
                sel = handle(k, sel)
    finally:
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()


if __name__ == "__main__":
    if covrd.WIN:
        run_windows()
    else:
        run_curses()
