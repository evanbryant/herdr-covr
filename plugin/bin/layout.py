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
LIGHT = ("latte", "light", "day", "dawn", "morning")
# tables the block defines; a copy outside the block makes the file invalid TOML (duplicate table)
OURS = re.compile(r"^[ \t]*\[[ \t]*ui\.sidebar\.(spaces|agents)[ \t]*\]", re.M)
THEME_CUSTOM = re.compile(r"^[ \t]*\[[ \t]*theme\.custom[ \t]*\]", re.M)


def variant(text):
    """light (catppuccin-latte colours) or dark (mocha): the `layout` option, else herdr's [theme] name."""
    want = covrd.options().get("layout", "auto")
    if want in ("light", "dark"):
        return want
    name = (theme_name(text) or "").lower()
    return "light" if any(w in name for w in LIGHT) else "dark"


def theme_name(text):
    return covrd.herdr_config_value("theme", "name", text)


def block_for(text):
    """The layout block for this config: drop our [theme.custom] tweak if the user has their own table."""
    v = variant(text)
    block = covrd.read_text(os.path.join(LAYOUTS, "latte.toml" if v == "light" else "mocha.toml"))
    if THEME_CUSTOM.search(text):
        block = re.sub(r"^\[theme\.custom\]\n(?:[^\[\n][^\n]*\n)*\n?", "", block, flags=re.M)
    return block if block.endswith("\n") else block + "\n", v


def valid(text):
    if covrd.tomllib is None:
        return True  # the table checks above are all we can do without a TOML parser
    try:
        covrd.tomllib.loads(text)
        return True
    except Exception:
        return False


def reload_or_restore(path, before, existed):
    r = subprocess.run([covrd.HERDR, "server", "reload-config"], capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        return True
    if existed:
        covrd.write_atomic(path, before)
    else:
        os.remove(path)
    covrd.notify("herdr rejected the layout, config.toml restored: " + (r.stderr or r.stdout).strip()[:160])
    return False


def install():
    path = covrd.HERDR_CONFIG
    existed = os.path.exists(path)
    text = covrd.read_text(path, "") if existed else ""
    m = BLOCK.search(text)
    outside = text[:m.start()] + text[m.end():] if m else text
    if OURS.search(outside):
        covrd.notify("not installed: config.toml already defines [ui.sidebar.agents] / [ui.sidebar.spaces] "
                     "outside the covr block; remove those tables, then retry")
        return 1
    block, v = block_for(outside)
    if m:
        new = text[:m.start()] + block + text[m.end():]
    elif not text:
        new = block
    else:  # one blank line between the user's config and the block (uninstall takes it back out)
        new = text + ("\n" if text.endswith("\n") else "\n\n") + block
    if not valid(new):
        covrd.notify("not installed: the result would not be valid TOML (config.toml left unchanged)")
        return 1
    if new == text:
        covrd.notify(f"layout already installed ({v})")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    covrd.write_atomic(path, new)
    if not reload_or_restore(path, text, existed):
        return 1
    covrd.notify(f"layout installed ({v}); agent rows fill in while the daemon runs")
    return 0


def uninstall():
    path = covrd.HERDR_CONFIG
    text = covrd.read_text(path, "")
    m = BLOCK.search(text)
    if not m:
        covrd.notify("no covr layout in config.toml")
        return 0
    head, tail = text[:m.start()], text[m.end():]
    if not tail and head.endswith("\n\n"):
        head = head[:-1]  # the blank line install put before an appended block
    new = head + tail
    covrd.write_atomic(path, new)
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
