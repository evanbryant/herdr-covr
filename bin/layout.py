#!/usr/bin/env python3
"""Install / remove the covr sidebar layout in herdr's config.toml.

usage: layout.py install | uninstall | show

The layout is one marked block (`# >>> covr.sidebar layout` … `# <<< covr.sidebar`); any `# >>> covr.sidebar …`
opening line other than the keys block counts, so blocks written by older versions are found too.
Everything outside it, including a `covr.sidebar keys` block, is left byte-for-byte alone.
Install replaces the block in place (idempotent) or appends it; uninstall removes exactly what
install appended. Nothing is written when the result would be a config herdr rejects, and a
failed `herdr server reload-config` puts the old file back.
"""
import os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import covrd  # noqa: E402

LAYOUTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "layouts")
BLOCK = re.compile(r"^# >>> covr\.sidebar(?! keys)[^\n]*\n.*?^# <<< covr\.sidebar[ \t]*\r?(?:\n|\Z)", re.M | re.S)
# herdr's built-in light themes (0.9.3: catppuccin-latte, tokyo-night-day, gruvbox-light, one-light,
# solarized-light, kanagawa-lotus, rose-pine-dawn); a custom name counts as light when one of its words is
LIGHT = {"latte", "light", "day", "dawn", "morning", "lotus"}
# tables the block defines; a copy outside the block makes the file invalid TOML (duplicate table)
OURS = re.compile(r"^[ \t]*\[[ \t]*ui\.sidebar\.(spaces|agents)[ \t]*\]", re.M)
# on the opening line when install had to end the user's last line first (the file had no final newline), so
# uninstall takes that newline back out too
NO_EOL = " (config had no final newline)"
THEME_CUSTOM = re.compile(r"^[ \t]*\[[ \t]*theme\.custom[ \t]*\]", re.M)


def variant(text):
    """light (catppuccin-latte colours) or dark (mocha): the `layout` option, else herdr's [theme] name."""
    want = covrd.options().get("layout", "auto")
    if want in ("light", "dark"):
        return want
    words = re.split(r"[^a-z0-9]+", (theme_name(text) or "").lower())
    return "light" if LIGHT & set(words) else "dark"


def theme_name(text):
    return covrd.herdr_config_value("theme", "name", text)


def block_for(text):
    """The layout block for this config: drop our [theme.custom] tweak if the user has their own table."""
    v = variant(text)
    block = covrd.read_text(os.path.join(LAYOUTS, "latte.toml" if v == "light" else "mocha.toml"))
    if THEME_CUSTOM.search(text):
        block = re.sub(r"^\[theme\.custom\]\n(?:[^\[\n][^\n]*\n)*\n?", "", block, flags=re.M)
    block = block if block.endswith("\n") else block + "\n"
    if text.count("\r\n") * 2 > text.count("\n"):
        block = block.replace("\r\n", "\n").replace("\n", "\r\n")  # match a CRLF config
    return block, v


def valid(text):
    if covrd.tomllib is None:
        return True  # the table checks above are all we can do without a TOML parser
    try:
        covrd.tomllib.loads(text)
        return True
    except Exception:
        return False


def reload_or_restore(path, before, existed):
    r = subprocess.run([covrd.HERDR, "server", "reload-config"], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=15, **covrd.NOWIN)
    if r.returncode == 0:
        return True
    if existed:
        covrd.write_atomic(path, before, newline="")
    else:
        os.remove(path)
    covrd.notify("herdr rejected the layout, config.toml restored: " + (r.stderr or r.stdout).strip()[:160])
    return False


def install():
    path = covrd.HERDR_CONFIG
    existed = os.path.exists(path)
    text = covrd.read_text(path, "", newline="") if existed else ""  # newline="": line endings stay as they are
    m = BLOCK.search(text)
    outside = text[:m.start()] + text[m.end():] if m else text
    if OURS.search(outside):
        covrd.notify("not installed: config.toml already defines [ui.sidebar.agents] / [ui.sidebar.spaces] "
                     "outside the covr block; remove those tables, then retry")
        return 1
    block, v = block_for(outside)
    mark = lambda b: b.replace(" layout", " layout" + NO_EOL, 1)
    if m:
        if NO_EOL in m.group(0).splitlines()[0]:
            block = mark(block)
        new = text[:m.start()] + block + text[m.end():]
    elif not text:
        new = block
    else:  # one blank line between the user's config and the block (uninstall takes it back out)
        nl = "\r\n" if block.endswith("\r\n") else "\n"
        new = text + (nl if text.endswith("\n") else nl + nl) + (block if text.endswith("\n") else mark(block))
    if not valid(new):
        covrd.notify("not installed: the result would not be valid TOML (config.toml left unchanged)")
        return 1
    if new == text:
        covrd.notify(f"layout already installed ({v})")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    covrd.write_atomic(path, new, newline="")
    if not reload_or_restore(path, text, existed):
        return 1
    covrd.notify(f"layout installed ({v}); agent rows fill in while the daemon runs")
    return 0


def uninstall():
    path = covrd.HERDR_CONFIG
    text = covrd.read_text(path, "", newline="")
    m = BLOCK.search(text)
    if not m:
        covrd.notify("no covr layout in config.toml")
        return 0
    head, tail = text[:m.start()], text[m.end():]
    nl = "\r\n" if head.endswith("\r\n") else "\n"
    if not tail and head.endswith(nl + nl):
        head = head[:-len(nl)]  # the blank line install put before an appended block
        if NO_EOL in m.group(0).splitlines()[0] and head.endswith(nl):
            head = head[:-len(nl)]  # and the newline it added to the user's last line
    new = head + tail
    covrd.write_atomic(path, new, newline="")
    if not reload_or_restore(path, text, True):
        return 1
    covrd.notify("layout removed from config.toml")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "install":
        sys.exit(install())
    elif cmd == "uninstall":
        sys.exit(uninstall())
    else:
        text = covrd.read_text(covrd.HERDR_CONFIG, "")
        print(("installed" if BLOCK.search(text) else "not installed"), "·", covrd.HERDR_CONFIG, "·", variant(text))
