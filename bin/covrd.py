#!/usr/bin/env python3
"""covr daemon (covr.sidebar) — computes sidebar tokens from live herdr state.

usage: covrd.py startup | spawn | run | poke | stop | status | settings | set <option> <value|cycle> | pin agents|spaces

Tokens pushed per agent pane (source "covr"): head (glyph + label, coloured by a
glyph-prefix rule), age / age_stale, kind, tag, wait, done, task, rule, rank, kgrp.
Per workspace: alert, dirty. Options live in $HERDR_PLUGIN_CONFIG_DIR/config.toml;
the daemon never rewrites herdr's own config.toml (only the install-layout action does, see layout.py).

One daemon per herdr session (socket): its pid, lock, memo and log live in
$HERDR_PLUGIN_STATE_DIR/s/<hash of the socket path>/. Pins are shared by all sessions.
"""
import errno, glob, hashlib, json, os, re, signal, socket, subprocess, sys, time, unicodedata

WIN = os.name == "nt"
if WIN:
    import msvcrt
else:
    import fcntl
# Windows: no console window flashing up for the daemon's git / herdr child processes
NOWIN = {"creationflags": 0x08000000} if WIN else {}  # CREATE_NO_WINDOW

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

PID = "covr.sidebar"
SRC = "covr"
HERE = os.path.dirname(os.path.abspath(__file__))
def _herdr_home(kind):
    """herdr's own config (or state) directory. herdr passes the plugin's dirs, <herdr dir>/plugins/<config|>/<id>,
    so walk up from those; otherwise use the platform default."""
    env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR" if kind == "config" else "HERDR_PLUGIN_STATE_DIR")
    tail = ("plugins", "config", PID) if kind == "config" else ("plugins", PID)
    if env:
        parts = os.path.normpath(env).split(os.sep)
        if tuple(parts[-len(tail):]) == tail:
            return os.sep.join(parts[:-len(tail)])
    if WIN:
        return os.path.join(os.environ.get("APPDATA" if kind == "config" else "LOCALAPPDATA") or os.path.expanduser("~"), "herdr")
    base = (os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) if kind == "config" \
        else (os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"))
    return os.path.join(base, "herdr")


HERDR_CONFIG_DIR, HERDR_STATE_DIR = _herdr_home("config"), _herdr_home("state")
CFG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(HERDR_CONFIG_DIR, "plugins", "config", PID)
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(HERDR_STATE_DIR, "plugins", PID)
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
SOCK = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(HERDR_CONFIG_DIR, "herdr.sock")
SESSION = hashlib.sha1(SOCK.encode()).hexdigest()[:12]
RUN_DIR = os.path.join(STATE_DIR, "s", SESSION)
PIDFILE, LOCK, MEMO, LOG, WAKE = (os.path.join(RUN_DIR, n) for n in ("covrd.pid", "covrd.lock", "memo.json", "covrd.log", "wake"))
LEGACY_PID, LEGACY_MEMO = os.path.join(STATE_DIR, "covrd.pid"), os.path.join(STATE_DIR, "memo.json")  # before 0.2
HERDR_CONFIG = os.path.join(HERDR_CONFIG_DIR, "config.toml")
GONE_AFTER = 60  # seconds without a reachable socket before the daemon decides its server is gone for good
LOG_MAX = 256 * 1024  # covrd.log rotates to covrd.log.1 past this size
TOKEN_MAX = 80        # herdr caps token values at 80 characters
MIN_GAP = 0.5         # seconds between recomputes when hooks fire in bursts (pane.focused on every focus change)
FIGHT_MOVES, FIGHT_WINDOW, FIGHT_PAUSE = 3, 60, 300  # re-applying one order 3x in 60 s pauses space sorting 5 min
ZW = "​"

DEFAULTS = {"layout": "auto", "age_source": "observed", "seen_after": "5s", "label": "space", "show_kind": "never", "group_by": "none", "show_task": "attention",
            "disambiguate": True, "stale_after": "30m", "view": "triage", "space_sort": "manual", "show_tab": "named", "tick_seconds": 5}
CYCLES = {"view": ["triage", "needs me", "here+"], "group_by": ["none", "project", "kind"],
          "show_kind": ["never", "auto", "always"], "label": ["space", "task"],
          "show_task": ["attention", "all", "never"], "space_sort": ["manual", "alpha", "recent", "activity"],
          "show_tab": ["named", "always", "never"], "layout": ["auto", "light", "dark"],
          "age_source": ["observed", "claude-transcripts"]}
GLYPH = {"blocked": "×", "done": "✓", "working": "◐", "idle": "○", "unknown": "·", "stale": "◗"}
PRIO = {"blocked": 4, "done": 3, "working": 2, "idle": 1, "unknown": 0}
PINS = os.path.join(STATE_DIR, "pins.json")
TOKENS = ["pin", "head", "age", "age_stale", "kind", "tag", "wait", "done", "task", "rule", "rank", "kgrp", "grp"]


def log(*a):
    os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")  # one rotation: the previous log is kept, older ones dropped
    except OSError:
        pass
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


_once = set()


def log_once(key, *a):
    """Log (and toast) a problem once per daemon lifetime instead of on every tick."""
    if key in _once:
        return
    _once.add(key)
    log(*a)
    notify(" ".join(str(x) for x in a)[:240])


def read_text(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# ---------------- herdr access ----------------
def call(method, params=None):
    """One request to herdr's API: newline-delimited JSON over its local socket, which is a Unix socket
    on Linux/macOS and a named pipe (\\\\.\\pipe\\<socket path>) on Windows."""
    req = (json.dumps({"id": "covr", "method": method, "params": params or {}}) + "\n").encode()
    buf = b""
    if WIN:
        for attempt in range(20):
            try:
                with open("\\\\.\\pipe\\" + SOCK, "r+b", buffering=0) as f:
                    f.write(req)
                    while not buf.endswith(b"\n"):
                        c = f.read(1 << 16)
                        if not c:
                            break
                        buf += c
                break
            except OSError as e:
                # A pipe server makes a new instance after each client, so for a moment there is none
                # (file not found) or all are taken (231, ERROR_PIPE_BUSY). Retry briefly before deciding
                # the server is gone: 0.3 s for "not found", 1 s for "busy".
                missing = isinstance(e, FileNotFoundError)
                if not (missing or getattr(e, "winerror", None) == 231) or attempt >= (5 if missing else 19):
                    raise
                buf = b""
                time.sleep(0.05)
    else:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        s.connect(SOCK)
        s.sendall(req)
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
    subprocess.run([HERDR, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **NOWIN)


SEQ = {"last": 0, "on": True}


def next_seq():
    """Strictly increasing report sequence: herdr drops a report whose --seq is not above the last one it
    accepted for that target and source, so a late or out-of-order write (a tick still running while
    `stop` clears) cannot win. Wall-clock milliseconds, never below the last value used (persisted in the
    memo), so it survives daemon restarts and a clock stepping back."""
    SEQ["last"] = max(SEQ["last"] + 1, int(time.time() * 1000))
    return SEQ["last"]


def report(kind, target, set_=None, clear=()):
    args = [kind, "report-metadata", target, "--source", SRC]
    if SEQ["on"]:
        args += ["--seq", str(next_seq())]
    for k, v in (set_ or {}).items():
        args += ["--token", f"{k}={v}"]
    for k in clear:
        args += ["--clear-token", k]
    cli(*args)


# ---------------- options ----------------
def parse_value(v):
    """One TOML scalar as write_option writes it (or a hand edit): "str" / 'str', true/false, int.
    Returns (value, ok); a trailing # comment after the value is ignored."""
    v = v.strip()
    if v[:1] in ('"', "'"):
        q, i, out = v[0], 1, ""
        while i < len(v) and v[i] != q:
            if q == '"' and v[i] == "\\" and i + 1 < len(v):
                out += {"n": "\n", "t": "\t"}.get(v[i + 1], v[i + 1])
                i += 2
                continue
            out += v[i]
            i += 1
        return (out, True) if i < len(v) else (None, False)
    v = v.split("#", 1)[0].strip()
    if v in ("true", "false"):
        return v == "true", True
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v), True
    return None, False


def read_flat(path):
    """Top-level `key = value` lines of a TOML file, for Pythons without tomllib (< 3.11).
    Stops at the first [table] header, so only top-level keys are read."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("["):
                    break
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                val, ok = parse_value(v)
                if ok:
                    out[k.strip().strip('"')] = val
    except OSError:
        pass
    return out


DURATION = re.compile(r"(\d+)\s*([smhd]?)")


def validate(key, value):
    """(value, None) if valid for `key`, else (None, reason). Enum options must be one of CYCLES; booleans
    and ints must have those types (strings like "true" / "7" from the CLI are converted); stale_after is
    a duration like 45m / 2h / 1d between 1 minute and 30 days; tick_seconds is 2..60."""
    if key not in DEFAULTS:
        return None, f"unknown option {key!r}"
    d = DEFAULTS[key]
    if key in CYCLES:
        return (value, None) if value in CYCLES[key] else (None, f"{key} must be one of {', '.join(map(str, CYCLES[key]))}")
    if isinstance(d, bool):
        if isinstance(value, bool):
            return value, None
        v = str(value).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True, None
        if v in ("0", "false", "no", "off"):
            return False, None
        return None, f"{key} must be true or false"
    if key == "seen_after":
        v = str(value).strip().lower()
        if v == "off" or (DURATION.fullmatch(v) and 1 <= seconds(v) <= 3600):
            return v, None
        return None, "seen_after must be off or a duration between 1s and 1h, like 5s or 1m"
    if key == "stale_after":
        m = DURATION.fullmatch(str(value).strip())
        if not m or not 60 <= seconds(value) <= 30 * 86400:
            return None, "stale_after must be a duration between 1m and 30d, like 45m, 2h or 1d"
        return str(value).strip(), None
    if isinstance(d, int):
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None, f"{key} must be a whole number"
        return (v, None) if 2 <= v <= 60 else (None, f"{key} must be between 2 and 60")
    return value, None


def options():
    o = dict(DEFAULTS)
    p = os.path.join(CFG_DIR, "config.toml")
    if not os.path.exists(p):
        return o
    try:
        if tomllib:
            with open(p, "rb") as f:
                raw = tomllib.load(f)
        else:
            raw = read_flat(p)
    except Exception as e:  # a torn or hand-broken file: keep defaults for this tick
        log_once(("unreadable", repr(e)), "options file unreadable, using defaults:", repr(e))
        return o
    for k, v in raw.items():
        if k not in DEFAULTS:
            continue  # an old or misspelled key: ignored
        val, why = validate(k, v)
        if why:
            log_once(("invalid", k, repr(v)), f"option {k} = {v!r} ignored ({why}); using {DEFAULTS[k]!r}")
        else:
            o[k] = val
    return o


def write_option(key, value):
    o = options()
    if value == "cycle":
        seq = CYCLES[key]
        value = seq[(seq.index(o[key]) + 1) % len(seq)] if o[key] in seq else seq[0]
    else:
        value, why = validate(key, value)
        if why:
            raise ValueError(why)
    o[key] = value
    os.makedirs(CFG_DIR, exist_ok=True)
    text = "# covr options — edited by actions; the daemon picks changes up live\n"
    for k in DEFAULTS:
        v = o[k]
        text += f"{k} = {json.dumps(v) if not isinstance(v, bool) else str(v).lower()}\n"
    write_atomic(os.path.join(CFG_DIR, "config.toml"), text)
    return value


def write_atomic(path, text):
    """Readers never see a half-written file (the popup, actions and the daemon share these files)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)  # keep an existing file's mode (herdr's config.toml)
    except OSError:
        os.chmod(tmp, 0o600)  # new plugin files: owner-only
    os.replace(tmp, path)


def load_pins():
    p = read_json(PINS, {})
    if not isinstance(p, dict):
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
    write_atomic(PINS, json.dumps(p))
    return target, on


def seconds(s):
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", str(s).strip())
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 3600


BLANK = "\u2800"  # braille blank: renders empty but is not whitespace, so herdr does not trim it


def cells(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def fnv1a64(data):
    h = 0xcbf29ce484222325
    for b in data:
        h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
    return h


def client_prefs_path():
    """herdr keeps each session's dragged sidebar width in client-shell/local-<fnv1a64(client socket)>.json;
    the client socket sits next to the server socket (…/herdr-client.sock)."""
    client = os.path.join(os.path.dirname(SOCK), "herdr-client.sock")
    return os.path.join(HERDR_STATE_DIR, "client-shell", f"local-{fnv1a64(client.encode()):016x}.json")


def sidebar_width():
    """This session's sidebar width: the dragged width (its client prefs), else ui.sidebar_width, else 26.
    Anything outside 10..200 cells is treated as missing."""
    prefs = read_json(client_prefs_path(), {})
    for w in ((prefs.get("sidebar_width") if isinstance(prefs, dict) else None),
              herdr_config_value("ui", "sidebar_width")):
        try:
            w = int(w)
        except (TypeError, ValueError):
            continue
        if 10 <= w <= 200:
            return w
    return 26


def herdr_config_value(table, key, text=None):
    """One scalar from herdr's config.toml (or from `text`), e.g. ("ui", "sidebar_width") or ("theme", "name").
    Uses tomllib when available, else a line scan of that [table]."""
    try:
        if text is None:
            with open(HERDR_CONFIG, encoding="utf-8") as f:
                text = f.read()
        if tomllib:
            return tomllib.loads(text).get(table, {}).get(key)
        cur = None
        for line in text.splitlines():
            s = line.strip()
            m = re.match(r"^\[(\[?)\s*([^\]]+?)\s*\]", s)
            if m:  # [table] or [[array.of.tables]] (the latter never matches a plain table name)
                cur = ("[]" if m.group(1) else "") + m.group(2)
            elif cur == table and re.match(rf"^{re.escape(key)}\s*=", s):
                val, ok = parse_value(s.split("=", 1)[1])
                return val if ok else None
    except Exception:
        pass
    return None


def pinned_row(glyph, text, age, width):
    """One composed row: 'glyph text' left, age flush right. Usable cells = width - 1 indent - 1 divider."""
    usable = min(width - 3, TOKEN_MAX)   # 1 indent + 1 gap + 1 divider; never past herdr's 80-character cap
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


def clip(text, limit=TOKEN_MAX):
    """Shorten a token value to herdr's cap ourselves (with …) instead of letting herdr cut it mid-word."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


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
def tkey(cwd, title):
    """Cache key for an agent's transcript lookup: a hash, so no repo path or session title is stored."""
    return "h:" + hashlib.sha1(f"{cwd}|{title}".encode()).hexdigest()[:16]


def file_contains(path, text, limit=64 << 20):
    """Does the file contain `text`? Reads in chunks (up to `limit` bytes), keeping an overlap so a match
    across a chunk boundary is found."""
    needle, tail, seen = text.encode("utf-8"), b"", 0
    try:
        with open(path, "rb") as f:
            while seen < limit:
                chunk = f.read(1 << 20)
                if not chunk:
                    return False
                if needle in tail + chunk:
                    return True
                tail, seen = chunk[-(len(needle) - 1):] if len(needle) > 1 else b"", seen + len(chunk)
    except OSError:
        pass
    return False


def transcript_mtime(cwd, title, cache):
    """Best-effort 'last activity' for an agent whose state began before the daemon saw it:
    newest Claude transcript in the cwd's project dir that mentions the session title."""
    if not cwd or not title:
        return None
    key = tkey(cwd, title)
    if key in cache:
        return cache[key]
    slug = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    files = []
    for root in glob.glob(os.path.expanduser("~/.claude*/projects")):
        files += glob.glob(os.path.join(root, slug, "*.jsonl"))
    files.sort(key=lambda p: -os.path.getmtime(p))
    best = None
    for p in files[:25]:
        if file_contains(p, title):
            best = os.path.getmtime(p)
            break
    cache[key] = best
    return best


def mark_seen(pane_id, opt, memo, now):
    """A finished agent stays "done" until herdr's server sees an explicit focus on it. When it is already
    the selected pane (e.g. it finished while the terminal window was in the background), nothing ever
    focuses it again, so the ✓ would stay. After `seen_after` we focus it where it is, which marks it viewed
    and moves nothing. Returns True once it has been marked."""
    if str(opt["seen_after"]).lower() == "off":
        return False
    due = memo.setdefault("seen_due", {}).setdefault(pane_id, now + seconds(opt["seen_after"]))
    if now < due:
        return False
    try:
        call("agent.focus", {"target": pane_id})
    except Exception as e:
        log("mark seen failed", pane_id, repr(e))
        memo["seen_due"][pane_id] = now + max(5, seconds(opt["seen_after"]))  # try again later, never spin
        return False
    memo["seen_due"].pop(pane_id, None)
    return True


def wait_reason(pane_id):
    try:
        txt = call("pane.read", {"pane_id": pane_id, "source": "visible", "lines": 40}).get("read", {}).get("text", "")
    except Exception:
        try:
            txt = subprocess.run([HERDR, "pane", "read", pane_id], capture_output=True, encoding="utf-8", errors="replace", timeout=5, **NOWIN).stdout
        except Exception:
            txt = ""
    # blank lines (and box rules, which strip to nothing) stay in as paragraph breaks
    lines = [re.sub(r"[│╭╮╰╯─┃]+", " ", l).strip() for l in txt.splitlines()]
    for i in range(len(lines) - 1, -1, -1):
        # a question ends in ? (or a full-width ？), maybe followed by a [y/N] choice
        if re.search(r"[?？]\s*(\[[^\]]{1,9}\])?\s*$", lines[i]) and not re.match(r"^(❯|>|\d+\.)", lines[i]):
            # prefer the command/tool line just above an approval question
            if re.search(r"(proceed|allow|approve|want to)", lines[i], re.I):
                above = [k for k in range(i) if lines[k]]
                if above:
                    return reason_text(lines[above[-1]])
            header = question_header(lines, i)
            return reason_text((header + ": " if header else "") + unmark(paragraph(lines, i)))
    return "waiting for you"


TAB = r"[☐☒✔]\s+\S.*?"
TABS = re.compile(rf"^(←\s+)?{TAB}(\s{{2,}}{TAB})*(\s+→)?$")


def question_header(lines, i, look=12):
    """Claude's multiple-choice form labels each question with a short header on a tab line above it
    (`←  ☐ Deploy  ☐ Rollback  ✔ Submit  →`, or ` ☐ Deploy` for one question). The first unanswered ☐ is
    the question on screen. Only a real form counts: a line of nothing but tabs, with the form's footer
    (`Enter to select · … · Esc to cancel`) below the question, so a todo list (`☐ Run the tests`) never becomes a header."""
    if not any(re.search(r"^Enter to select|Esc to cancel", l) for l in lines[i + 1:]):  # either half, if it wraps
        return ""
    for line in reversed(lines[max(0, i - look):i]):
        if TABS.match(line):
            tabs = re.findall(r"☐\s+(.+?)(?=\s{2,}|\s*[☐☒✔→]|$)", line)
            return tabs[0].strip() if tabs else ""
    return ""


def paragraph(lines, i, most=4):
    """The text ends at lines[i], but a long question wraps: walk up to where its paragraph starts (a blank
    line, a ● message marker or a numbered option), so the row shows the start, not the wrapped tail."""
    j = i
    while j > 0 and i - j < most and lines[j - 1] and not re.match(r"^(●|⏺|❯|>|\d+\.)", lines[j]) \
            and not re.match(r"^(❯|>|\d+\.)", lines[j - 1]):
        j -= 1
    return " ".join(lines[j:i + 1])


def unmark(text):
    """Drop the leading message/bullet markers (● ⏺ ☐ ✻ * •)."""
    return re.sub(r"^[●⏺☐✻*•\s]+", "", text)


def reason_text(text, limit=60):
    """One line for the row: markers dropped, whitespace collapsed, cut at a word with …"""
    text = re.sub(r"\s+", " ", unmark(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" ,;:—-") + "…"


# ---------------- compute ----------------
def compute(opt, memo):
    agents = call("agent.list")["agents"]
    memo["live"] = {a["pane_id"]: set((a.get("tokens") or {}).keys()) for a in agents}
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
        if st == "done" and a.get("focused") and mark_seen(pid, opt, memo, now):
            st = "idle"  # the agent you have selected has been finished long enough: it counts as viewed
        rec = seen.get(pid)
        if not rec or rec["seq"] != seq:
            first = rec is None
            start = now
            if first:  # already in this state when we first saw it: the start is unknown ...
                start = None
                if opt["age_source"] == "claude-transcripts":  # ... unless the user lets us look it up (opt-in)
                    start = transcript_mtime(a.get("cwd"), a.get("terminal_title_stripped"), tcache) or None
            rec = seen[pid] = {"seq": seq, "since": start, "st": st}
        elif rec.get("st") not in (None, st):
            # the visible state changed without herdr counting a state change: a finished agent was viewed
            # (done -> idle). The timer restarts, so "idle" and "asleep" count from when you viewed it.
            rec["since"] = now
        rec["st"] = st
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
    pending = memo.setdefault("seen_due", {})
    for pid in list(pending):  # only agents that are still finished and still selected stay due
        if str(opt["seen_after"]).lower() == "off" or not any(a["pane_id"] == pid and a.get("agent_status") == "done" and a.get("focused") for a in agents):
            pending.pop(pid)
    memo["wake_at"] = min(pending.values()) if pending else None
    live_keys = {tkey(a.get("cwd"), a.get("terminal_title_stripped")) for a in agents}
    for k in list(tcache):
        if k not in live_keys:
            tcache.pop(k)  # also drops pre-0.3 raw "cwd|title" keys
    focus = memo.setdefault("focus", {})
    for w in list(focus):
        if w not in spaces:
            focus.pop(w)
    if memo.get("manual"):
        memo["manual"] = [w for w in memo["manual"] if w in spaces]

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
            if opt["disambiguate"] and r["tab"] and len(dup[(r["space"], r["tab"], r["vis"])]) > 1 and r["title"]:
                parts.append(short_tag(r["title"]))  # same named tab, same state: the tab alone can't tell them apart
            if show_kind:
                parts.append(r["kind"])
        else:
            parts = [label] + ([r["tab"]] if r["tab"] and opt["label"] == "space" else [])
            if opt["disambiguate"] and opt["label"] == "space" and len(dup[(r["space"], r["tab"], r["vis"])]) > 1 and r["title"]:
                parts.append(short_tag(r["title"]))
            # grouped by kind, the first row names its group (only worth it when more than one kind is live);
            # show_kind = always puts the kind on every row whatever the grouping
            if opt["show_kind"] == "always" or (show_kind and mode != "kind") or (mode == "kind" and r["pid"] in firsts and len(kinds) > 1):
                parts.append(r["kind"])
        body = " · ".join(parts) + (" ★" if r["pinned"] else "")
        t["head"] = pinned_row(glyph, body, fmt_age(r["age"]), width)
        if r["pinned"]:
            t["pin"] = "0"
        if mode == "kind" and r["pid"] in lasts and len(kinds) > 1:
            t["rule"] = "─" * 26
        if opt["show_task"] != "never" and r["st"] == "blocked":
            t["wait"] = clip("↳ " + wait_reason(r["pid"]))
        if opt["show_task"] in ("attention", "all") and r["st"] == "done" and opt["label"] == "space":
            t["done"] = clip("↳ " + (r["title"] or "done"))
        if opt["show_task"] == "all" and r["st"] not in ("blocked", "done") and r["title"] and opt["label"] == "space":
            t["task"] = clip(r["title"])
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
                # a repo's own config must not run commands here (core.fsmonitor executes on status),
                # and a background status must never take index.lock from under the user's git
                r = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", cwd,
                                    "status", "--porcelain", "--untracked-files=no"],
                                   capture_output=True, encoding="utf-8", errors="replace", timeout=2, **NOWIN)
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


def move_spaces(want, memo):
    """Reorder spaces, unless we keep having to re-apply the same order: then something (usually the user
    dragging spaces while a sort is active) is undoing it, and we pause sorting for a while instead of
    fighting. A different order (a real state change) is never held back."""
    g = memo.setdefault("fight", {"order": None, "times": [], "until": 0})
    now = time.time()
    if now < g["until"]:
        return
    if g["order"] == want:
        g["times"] = [t for t in g["times"] if now - t < FIGHT_WINDOW] + [now]
        if len(g["times"]) >= FIGHT_MOVES:
            g.update(until=now + FIGHT_PAUSE, times=[])
            log_once(("fight", tuple(want)), "spaces keep being moved back: pausing space sorting for "
                     f"{FIGHT_PAUSE // 60} min (switch space sort to manual to keep your own order)")
            return
    else:
        g.update(order=want, times=[now])
    res = call("workspace.move_block", {"workspace_ids": want})
    got = [w["workspace_id"] for w in sorted(res.get("workspaces", []), key=lambda w: w.get("number", 0))]
    log("spaces reordered", " ".join(want) + ("" if not got or got == want else f" (herdr left {' '.join(got)})"))


def lost_tokens(memo):
    """True when a still-live agent shows none of the tokens we pushed to it: its server restarted
    (or handed off) and dropped every token and the view, so all of it must be pushed again.
    (All of them, not some: herdr may drop a single value it sanitises to empty, and that must not
    turn into a full resync every tick.)"""
    live = memo.get("live") or {}
    for pid, t in (memo.get("pushed") or {}).items():
        if t and pid in live and not set(t) & live[pid]:
            return True
    return False


def apply(out, wout, view, memo):
    lost = lost_tokens(memo)
    memo["lost_streak"] = memo.get("lost_streak", 0) + 1 if lost else 0
    if lost and memo["lost_streak"] >= 3 and SEQ["on"]:
        # herdr keeps rejecting our reports: its last accepted --seq must be above ours (e.g. a clock
        # stepped back across a daemon restart). Unsequenced reports always apply, so use those.
        SEQ["on"] = False
        log_once("seq-off", "herdr is ignoring sequenced reports; sending them unsequenced from now on")
    if memo.pop("resync", False) or lost:
        log("resync: pushing every token and the view again")
        for k in ("pushed", "wpushed", "view"):
            memo.pop(k, None)
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
        move_spaces(plan["want"], memo)
    if memo.get("view") != view:
        call("agent.view.set", view)
        memo["view"] = view


# ---------------- lifecycle ----------------
def _try_lock(fd, exclusive):
    """Non-blocking lock on the whole lock file. True if taken. flock on Unix; on Windows msvcrt byte-range
    locks (always exclusive; released by the OS when the process exits, like flock)."""
    try:
        if WIN:
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd):
    try:
        if WIN:
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def lock_held():
    """Is some process holding this session's daemon lock? (a momentary probe; never blocks)"""
    try:
        fd = os.open(LOCK, os.O_RDWR if WIN else os.O_RDONLY)
    except OSError:
        return False
    try:
        if _try_lock(fd, exclusive=False):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def alive():
    """pid of this session's daemon, or None. The lock (held for the daemon's lifetime) is the truth;
    the pidfile only says whom to signal."""
    if not lock_held():
        return None
    for _ in range(20):  # the lock holder writes its pid right after locking
        try:
            return int(read_text(PIDFILE, ""))
        except ValueError:
            time.sleep(0.05)
    return None


def take_lock():
    """Become this session's one daemon, or return None if another process already is.
    Retries briefly so a concurrent alive() probe (a momentary shared lock) cannot make us give up."""
    os.makedirs(RUN_DIR, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    for _ in range(20):
        if _try_lock(fd, exclusive=True):
            return fd
        time.sleep(0.05)
    os.close(fd)
    return None


def retire_legacy():
    """A daemon from before per-session state (global pidfile) would run beside the new one: stop it
    once, and carry its memo over (ages, focus times, the saved manual order) to the default session."""
    try:
        if WIN:
            raise ValueError("no pre-0.2 daemons on Windows")
        pid = int(read_text(LEGACY_PID, ""))
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, encoding="utf-8", errors="replace").stdout
        if "covrd.py" in cmd and " run" in cmd:
            os.kill(pid, signal.SIGTERM)
            log("stopped pre-0.2 daemon", pid)
    except (OSError, ValueError):
        pass
    try:
        os.remove(LEGACY_PID)
    except OSError:
        pass
    if os.path.exists(LEGACY_MEMO) and not os.path.exists(MEMO):
        os.makedirs(RUN_DIR, exist_ok=True)
        os.replace(LEGACY_MEMO, MEMO)


def plugin_enabled():
    """False once the user disables or unlinks the plugin (herdr does not stop our daemon for us)."""
    for p in call("plugin.list").get("plugins", []):
        if p.get("plugin_id") == PID:
            return bool(p.get("enabled"))
    return False


def server_unreachable(e):
    return isinstance(e, (FileNotFoundError, ConnectionRefusedError)) or \
        (isinstance(e, OSError) and e.errno in (errno.ENOENT, errno.ECONNREFUSED))


def code_stamp():
    """Changes when the plugin's code is updated in place (reinstall, git pull): the daemon then restarts
    itself, since a long-running process would otherwise keep running the old code forever."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return tuple(os.stat(os.path.join(here, f)).st_mtime_ns for f in ("covrd.py",))
    except OSError:
        return None


def run():
    lock = take_lock()
    if lock is None:
        return  # another daemon already serves this session
    stamp = code_stamp()
    reload_code = False
    write_atomic(PIDFILE, str(os.getpid()))
    memo = {}
    old = read_json(MEMO, {})
    if isinstance(old, dict):
        memo["since"], memo["tcache"] = old.get("since") or {}, old.get("tcache") or {}
        memo["focus"] = old.get("focus") or {}
        if old.get("manual"):
            memo["manual"] = old["manual"]
        memo["last_sort"] = old.get("last_sort")
        SEQ["last"] = int(old.get("seq") or 0)
    woke = {"flag": False, "stop": False}
    if WIN:
        try:
            os.remove(WAKE)  # requests addressed to an earlier daemon are stale
        except OSError:
            pass
    else:
        signal.signal(signal.SIGUSR1, lambda *_: woke.update(flag=True))
        signal.signal(signal.SIGUSR2, lambda *_: (memo.update(resync=True), woke.update(flag=True)))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("started", os.getpid(), "socket", SOCK)
    down_since = None
    last_tick = 0.0
    try:
        while True:
            if code_stamp() != stamp:
                log("plugin code updated: restarting the daemon")
                reload_code = True
                return
            try:
                if not plugin_enabled():
                    log("plugin disabled or unlinked: clearing tokens and exiting")
                    clear_all()
                    return
                opt = options()
                apply(*compute(opt, memo), memo)
                keep = {k: memo.get(k) for k in ("since", "tcache", "focus", "manual", "last_sort")}
                write_atomic(MEMO, json.dumps(dict(keep, seq=SEQ["last"])))
                last_tick = time.time()
                down_since = None
            except Exception as e:
                if server_unreachable(e):
                    down_since = down_since or time.time()
                    if time.time() - down_since >= GONE_AFTER:
                        log(f"server unreachable for {GONE_AFTER}s: exiting")
                        return
                    memo["resync"] = True  # whatever comes back up has none of our tokens
                else:
                    log("error", repr(e))
                    memo.pop("view", None)
            tick = options()["tick_seconds"]
            for _ in range(tick * 10):
                if WIN:
                    take_wake(woke, memo)
                    if woke["stop"]:
                        log("stop requested")
                        return
                if woke["flag"] and time.time() - last_tick >= MIN_GAP:
                    break  # a hook woke us; bursts (focus changes) coalesce into one recompute
                if memo.get("wake_at") and time.time() >= memo["wake_at"]:
                    break  # a selected, finished agent is due to be marked viewed
                time.sleep(0.1)
            woke["flag"] = False
    finally:
        try:
            if int(read_text(PIDFILE, "")) == os.getpid():
                os.remove(PIDFILE)
        except (OSError, ValueError):
            pass
        os.close(lock)  # releases the lock; the lock file itself stays (unlinking it would race a new daemon)
        if reload_code:  # same process id, same env, fresh code; the new code takes the lock again
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "run"])


def take_wake(woke, memo):
    """Windows has no SIGUSR1/2: hooks append a word to the wake file instead (wake | resync | stop)."""
    try:
        taken = WAKE + f".{os.getpid()}"
        os.replace(WAKE, taken)
    except OSError:
        return
    words = (read_text(taken, "") or "").split()
    try:
        os.remove(taken)
    except OSError:
        pass
    if words:
        woke["flag"] = True
    if "resync" in words:
        memo["resync"] = True
    if "stop" in words:
        woke["stop"] = True


def spawn():
    """Start this session's daemon unless it runs; safe to call from many hooks at once."""
    if alive():
        return alive()
    retire_legacy()
    os.makedirs(RUN_DIR, exist_ok=True)
    argv = [sys.executable, os.path.abspath(__file__), "run"]
    with open(LOG, "a") as out:  # the child keeps its own copy of the handle
        if WIN:
            # detached, no console, own process group; break away from the hook's job object when herdr allows
            # it, so the daemon outlives the hook that started it
            base = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            for flags in (base | 0x01000000, base):  # | CREATE_BREAKAWAY_FROM_JOB
                try:
                    subprocess.Popen(argv, creationflags=flags, stdin=subprocess.DEVNULL, stdout=out,
                                     stderr=subprocess.STDOUT, env=os.environ.copy())
                    break
                except OSError:
                    continue
        else:
            subprocess.Popen(argv, start_new_session=True, stdout=out, stderr=subprocess.STDOUT, env=os.environ.copy())
    for _ in range(30):
        if alive():
            break
        time.sleep(0.1)
    return alive()


def signal_daemon(kind="wake"):
    """Tell this session's daemon to recompute now ("wake"), push everything again ("resync"), or exit
    ("stop"). Signals on Unix; on Windows a word appended to the wake file, which the daemon checks every
    0.1 s. Returns the daemon's pid, or None if none runs."""
    p = alive()
    if p:
        try:
            if WIN:
                with open(WAKE, "a", encoding="utf-8") as f:
                    f.write(kind + "\n")
            else:
                os.kill(p, {"wake": signal.SIGUSR1, "resync": signal.SIGUSR2, "stop": signal.SIGTERM}[kind])
        except OSError:
            pass
    return p


def stop():
    p = signal_daemon("stop")
    for _ in range(50):  # wait for it to exit before clearing, so a last tick cannot re-push
        if not lock_held():
            break
        time.sleep(0.1)
    else:
        if p and WIN:
            try:
                os.kill(p, signal.SIGTERM)  # TerminateProcess; the OS releases its lock
            except OSError:
                pass
    SEQ["last"] = int((read_json(MEMO, {}) or {}).get("seq") or 0)
    clear_all()
    return p


def open_settings():
    """The settings popup is a plugin pane, so it runs with this session's plugin env."""
    r = subprocess.run([HERDR, "plugin", "pane", "open", "--plugin", PID, "--entrypoint", "settings"],
                       capture_output=True, encoding="utf-8", errors="replace", timeout=10, **NOWIN)
    if r.returncode != 0:
        busy = "ui_busy" in (r.stdout + r.stderr)
        notify("close the open dialog first, then retry" if busy else (r.stderr or r.stdout).strip()[:200])
    return r.returncode


def notify(body):
    try:
        call("notification.show", {"title": "covr", "body": body})
    except Exception:
        pass


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
    elif cmd == "startup":
        # [[startup]] also runs after a server restart / live handoff: a surviving daemon must re-push
        # everything (the new server has none of our tokens and no view), a missing one is started
        if not signal_daemon("resync"):
            spawn()
        print("running", alive())
    elif cmd == "spawn":
        print("running", spawn())
    elif cmd == "poke":
        if not signal_daemon():
            spawn()
    elif cmd == "stop":
        stop()
        print("stopped")
    elif cmd == "settings":
        sys.exit(open_settings())
    elif cmd == "pin":
        target, on = toggle_pin(sys.argv[2])
        signal_daemon()
        notify(f"{'pinned' if on else 'unpinned'} {target}" if target else "nothing focused")
        print(target, on)
    elif cmd == "set":
        key = sys.argv[2] if len(sys.argv) > 2 else ""
        if key not in DEFAULTS or len(sys.argv) < 4:
            notify(f"unknown option {key!r}")
            sys.exit(f"usage: covrd.py set <{'|'.join(DEFAULTS)}> <value|cycle>")
        try:
            v = write_option(key, sys.argv[3])
        except ValueError as e:
            notify(f"not changed: {e}")
            sys.exit(f"covrd.py set {key}: {e}")
        signal_daemon()
        notify(f"{key} = {v}")
        print(key, "=", v)
    else:
        p = alive()
        print(f"running {p}" if p else "stopped", f"(session {SESSION}, socket {SOCK})")


if __name__ == "__main__":
    main()
