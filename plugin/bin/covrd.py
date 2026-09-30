#!/usr/bin/env python3
"""covr daemon (covr.sidebar) — computes sidebar tokens from live herdr state.

usage: covrd.py spawn | run | poke | stop | status | set <option> <value|cycle>

Tokens pushed per agent pane (source "covr"): head (glyph + label, coloured by a
glyph-prefix rule), age / age_stale, kind, tag, wait, done, task, rule, rank, kgrp.
Per workspace: alert, dirty. Options live in $HERDR_PLUGIN_CONFIG_DIR/config.toml;
nothing here ever rewrites herdr's own config.toml.
"""
import glob, json, os, re, signal, socket, subprocess, sys, time, unicodedata

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

PID = "covr.sidebar"
SRC = "covr"
HERE = os.path.dirname(os.path.abspath(__file__))
CFG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser(f"~/.config/herdr/plugins/config/{PID}")
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(f"~/.local/state/herdr/plugins/{PID}")
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
SOCK = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
PIDFILE, MEMO, LOG = (os.path.join(STATE_DIR, n) for n in ("covrd.pid", "memo.json", "covrd.log"))
ZW = "​"

DEFAULTS = {"label": "space", "show_kind": "never", "group_by": "none", "show_task": "attention",
            "disambiguate": True, "stale_after": "1h", "view": "triage", "space_sort": "manual", "show_tab": "named", "tick_seconds": 5}
CYCLES = {"view": ["triage", "needs me", "here+"], "group_by": ["none", "project", "kind"],
          "show_kind": ["never", "auto", "always"], "label": ["space", "task"],
          "show_task": ["attention", "all", "never"], "space_sort": ["manual", "alpha", "recent", "activity"],
          "show_tab": ["named", "always", "never"]}
GLYPH = {"blocked": "×", "done": "✓", "working": "◐", "idle": "○", "unknown": "·", "stale": "☾"}
PRIO = {"blocked": 4, "done": 3, "working": 2, "idle": 1, "unknown": 0}
PINS = os.path.join(STATE_DIR, "pins.json")
TOKENS = ["pin", "head", "age", "age_stale", "kind", "tag", "wait", "done", "task", "rule", "rank", "kgrp", "grp"]


def log(*a):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


# ---------------- herdr access ----------------
def call(method, params=None):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(SOCK)
    s.sendall((json.dumps({"id": "covr", "method": method, "params": params or {}}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1 << 16)
        if not c:
            break
        buf += c
    s.close()
    r = json.loads(buf)
    if "error" in r:
        raise RuntimeError(f"{method}: {r['error']}")
    return r["result"]


def cli(*args):
    subprocess.run([HERDR, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def report(kind, target, set_=None, clear=()):
    args = [kind, "report-metadata", target, "--source", SRC]
    for k, v in (set_ or {}).items():
        args += ["--token", f"{k}={v}"]
    for k in clear:
        args += ["--clear-token", k]
    cli(*args)


# ---------------- options ----------------
def options():
    o = dict(DEFAULTS)
    p = os.path.join(CFG_DIR, "config.toml")
    if tomllib and os.path.exists(p):
        with open(p, "rb") as f:
            o.update(tomllib.load(f))
    return o


def write_option(key, value):
    o = options()
    if value == "cycle":
        seq = CYCLES[key]
        value = seq[(seq.index(o[key]) + 1) % len(seq)] if o[key] in seq else seq[0]
    elif isinstance(DEFAULTS.get(key), bool):
        value = value.lower() in ("1", "true", "yes", "on")
    elif isinstance(DEFAULTS.get(key), int):
        value = int(value)
    o[key] = value
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(os.path.join(CFG_DIR, "config.toml"), "w") as f:
        f.write("# covr.sidebar (covr) options — edited by actions; the daemon picks changes up live\n")
        for k in DEFAULTS:
            v = o[k]
            f.write(f"{k} = {json.dumps(v) if not isinstance(v, bool) else str(v).lower()}\n")
    return value


def load_pins():
    try:
        p = json.load(open(PINS))
    except Exception:
        p = {}
    return {"agents": list(p.get("agents", [])), "spaces": list(p.get("spaces", []))}


def toggle_pin(kind):
    ctx = {}
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except Exception:
        pass
    if kind == "agents":
        target = os.environ.get("HERDR_PANE_ID") or ctx.get("focused_pane_id")
    else:
        target = os.environ.get("HERDR_WORKSPACE_ID") or ctx.get("workspace_id")
    if not target:
        return None, None
    p = load_pins()
    on = target not in p[kind]
    p[kind] = [x for x in p[kind] if x != target] + ([target] if on else [])
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(p, open(PINS, "w"))
    return target, on


def seconds(s):
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", str(s).strip())
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 3600


BLANK = "\u2800"  # braille blank: renders empty but is not whitespace, so herdr does not trim it


def cells(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def sidebar_width():
    """Live sidebar width: the user's dragged width (client prefs) wins over ui.sidebar_width."""
    w = None
    prefs = sorted(glob.glob(os.path.expanduser("~/.local/state/herdr/client-shell/local-*.json")), key=os.path.getmtime)
    for p in reversed(prefs):
        try:
            w = json.load(open(p)).get("sidebar_width")
            if w:
                break
        except Exception:
            pass
    if not w:
        try:
            cfg = tomllib.load(open(os.path.expanduser("~/.config/herdr/config.toml"), "rb"))
            w = cfg.get("ui", {}).get("sidebar_width")
        except Exception:
            pass
    return int(w or 26)


def pinned_row(glyph, text, age, width):
    """One composed row: 'glyph text' left, age flush right. Usable cells = width - 1 indent - 1 divider."""
    usable = width - 3   # 1 indent + 1 gap + 1 divider
    right = (" " + age) if age else ""
    room = usable - cells(right) - 2          # glyph + space
    if cells(text) > room:
        parts = text.split(" · ")
        if len(parts) > 1 and cells(" · ".join(parts[1:])) + 5 <= room:
            # shorten the space name first: the tab/task after it is usually the more telling part
            rest = " · " + " · ".join(parts[1:])
            head = parts[0]
            while head and cells(head) + 1 + cells(rest) > room:
                head = head[:-1]
            text = head.rstrip() + "…" + rest
        else:
            while text and cells(text) > room - 1:
                text = text[:-1]
            text = text.rstrip(" ·") + "…"
    pad = usable - 2 - cells(text) - cells(right)
    return f"{glyph} {text}{BLANK * pad}{right}" if age else f"{glyph} {text}"


def tab_label(label, mode):
    """herdr tab name for the row: 'named' shows only names the user gave (auto-numbered tabs are '1', '2', …)."""
    label = (label or "").strip()
    if mode == "never" or not label:
        return ""
    if mode == "named" and label.isdigit():
        return ""
    return label


def short_tag(title, limit=12):
    """First one or two words of the task title, never cut mid-word."""
    w = title.split()
    two = " ".join(w[:2])
    if len(two) <= limit:
        return two
    return w[0] if len(w[0]) <= limit else w[0][:limit - 1] + "…"


def fmt_age(sec):
    if sec is None:
        return ""
    if sec < 60:
        return "<1m"
    m = sec // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m}m" if m and h < 10 else f"{h}h"
    return f"{h // 24}d"


# ---------------- age source ----------------
def transcript_mtime(cwd, title, cache):
    """Best-effort 'last activity' for an agent whose state began before the daemon saw it:
    newest Claude transcript in the cwd's project dir that mentions the session title."""
    if not cwd or not title:
        return None
    key = f"{cwd}|{title}"
    if key in cache:
        return cache[key]
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    files = []
    for root in glob.glob(os.path.expanduser("~/.claude*/projects")):
        files += glob.glob(os.path.join(root, slug, "*.jsonl"))
    files.sort(key=lambda p: -os.path.getmtime(p))
    best = None
    for p in files[:25]:
        try:
            r = subprocess.run(["grep", "-lF", title, p], capture_output=True, timeout=3)
            if r.returncode == 0:
                best = os.path.getmtime(p)
                break
        except Exception:
            pass
    cache[key] = best
    return best


def wait_reason(pane_id):
    try:
        txt = call("pane.read", {"pane_id": pane_id, "source": "visible", "lines": 40}).get("read", {}).get("text", "")
    except Exception:
        try:
            txt = subprocess.run([HERDR, "pane", "read", pane_id], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            txt = ""
    lines = [re.sub(r"[│╭╮╰╯─┃]+", " ", l).strip() for l in txt.splitlines()]
    lines = [l for l in lines if l]
    for i in range(len(lines) - 1, -1, -1):
        if re.search(r"\?\s*$", lines[i]) and not re.match(r"^(❯|>|\d+\.)", lines[i]):
            q = lines[i]
            # prefer the command/tool line just above an approval question
            if re.search(r"(proceed|allow|approve|want to)", q, re.I) and i > 0:
                q = lines[i - 1]
            return re.sub(r"\s+", " ", q)[:60]
    return "waiting for you"


# ---------------- compute ----------------
def compute(opt, memo):
    agents = call("agent.list")["agents"]
    spaces = {w["workspace_id"]: w for w in call("workspace.list")["workspaces"]}
    try:
        tl = call("tab.list")
        tl = tl.get("tabs", tl) if isinstance(tl, dict) else tl
        tabs = {t["tab_id"]: (t.get("label") or "") for t in tl}
    except Exception:
        tabs = {}
    now = time.time()
    stale_after = seconds(opt["stale_after"])
    seen = memo.setdefault("since", {})
    tcache = memo.setdefault("tcache", {})
    rows = []
    for a in agents:
        pid = a["pane_id"]
        st = a["agent_status"] if a["agent_status"] in PRIO else "unknown"
        seq = a.get("state_change_seq", 0)
        rec = seen.get(pid)
        if not rec or rec["seq"] != seq:
            first = rec is None
            start = now
            if first:  # unknown start: best-effort from the transcript
                t = transcript_mtime(a.get("cwd"), a.get("terminal_title_stripped"), tcache)
                start = t if t else None
            rec = seen[pid] = {"seq": seq, "since": start}
        age = None if rec["since"] is None else max(0, int(now - rec["since"]))
        stale = st == "idle" and age is not None and age >= stale_after
        ws = spaces.get(a["workspace_id"], {})
        rows.append(dict(pid=pid, st=st, stale=stale, age=age, seq=seq, kind=a.get("agent") or "?",
                         space=ws.get("label", a["workspace_id"]), wsid=a["workspace_id"],
                         title=(a.get("terminal_title_stripped") or "").strip(),
                         tab=tab_label(tabs.get(a.get("tab_id"), ""), opt["show_tab"])))
    for pid in list(seen):
        if pid not in {r["pid"] for r in rows}:
            seen.pop(pid)

    width = sidebar_width()
    pins = load_pins()
    pins["agents"] = [x for x in pins["agents"] if x in {r["pid"] for r in rows}]
    kinds = {r["kind"] for r in rows}
    show_kind = opt["show_kind"] == "always" or (opt["show_kind"] == "auto" and len(kinds) > 1)
    mode = opt["group_by"] if opt["group_by"] in ("kind", "project") else "none"
    group = mode != "none"
    # rank: freshest first within a tier (unknown ages sink, by seq); stable between state changes
    for r in rows:
        since = seen[r["pid"]]["since"]
        r["rank"] = f"{10**10 - int(since):010d}" if since is not None else f"9{10**9 - r['seq'] % 10**9:09d}"
        r["pinned"] = r["pid"] in pins["agents"]
        r["key"] = (0 if r["pinned"] else 1, -PRIO[r["st"]], r["rank"])   # the current (ungrouped) sort
        r["g"] = r["kind"] if mode == "kind" else r["wsid"]
    # groups ordered by their highest-ranked agent, then by that agent's key; members keep the current sort
    best = {}
    for r in rows:
        best[r["g"]] = min(best.get(r["g"], r["key"]), r["key"])
    if mode == "project":  # pinned spaces lead the project groups
        best = {g: ((0 if g in pins["spaces"] else 1),) + k for g, k in best.items()}
    gorder = sorted(best, key=lambda g: (best[g], str(g)))
    gidx = {g: i + 1 for i, g in enumerate(gorder)}
    order = sorted(rows, key=lambda r: ((gidx[r["g"]] if group else 0), r["key"]))
    firsts, lasts = set(), set()
    if group:
        prev = None
        for i, r in enumerate(order):
            if r["g"] != prev:
                firsts.add(r["pid"])
                if i:
                    lasts.add(order[i - 1]["pid"])
            prev = r["g"]
    # disambiguation
    dup = {}
    for r in rows:  # agents that would render identically: same space AND same visible glyph
        r["vis"] = "stale" if r["stale"] else r["st"]
        dup.setdefault((r["space"], r["tab"], r["vis"]), []).append(r)

    out = {}
    for r in rows:
        label = r["space"]
        if opt["label"] == "task" and r["title"]:
            label = r["title"]
        glyph = GLYPH["stale"] if r["stale"] else GLYPH[r["st"]]
        t = {"rank": r["rank"]}
        if group:
            t["grp"] = f"{gidx[r['g']]:04d}"
        if mode == "project":
            # project named once (first agent of the group); members indented under it, identified by task
            task = r["tab"] or (short_tag(r["title"], 18) if r["title"] else "")
            if r["pid"] in firsts:
                parts = [r["space"]] + ([task] if task else [])
            else:
                parts = [BLANK * 2 + (task or r["space"])]
            if show_kind:
                parts.append(r["kind"])
        else:
            parts = [label] + ([r["tab"]] if r["tab"] and opt["label"] == "space" else [])
            if opt["disambiguate"] and opt["label"] == "space" and len(dup[(r["space"], r["tab"], r["vis"])]) > 1 and r["title"]:
                parts.append(short_tag(r["title"]))
            # kind-group labels only mean something when more than one kind is live
            if (mode == "kind" and r["pid"] in firsts and len(kinds) > 1) or (show_kind and not group):
                parts.append(r["kind"])
        body = " · ".join(parts) + (" ★" if r["pinned"] else "")
        t["head"] = pinned_row(glyph, body, fmt_age(r["age"]), width)
        if r["pinned"]:
            t["pin"] = "0"
        if mode == "kind" and r["pid"] in lasts and len(kinds) > 1:
            t["rule"] = "─" * 26
        if opt["show_task"] != "never" and r["st"] == "blocked":
            t["wait"] = "↳ " + wait_reason(r["pid"])
        if opt["show_task"] in ("attention", "all") and r["st"] == "done" and opt["label"] == "space":
            t["done"] = "↳ " + (r["title"] or "done")
        if opt["show_task"] == "all" and r["st"] not in ("blocked", "done") and r["title"] and opt["label"] == "space":
            t["task"] = r["title"]
        out[r["pid"]] = t

    # spaces: parent alert for blocked worktree children, dirty marker
    wout = {}
    by_repo = {}
    for w in spaces.values():
        wt = w.get("worktree") or {}
        if wt.get("repo_key"):
            by_repo.setdefault(wt["repo_key"], []).append(w)
    for ws_list in by_repo.values():
        parents = [w for w in ws_list if not (w.get("worktree") or {}).get("is_linked_worktree")]
        kids = [w for w in ws_list if (w.get("worktree") or {}).get("is_linked_worktree")]
        n = sum(1 for k in kids for r in rows if r["wsid"] == k["workspace_id"] and r["st"] == "blocked")
        for p in parents:
            if n:
                wout.setdefault(p["workspace_id"], {})["alert"] = f"!{n}"
    if now - memo.get("dirty_at", 0) > 30:
        memo["dirty_at"] = now
        memo["dirty"] = {}
        cwd_of = {}
        try:
            panes = call("pane.list")
            panes = panes.get("panes", panes) if isinstance(panes, dict) else panes
        except Exception:
            panes = agents
        for pn in panes:
            cwd_of.setdefault(pn["workspace_id"], pn.get("cwd"))
        for w in spaces.values():
            cwd = (w.get("worktree") or {}).get("checkout_path") or cwd_of.get(w["workspace_id"])
            if not cwd:
                continue
            try:
                r = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, text=True, timeout=2)
                if r.returncode == 0 and r.stdout.strip():
                    memo["dirty"][w["workspace_id"]] = True
            except Exception:
                pass
    for wid in memo.get("dirty", {}):
        wout.setdefault(wid, {})["dirty"] = "±"
    for wid in pins["spaces"]:
        if wid in spaces:
            wout.setdefault(wid, {})["spin"] = "★"
    memo["space_plan"] = plan_spaces(opt, spaces, rows, pins, memo, now)

    view = view_params(opt, mode)
    return out, wout, view


def plan_spaces(opt, spaces, rows, pins, memo, now):
    """Desired full workspace order (top-level spaces sorted; worktree children follow their parent)."""
    cur = [w["workspace_id"] for w in sorted(spaces.values(), key=lambda w: w.get("number", 0))]
    parent = {}
    by_repo = {}
    for w in spaces.values():
        wt = w.get("worktree") or {}
        if wt.get("repo_key"):
            by_repo.setdefault(wt["repo_key"], []).append(w)
    for ws in by_repo.values():
        tops = [w for w in ws if not (w.get("worktree") or {}).get("is_linked_worktree")]
        if tops:
            for w in ws:
                if (w.get("worktree") or {}).get("is_linked_worktree"):
                    parent[w["workspace_id"]] = tops[0]["workspace_id"]
    top = [w for w in cur if w not in parent]
    focus = memo.setdefault("focus", {})
    for w in spaces.values():
        if w.get("focused"):
            focus[w["workspace_id"]] = now
    sort = opt["space_sort"]
    if not memo.get("manual"):
        memo["manual"] = top[:]           # snapshot the user's order before any reorder ever happens
    last = memo.get("last_sort")
    memo["last_sort"] = sort
    restoring = sort == "manual" and last not in (None, "manual") and memo.get("manual")
    if sort == "manual" and not restoring:
        memo["manual"] = top[:]           # the user's own order is the source of truth in manual mode
    saved = memo.get("manual") or top
    base = [w for w in saved if w in top] + [w for w in top if w not in saved]
    if sort == "alpha":
        base = sorted(top, key=lambda w: spaces[w].get("label", "").casefold())
    elif sort == "recent":
        base = sorted(top, key=lambda w: -focus.get(w, 0))
    elif sort == "activity":
        tier = {}
        for r in rows:
            w = parent.get(r["wsid"], r["wsid"])
            tier[w] = max(tier.get(w, -1), PRIO[r["st"]])
        base = sorted(base, key=lambda w: -tier.get(w, -1))       # stable: manual order breaks ties
    pinned = [w for w in pins["spaces"] if w in top]
    base = pinned + [w for w in base if w not in pinned]
    want = []
    for w in base:
        want.append(w)
        want += [c for c in cur if parent.get(c) == w]
    want += [w for w in cur if w not in want]
    if sort == "manual" and not pinned and not restoring:
        want = cur                          # never touch the user's order unless asked (pins / other sorts)
    return {"cur": cur, "want": want}


def view_params(opt, mode):
    group = mode != "none"
    if group:
        sort = [{"field": {"token": "grp"}, "order": "asc"}, {"field": {"token": "pin"}, "order": "asc"},
                {"field": "attention", "order": "desc"}, {"field": {"token": "rank"}, "order": "asc"}]
    else:  # pinned first (pin token only exists on pinned agents; missing values sort last)
        sort = [{"field": {"token": "pin"}, "order": "asc"}, {"field": "attention", "order": "desc"},
                {"field": {"token": "rank"}, "order": "asc"}]
    glabel = {"kind": "by kind", "project": "by project"}.get(mode)
    p = {"source": f"plugin:{PID}", "label": glabel or opt["view"], "sort": sort}
    need = {"op": "in", "field": "status", "values": ["blocked", "done"]}
    if opt["view"] == "needs me":
        p["filter"] = need
    elif opt["view"] == "here+":
        p["filter"] = {"op": "any", "filters": [
            {"op": "eq", "field": "workspace_id", "value": {"context": "current_workspace_id"}}, need]}
    if group and opt["view"] != "triage":
        p["label"] = f"{glabel} · {opt['view']}"
    return p


def apply(out, wout, view, memo):
    last = memo.setdefault("pushed", {})
    for pid, t in out.items():
        prev = last.get(pid, {})
        if prev != t:
            changed = {k: v for k, v in t.items() if prev.get(k) != v}
            gone = [k for k in prev if k not in t]
            report("pane", pid, changed, gone)
            last[pid] = t
    for pid in list(last):
        if pid not in out:
            last.pop(pid)
    wl = memo.setdefault("wpushed", {})
    for wid in set(wout) | set(wl):
        t, prev = wout.get(wid, {}), wl.get(wid, {})
        if t != prev:
            report("workspace", wid, {k: v for k, v in t.items() if prev.get(k) != v}, [k for k in prev if k not in t])
            wl[wid] = t
            if not t:
                wl.pop(wid)
    plan = memo.get("space_plan")
    if plan and plan["want"] != plan["cur"]:
        call("workspace.move_block", {"workspace_ids": plan["want"]})
        log("spaces reordered", " ".join(plan["want"]))
    if memo.get("view") != view:
        call("agent.view.set", view)
        memo["view"] = view


# ---------------- lifecycle ----------------
def alive():
    try:
        pid = int(open(PIDFILE).read())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def run():
    os.makedirs(STATE_DIR, exist_ok=True)
    open(PIDFILE, "w").write(str(os.getpid()))
    memo = {}
    try:
        old = json.load(open(MEMO))
        memo["since"], memo["tcache"] = old.get("since", {}), old.get("tcache", {})
        memo["focus"] = old.get("focus") or {}
        if old.get("manual"):
            memo["manual"] = old["manual"]
        memo["last_sort"] = old.get("last_sort")
    except Exception:
        pass
    woke = {"flag": False}
    signal.signal(signal.SIGUSR1, lambda *_: woke.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("started", os.getpid())
    while True:
        try:
            opt = options()
            apply(*compute(opt, memo), memo)
            json.dump({k: memo.get(k) for k in ("since", "tcache", "focus", "manual", "last_sort")}, open(MEMO, "w"))
        except Exception as e:
            log("error", repr(e))
            memo.pop("view", None)
        tick = max(2, int(options().get("tick_seconds", 5)))
        for _ in range(tick * 10):
            if woke["flag"]:
                break
            time.sleep(0.1)
        woke["flag"] = False


def clear_all():
    try:
        for a in call("agent.list")["agents"]:
            report("pane", a["pane_id"], {}, TOKENS)
        for w in call("workspace.list")["workspaces"]:
            report("workspace", w["workspace_id"], {}, ["alert", "dirty", "spin"])
        call("agent.view.clear", {"source": f"plugin:{PID}"})
    except Exception as e:
        log("clear error", repr(e))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        run()
    elif cmd == "spawn":
        if not alive():
            os.makedirs(STATE_DIR, exist_ok=True)
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "run"], start_new_session=True,
                             stdout=open(LOG, "a"), stderr=subprocess.STDOUT, env=os.environ.copy())
            time.sleep(0.3)
        print("running", alive())
    elif cmd == "poke":
        p = alive()
        if p:
            os.kill(p, signal.SIGUSR1)
        else:
            main_spawn()
    elif cmd == "stop":
        p = alive()
        if p:
            os.kill(p, signal.SIGTERM)
        clear_all()
        print("stopped")
    elif cmd == "pin":
        target, on = toggle_pin(sys.argv[2])
        p = alive()
        if p:
            os.kill(p, signal.SIGUSR1)
        try:
            call("notification.show", {"title": "covr", "body": f"{'pinned' if on else 'unpinned'} {target}" if target else "nothing focused"})
        except Exception:
            pass
        print(target, on)
    elif cmd == "set":
        v = write_option(sys.argv[2], sys.argv[3])
        p = alive()
        if p:
            os.kill(p, signal.SIGUSR1)
        try:
            call("notification.show", {"title": "covr", "body": f"{sys.argv[2]} = {v}"})
        except Exception:
            pass
        print(sys.argv[2], "=", v)
    else:
        print("running" if alive() else "stopped", alive() or "")


def main_spawn():
    sys.argv = [sys.argv[0], "spawn"]
    main()


if __name__ == "__main__":
    main()
