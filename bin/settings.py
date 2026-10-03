#!/usr/bin/env python3
"""covr settings popup. ↑/↓ select · ←/→ or enter change · s start/stop daemon · q / esc close.

Writes the plugin option file through covrd.write_option and pokes the daemon, so every change shows up
in the sidebar immediately. Drawn with curses on Linux/macOS; Windows Python ships without curses, so there
it draws with VT escape sequences (herdr's popup is a ConPTY terminal) and reads keys with msvcrt.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covrd  # noqa: E402

ITEMS = [
    ("group_by", "Group agents", ["none", "project", "kind"]),
    ("space_sort", "Sort spaces", ["manual", "alpha", "recent", "activity"]),
    ("view", "Agent view", ["triage", "needs me", "here+"]),
    ("label", "Row label", ["space", "task"]),
    ("show_tab", "Tab name on rows", ["named", "always", "never"]),
    ("show_kind", "Show agent kind", ["never", "auto", "always"]),
    ("show_icon", "Agent kind icon (✻ ◈ ✦ …)", [True, False]),
    ("show_task", "Second-line text", ["attention", "all", "never"]),
    ("disambiguate", "Task tags on look-alike rows", [True, False]),
    ("stale_after", "Idle → asleep (◗) after", ["15m", "30m", "1h", "2h", "4h"]),
    ("seen_after", "Selected ✓ → viewed after", ["off", "5s", "15s", "1m"]),
    ("age_source", "Ages of already-running agents", ["observed", "claude-transcripts"]),
]
HELP = {
    "group_by": "project: projects ordered by their best agent · kind: by agent type",
    "space_sort": "manual never moves spaces · other modes reorder (manual order is restored)",
    "view": "needs me = blocked + done · here+ = this space + anything needing you",
    "label": "what identifies an agent row",
    "show_tab": "named = only tabs you renamed (auto-numbered tabs hidden)",
    "show_kind": "auto = only while more than one kind is running · always = on every row",
    "show_icon": "a mark per agent kind after the state glyph: ✻ claude · ◈ codex · ✦ gemini · ⊘ grok …",
    "show_task": "attention = blocked reason + what finished",
    "disambiguate": "adds 1–2 words of the task when two rows would look identical",
    "stale_after": "idle longer than this dims to ◗ and sinks",
    "seen_after": "a finished agent you already have selected turns from ✓ to ○ after this long",
    "age_source": "observed = no age until a state changes · claude-transcripts = look it up in ~/.claude*",
}
FOOTER = "↑↓ select   ←→/enter change   s start/stop daemon   q close"


def show(v):
    return "on" if v is True else "off" if v is False else str(v)


def screen(sel, height):
    """The popup's lines as (text, style) with style in bold | dim | reverse | normal; the last line is the footer."""
    opt, pins = covrd.options(), covrd.load_pins()
    lines = [("covr — settings", "bold"),
             ("daemon running" if covrd.alive() else "daemon STOPPED (s to start)", "dim"), ("", "normal")]
    for i, (key, label, _) in enumerate(ITEMS):
        lines.append((f"{label:<30} ‹ {show(opt.get(key))} ›", "reverse" if i == sel else "normal"))
    lines += [("", "normal"), (HELP.get(ITEMS[sel][0], ""), "dim"), ("", "normal"),
              (f"pinned: {len(pins['agents'])} agents · {len(pins['spaces'])} spaces"
               "   (actions: pin-agent, pin-space)", "dim")]
    lines = lines[:max(1, height - 1)]
    lines += [("", "normal")] * max(0, height - 1 - len(lines))
    return lines + [(FOOTER, "dim")]


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
        cur = covrd.options().get(name)
        i = vals.index(cur) if cur in vals else -1
        i = (i - 1) % len(vals) if key == "left" else (i + 1) % len(vals)
        try:
            covrd.write_option(name, str(vals[i]).lower() if isinstance(vals[i], bool) else vals[i])
        except ValueError:
            pass
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
