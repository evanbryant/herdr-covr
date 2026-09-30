#!/usr/bin/env python3
"""covr settings popup (curses). ↑/↓ select · ←/→ or enter change · q / esc close.

Writes the plugin option file through covrd.write_option and pokes the daemon, so every
change shows up in the sidebar immediately.
"""
import curses, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covrd  # noqa: E402

ITEMS = [
    ("group_by", "Group agents", ["none", "project", "kind"]),
    ("space_sort", "Sort spaces", ["manual", "alpha", "recent", "activity"]),
    ("view", "Agent view", ["triage", "needs me", "here+"]),
    ("label", "Row label", ["space", "task"]),
    ("show_tab", "Tab name on rows", ["named", "always", "never"]),
    ("show_kind", "Show agent kind", ["never", "auto", "always"]),
    ("show_task", "Second-line text", ["attention", "all", "never"]),
    ("disambiguate", "Task tags on look-alike rows", [True, False]),
    ("stale_after", "Idle → stale moon after", ["15m", "30m", "1h", "2h", "4h"]),
]
HELP = {
    "group_by": "project: projects ordered by their best agent · kind: by agent type",
    "space_sort": "manual never moves spaces · other modes reorder (manual order is restored)",
    "view": "needs me = blocked + done · here+ = this space + anything needing you",
    "label": "what identifies an agent row",
    "show_tab": "named = only tabs you renamed (auto-numbered tabs hidden)",
    "show_kind": "auto = only while more than one kind is running",
    "show_task": "attention = blocked reason + what finished",
    "disambiguate": "adds 1–2 words of the task when two rows would look identical",
    "stale_after": "idle longer than this dims to ☾ and sinks",
}


def poke():
    covrd.signal_daemon()


def show(v):
    return "on" if v is True else "off" if v is False else str(v)


def main(scr):
    curses.curs_set(0)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    sel = 0
    while True:
        opt = covrd.options()
        pins = covrd.load_pins()
        scr.erase()
        h, w = scr.getmaxyx()
        scr.addnstr(0, 2, "covr — settings", w - 3, curses.A_BOLD)
        daemon = "daemon running" if covrd.alive() else "daemon STOPPED (s to start)"
        scr.addnstr(1, 2, daemon, w - 3, curses.A_DIM)
        for i, (key, label, vals) in enumerate(ITEMS):
            y = 3 + i
            if y >= h - 4:
                break
            val = show(opt.get(key))
            line = f"{label:<30} ‹ {val} ›"
            attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
            scr.addnstr(y, 2, line, w - 3, attr)
        y = 4 + len(ITEMS)
        if y < h - 3:
            scr.addnstr(y, 2, HELP.get(ITEMS[sel][0], ""), w - 3, curses.A_DIM)
        if y + 2 < h - 1:
            scr.addnstr(y + 2, 2, f"pinned: {len(pins['agents'])} agents · {len(pins['spaces'])} spaces"
                        "   (prefix+m pin agent · prefix+y pin space)", w - 3, curses.A_DIM)
        scr.addnstr(h - 1, 2, "↑↓ select   ←→/enter change   s start/stop daemon   q close", w - 3, curses.A_DIM)
        scr.refresh()
        k = scr.getch()
        if k in (ord("q"), 27):
            return
        if k in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(ITEMS)
        elif k in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(ITEMS)
        elif k in (curses.KEY_RIGHT, curses.KEY_LEFT, 10, 13, ord(" "), ord("l"), ord("h")):
            key, _, vals = ITEMS[sel]
            cur = opt.get(key)
            i = vals.index(cur) if cur in vals else -1
            i = (i - 1) % len(vals) if k in (curses.KEY_LEFT, ord("h")) else (i + 1) % len(vals)
            covrd.write_option(key, str(vals[i]).lower() if isinstance(vals[i], bool) else vals[i])
            poke()
        elif k == ord("s"):
            if covrd.alive():
                covrd.stop()
            else:
                covrd.spawn()


if __name__ == "__main__":
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
