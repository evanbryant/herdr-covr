#!/usr/bin/env python3
"""tmux `capture-pane -e -p` output -> a standalone HTML page that looks like the terminal did.

usage: render.py <capture.ans> <out.html> [--theme light|dark] [--cols N | --sidebar] [--rows A:B] [--title T]

--sidebar crops to herdr's sidebar (everything left of the column holding the divider │).
The terminal's default colours and 16-colour palette are catppuccin latte (light) or mocha (dark), matching
the herdr theme used for the capture; herdr itself draws with truecolor, which passes through as is.
Open the page in any browser, or screenshot it headless (e.g. playwright: page.locator(".term").screenshot()).
"""
import argparse
import os, html, re, unicodedata

THEMES = {
    "dark": dict(fg=(205, 214, 244), bg=(30, 30, 46), base16=[
        (69, 71, 90), (243, 139, 168), (166, 227, 161), (249, 226, 175), (137, 180, 250), (245, 194, 231), (148, 226, 213), (186, 194, 222),
        (88, 91, 112), (243, 139, 168), (166, 227, 161), (249, 226, 175), (137, 180, 250), (245, 194, 231), (148, 226, 213), (166, 173, 200)]),
    "light": dict(fg=(76, 79, 105), bg=(239, 241, 245), base16=[
        (92, 95, 119), (210, 15, 57), (64, 160, 43), (223, 142, 29), (30, 102, 245), (234, 118, 203), (23, 146, 153), (172, 176, 190),
        (108, 111, 133), (210, 15, 57), (64, 160, 43), (223, 142, 29), (30, 102, 245), (234, 118, 203), (23, 146, 153), (188, 192, 204)]),
}


def c256(n, base16):
    if n < 16:
        return base16[n]
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def sgr(params, st, base16):
    p = [int(x) if x else 0 for x in params.split(";")] if params else [0]
    i = 0
    while i < len(p):
        c = p[i]
        if c == 0:
            st.update(fg=None, bg=None, bold=False, dim=False, rev=False)
        elif c == 1:
            st["bold"] = True
        elif c == 2:
            st["dim"] = True
        elif c == 22:
            st["bold"] = st["dim"] = False
        elif c == 7:
            st["rev"] = True
        elif c == 27:
            st["rev"] = False
        elif c in (38, 48):
            key = "fg" if c == 38 else "bg"
            if i + 1 < len(p) and p[i + 1] == 2:
                st[key] = tuple(p[i + 2:i + 5])
                i += 4
            elif i + 1 < len(p) and p[i + 1] == 5:
                st[key] = c256(p[i + 2], base16)
                i += 2
        elif c == 39:
            st["fg"] = None
        elif c == 49:
            st["bg"] = None
        elif 30 <= c <= 37:
            st["fg"] = base16[c - 30]
        elif 90 <= c <= 97:
            st["fg"] = base16[c - 90 + 8]
        elif 40 <= c <= 47:
            st["bg"] = base16[c - 40]
        elif 100 <= c <= 107:
            st["bg"] = base16[c - 100 + 8]
        i += 1


def style(st, t):
    fg, bg = st["fg"] or t["fg"], st["bg"] or t["bg"]
    if st["rev"]:
        fg, bg = bg, fg
    s = f"color:rgb{fg};"
    if bg != t["bg"]:
        s += f"background:rgb{bg};"
    if st["bold"]:
        s += "font-weight:700;"
    if st["dim"]:
        s += "opacity:.55;"
    return s


def cells(tok):
    """Escape text for HTML, pinning every non-ASCII character to its terminal cell width. A browser draws
    symbols its font lacks (◗, braille blanks, …) from a fallback font at another width; a terminal never does."""
    out = []
    for ch in tok:
        if ord(ch) < 128:
            out.append(html.escape(ch))
        else:
            w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
            out.append(f'<span class="c{w}">{html.escape(ch)}</span>')
    return "".join(out)


def sidebar_cols(text):
    plain = [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", line) for line in text.split("\n")]
    counts = {}
    for line in plain[2:20]:
        i = line.find("│")
        if i > 0:
            counts[i] = counts.get(i, 0) + 1
    return max(counts, key=counts.get) + 1 if counts else None


def convert(text, t, cols=None, rows=None):
    lines = text.split("\n")
    if rows:
        lines = lines[rows[0]:rows[1]]
    out, st = [], dict(fg=None, bg=None, bold=False, dim=False, rev=False)
    for line in lines:
        segs, col = [], 0
        for tok in re.split(r"(\x1b\[[0-9;]*m)", line):
            if tok.startswith("\x1b["):
                sgr(tok[2:-1], st, t["base16"])
                continue
            tok = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", tok)
            if cols is not None:
                tok = tok[:max(0, cols - col)]
            tok = tok.replace("\u2800", " ")  # herdr's row padding (braille blank): it looks like a space
            if tok:
                segs.append(f'<span style="{style(st, t)}">{cells(tok)}</span>')
                col += len(tok)
        if cols is not None and col < cols:
            segs.append(" " * (cols - col))
        out.append("".join(segs))
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def main():
    a = argparse.ArgumentParser()
    a.add_argument("ans")
    a.add_argument("out")
    a.add_argument("--theme", choices=THEMES, default="light")
    a.add_argument("--cols", type=int)
    a.add_argument("--sidebar", action="store_true")
    a.add_argument("--rows")
    a.add_argument("--title", default="")
    a.add_argument("--font", action="append", default=[], metavar="FILE",
                   help="font file to draw with, in fallback order (e.g. your terminal's font, then its symbol "
                        "fallback); repeatable. Default: DejaVu Sans Mono")
    a.add_argument("--size", type=float, default=15, help="font size in px (default 15)")
    a.add_argument("--line", type=float, default=1.3, help="line height (default 1.3)")
    o = a.parse_args()
    text = open(o.ans, encoding="utf-8", errors="replace").read()
    t = THEMES[o.theme]
    cols = sidebar_cols(text) if o.sidebar else o.cols
    rows = tuple(int(x) for x in o.rows.split(":")) if o.rows else None
    body = convert(text, t, cols, rows)
    bg, fg = "rgb%s" % (t["bg"],), "rgb%s" % (t["fg"],)
    faces = "".join(f"@font-face{{font-family:t{i};src:url('file://{os.path.abspath(f)}')}}\n" for i, f in enumerate(o.font))
    family = ",".join([f"t{i}" for i in range(len(o.font))] + ["'DejaVu Sans Mono'", "'Menlo'", "monospace"])
    page = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(o.title)}</title>
<style>
{faces}body{{margin:0;background:transparent}}
.term{{display:inline-block;white-space:pre;font:{o.size:g}px/{o.line:g} {family};color:{fg};background:{bg};
padding:14px 16px;border-radius:10px}}
.c1,.c2{{display:inline-block;text-align:center;overflow:visible}} .c1{{width:1ch}} .c2{{width:2ch}}
</style><div class="term">{body}</div>
"""
    with open(o.out, "w", encoding="utf-8") as f:
        f.write(page)


if __name__ == "__main__":
    main()
