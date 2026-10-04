#!/usr/bin/env python3
"""covr daemon (covr.sidebar) — computes sidebar tokens from live herdr state.

usage: covrd.py startup | spawn | run | poke | stop | status | settings | set <option> <value|cycle> | pin agents|spaces

Tokens pushed per agent pane (source "covr"): head (glyph + label, coloured by a
glyph-prefix rule), age / age_stale, kind, tag, wait, done, task, rule, rank, kgrp.
Per workspace: alert, dirty. Options live in $HERDR_PLUGIN_CONFIG_DIR/config.toml;
the daemon never rewrites herdr's own config.toml (only the install-layout action does, see layout.py).

One daemon per herdr session (socket): its pid, lock, memo and log live in
$HERDR_PLUGIN_STATE_DIR/s/<hash of the socket path>/. Pins are per session too (pins.json in that dir).
"""
import glob, hashlib, json, os, re, select, signal, socket, subprocess, sys, threading, time, traceback, unicodedata
from concurrent.futures import ThreadPoolExecutor

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
DEFAULT_SOCK = os.path.join(HERDR_CONFIG_DIR, "herdr.sock")
SOCK = os.environ.get("HERDR_SOCKET_PATH") or DEFAULT_SOCK
SESSION = hashlib.sha1(SOCK.encode()).hexdigest()[:12]
RUN_DIR = os.path.join(STATE_DIR, "s", SESSION)
PIDFILE, LOCK, MEMO, LOG, WAKE = (os.path.join(RUN_DIR, n) for n in ("covrd.pid", "covrd.lock", "memo.json", "covrd.log", "wake"))
STOPPED, SPAWN_LOCK = os.path.join(RUN_DIR, "stopped"), os.path.join(RUN_DIR, "spawn.lock")
LEGACY_PID, LEGACY_MEMO = os.path.join(STATE_DIR, "covrd.pid"), os.path.join(STATE_DIR, "memo.json")  # before 0.2
HERDR_CONFIG = os.path.join(HERDR_CONFIG_DIR, "config.toml")
GONE_AFTER = 60  # seconds without a reachable socket before the daemon decides its server is gone for good
LOG_MAX = 256 * 1024  # covrd.log rotates to covrd.log.1 past this size
TOKEN_MAX = 80        # herdr caps token values at 80 characters
MIN_GAP = 0.5         # seconds between recomputes when hooks fire in bursts (pane.focused on every focus change)
FIGHT_MOVES, FIGHT_WINDOW, FIGHT_PAUSE = 3, 60, 300  # re-applying one order 3x in 60 s pauses space sorting 5 min
ZW = "​"

DEFAULTS = {"layout": "auto", "age_source": "observed", "seen_after": "5s", "label": "space", "show_kind": "never", "group_by": "none", "show_task": "attention",
            "disambiguate": True, "kind_icon": "right", "stale_after": "30m", "view": "triage", "space_sort": "manual", "show_tab": "named", "tick_seconds": 5}
CYCLES = {"view": ["triage", "needs me", "here+"], "group_by": ["none", "project", "kind"],
          "show_kind": ["never", "auto", "always"], "label": ["space", "task"],
          "show_task": ["attention", "all", "never"], "space_sort": ["manual", "alpha", "recent", "activity"],
          "show_tab": ["named", "always", "never"], "kind_icon": ["right", "left", "inline", "off"], "layout": ["auto", "light", "dark"],
          "age_source": ["observed", "claude-transcripts"]}
# one single-width mark per agent kind, shown after the state glyph. Claude's ✻ and Gemini's ✦ are their own marks;
# the rest are the nearest plain shape. None of them reuses a state glyph (× ✓ ◐ ○ ◗ ·).
ICON = {"claude": "✻", "codex": "◈", "gemini": "✦", "grok": "⊘", "cursor": "◆", "github_copilot": "◉", "copilot": "◉",
        "opencode": "◫", "open_code": "◫", "amp": "▲", "droid": "▤", "pi": "π", "omp": "∏", "qwen": "✧", "kimi": "◍",
        "cline": "◘", "devin": "◇", "hermes": "☿", "letta": "λ", "kilo": "▣", "qoder": "◪", "qodercli": "◪",
        "agy": "◢", "kiro": "▽", "mastracode": "►"}
ICON_OTHER = "▫"
GLYPH = {"blocked": "×", "done": "✓", "working": "◐", "idle": "○", "unknown": "·", "stale": "◗"}
PRIO = {"blocked": 4, "done": 3, "working": 2, "idle": 1, "unknown": 0}
PINS = os.path.join(RUN_DIR, "pins.json")   # pane and workspace ids only mean something inside one session
LEGACY_PINS = os.path.join(STATE_DIR, "pins.json")  # before 0.6: one file shared by every session
WTOKENS = ["alert", "dirty", "spin"]
TOKENS = ["pin", "icon", "head", "icon_r", "age", "age_stale", "kind", "tag", "wait", "done", "task", "rule", "rank", "kgrp", "grp"]


def private_dir(path):
    """Plugin state is readable only by you: directories 0700 (the mode is ignored on Windows)."""
    os.makedirs(path, mode=0o700, exist_ok=True)


def open_log():
    """covrd.log for appending, created owner-only."""
    private_dir(RUN_DIR)
    return os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)


DAEMON = {"on": False}  # set in run(): the daemon's own stderr is covrd.log and follows its rotation


def log(*a):
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")  # one rotation: the previous log is kept, older ones dropped
            if DAEMON["on"] and not WIN:  # (on Windows its stdio is not the log: it must not hold it open)
                fd = open_log()  # a traceback must not go to the rotated (later deleted) file
                os.dup2(fd, 1)
                os.dup2(fd, 2)
                os.close(fd)
    except OSError:
        pass
    with os.fdopen(open_log(), "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


_once = set()


def log_once(key, *a):
    """Log (and toast) a problem once per daemon lifetime instead of on every tick."""
    if key in _once:
        return
    _once.add(key)
    log(*a)
    notify(" ".join(str(x) for x in a)[:240])


def read_text(path, default=None, newline=None):
    try:
        with open(path, encoding="utf-8", newline=newline) as f:
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
class HerdrError(RuntimeError):
    """herdr answered with an error; .code is its error code (pane_not_found, invalid_request, …)."""

    def __init__(self, method, err):
        self.code = err.get("code", "") if isinstance(err, dict) else ""
        self.message = err.get("message", "") if isinstance(err, dict) else str(err)
        super().__init__(f"{method}: {err}")


class ServerGone(OSError):
    """herdr's socket is missing, refuses us, or does not answer within CALL_TIMEOUT."""


CALL_TIMEOUT = 5


def _pipe_exchange(req):
    """Windows: one request over the named pipe. Retries briefly while no pipe instance is free."""
    for attempt in range(20):
        buf = b""
        try:
            with open("\\\\.\\pipe\\" + SOCK, "r+b", buffering=0) as f:
                f.write(req)
                while not buf.endswith(b"\n"):
                    c = f.read(1 << 16)
                    if not c:
                        break
                    buf += c
            return buf
        except OSError as e:
            # A pipe server makes a new instance after each client, so for a moment there is none
            # (file not found) or all are taken (231, ERROR_PIPE_BUSY). Retry briefly before deciding
            # the server is gone: 0.3 s for "not found", 1 s for "busy".
            missing = isinstance(e, FileNotFoundError)
            if not (missing or getattr(e, "winerror", None) == 231) or attempt >= (5 if missing else 19):
                raise ServerGone(str(e)) if missing or getattr(e, "winerror", None) == 231 else e
            time.sleep(0.05)
    return b""


def call(method, params=None):
    """One request to herdr's API: newline-delimited JSON over its local socket, which is a Unix socket
    on Linux/macOS and a named pipe (\\\\.\\pipe\\<socket path>) on Windows. A server that is missing or
    does not answer within CALL_TIMEOUT raises ServerGone."""
    req = (json.dumps({"id": "covr", "method": method, "params": params or {}}) + "\n").encode()
    buf = b""
    if WIN:
        # a blocking pipe read has no timeout of its own: do it on a worker thread and stop waiting after
        # CALL_TIMEOUT (a hung herdr then costs one parked thread, not the daemon)
        box = {}

        def work():
            try:
                box["buf"] = _pipe_exchange(req)
            except BaseException as e:
                box["err"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(CALL_TIMEOUT)
        if t.is_alive():
            raise ServerGone(f"{method}: no answer in {CALL_TIMEOUT}s")
        if "err" in box:
            raise box["err"]
        buf = box["buf"]
    else:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(CALL_TIMEOUT)
        try:
            s.connect(SOCK)
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as e:
            s.close()
            raise ServerGone(str(e))
        try:
            s.sendall(req)
            while not buf.endswith(b"\n"):
                c = s.recv(1 << 16)
                if not c:
                    break
                buf += c
        except socket.timeout:
            raise ServerGone(f"{method}: no answer in {CALL_TIMEOUT}s")
        finally:
            s.close()
    r = json.loads(buf)
    if "error" in r:
        raise HerdrError(method, r["error"])
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


REPORT = {"socket": True}


def report(kind, target, set_=None, clear=()):
    """Set and clear one pane's (or workspace's) tokens: over the socket ({pane,workspace}.report_metadata,
    a null value clears), or with the `herdr … report-metadata` CLI when the server does not know that method."""
    seq = next_seq() if SEQ["on"] else None
    if REPORT["socket"]:
        tokens = dict(set_ or {}, **{k: None for k in clear})
        params = {"pane_id" if kind == "pane" else "workspace_id": target, "source": SRC, "tokens": tokens}
        if seq is not None:
            params["seq"] = seq
        try:
            call(f"{kind}.report_metadata", params)
            return
        except HerdrError as e:
            if e.code in ("pane_not_found", "workspace_not_found"):
                return  # it closed between our read and this report: nothing left to set or clear
            if not (e.code == "invalid_request" and "unknown variant" in e.message):
                raise
            REPORT["socket"] = False  # a server without these methods
            log("socket reports unavailable, using the herdr CLI:", e)
    args = [kind, "report-metadata", target, "--source", SRC]
    if seq is not None:
        args += ["--seq", str(seq)]
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
    if re.fullmatch(r"[+-]?[0-9]+", v):
        return int(v), True
    return None, False


def read_flat(path):
    return read_flat_text(read_text(path, "") or "")


def read_flat_text(text):
    """Top-level `key = value` lines of a TOML text, for Pythons without tomllib (< 3.11).
    Stops at the first [table] header, so only top-level keys are read."""
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            break
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        val, ok = parse_value(v)
        if ok:
            out[k.strip().strip('"')] = val
    return out


DURATION = re.compile(r"([0-9]+)\s*([smhd]?)")


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
        if isinstance(value, bool) or not re.fullmatch(r"\s*[+-]?[0-9]+\s*", str(value)):
            return None, f"{key} must be a whole number"  # also refuses inf / nan and non-ASCII digits
        v = int(value)
        return (v, None) if 2 <= v <= 60 else (None, f"{key} must be between 2 and 60")
    return value, None


OPTIONS = os.path.join(CFG_DIR, "config.toml")


def read_options():
    """The option file's top-level keys ({} when there is none). Raises ValueError when it is not valid TOML.
    A UTF-8 byte-order mark (some Windows editors add one) is skipped."""
    text = read_text(OPTIONS)
    if text is None:
        return {}
    text = text.lstrip("\ufeff")
    if tomllib:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"config.toml is not valid TOML ({e})")
    return read_flat_text(text)


def options():
    o = dict(DEFAULTS)
    try:
        raw = read_options()
    except ValueError as e:  # a torn or hand-broken file: keep defaults for this tick
        log_once(("unreadable", str(e)), "options file unreadable, using defaults:", e)
        return o
    if raw.get("show_icon") is False and "kind_icon" not in raw:
        o["kind_icon"] = "off"  # 0.5.0 called it show_icon (true / false)
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
    """Set one option in the option file. Only that key's line changes (or is appended): other options,
    comments and invalid values someone is still fixing stay as they are. A file that does not parse is
    refused (ValueError) instead of being rewritten from defaults."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown option {key!r}")
    read_options()  # raises on a broken file: never overwrite what we cannot read
    o = options()
    if value == "cycle":
        if isinstance(DEFAULTS[key], bool):
            value = not o[key]
        elif key in CYCLES:
            seq = CYCLES[key]
            value = seq[(seq.index(o[key]) + 1) % len(seq)] if o[key] in seq else seq[0]
        else:
            raise ValueError(f"{key} takes a value, it cannot cycle")
    else:
        value, why = validate(key, value)
        if why:
            raise ValueError(why)
    line = f"{key} = {json.dumps(value) if not isinstance(value, bool) else str(value).lower()}"
    text = read_text(OPTIONS, newline="") or ""  # newline="": the file's line endings stay as they are
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    text = text[len(bom):] or "# covr options — edited by actions; the daemon picks changes up live\n"
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    top = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))  # top-level keys end here
    for i in range(top):
        m = re.match(rf"""\s*(["']?){re.escape(key)}\1\s*=\s*(.*?)(\r?\n)?$""", lines[i])
        if m:
            lines[i] = line + value_comment(m.group(2)) + (m.group(3) or "")
            break
    else:
        if top and not lines[top - 1].endswith("\n"):
            lines[top - 1] += nl
        lines.insert(top, line + nl)
    new = bom + "".join(lines)
    if tomllib:
        try:
            if tomllib.loads(new.lstrip("\ufeff")).get(key) != value:
                raise ValueError(f"could not set {key} in config.toml; edit it by hand")
        except tomllib.TOMLDecodeError:
            raise ValueError("the change would make config.toml invalid; edit it by hand")
    private_dir(CFG_DIR)
    write_atomic(OPTIONS, new, newline="")
    return value


def value_comment(v):
    """The trailing `# comment` (with the spaces before it) after a TOML value, or ""."""
    if v[:1] in ('"', "'"):
        end = v.find(v[0], 1)
        rest = v[end + 1:] if end > 0 else ""
    else:
        rest = v[v.find("#"):] if "#" in v else ""
        rest = " " + rest if rest else ""
    m = re.match(r"\s*#.*", rest)
    return re.match(r"\s*", rest).group(0) + rest.strip() if m else ""


def write_atomic(path, text, newline=None):
    """Readers never see a half-written file (the popup, actions and the daemon share these files).
    A symlinked file (dotfile managers) is written through: its target changes, the link stays."""
    path = os.path.realpath(path)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline=newline) as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o777)  # keep an existing file's mode (herdr's config.toml)
    except OSError:
        os.chmod(tmp, 0o600)  # new plugin files: owner-only
    os.replace(tmp, path)


def load_pins():
    """This session's pins. A pre-0.6 shared pins.json is taken over by the default session (its ids are
    session-local, so they are only right there, where most pins were made); named sessions start empty."""
    if not os.path.exists(PINS) and os.path.exists(LEGACY_PINS) and SOCK == DEFAULT_SOCK:
        try:
            private_dir(RUN_DIR)
            os.replace(LEGACY_PINS, PINS)
        except OSError:
            pass
    p = read_json(PINS, {})
    if not isinstance(p, dict):
        p = {}
    return {"agents": list(p.get("agents", [])), "spaces": list(p.get("spaces", []))}


def save_pins(p):
    private_dir(RUN_DIR)
    write_atomic(PINS, json.dumps(p))


def toggle_pin(kind):
    """Pin or unpin the focused agent (kind "agents") or the current space ("spaces").
    Returns (target, on); target is None when nothing is focused, on is None for a pane that is not an agent."""
    if kind not in ("agents", "spaces"):
        raise ValueError("pin takes agents or spaces")
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
    if kind == "agents" and target not in p[kind]:
        try:
            if target not in {a["pane_id"] for a in call("agent.list")["agents"]}:
                return target, None  # a plain shell: a pin would do nothing visible
        except Exception:
            pass  # herdr unreachable: pin it anyway, the daemon sorts it out
    on = target not in p[kind]
    p[kind] = [x for x in p[kind] if x != target] + ([target] if on else [])
    save_pins(p)
    return target, on


PIN_GRACE = 60  # seconds a pinned id may be missing (a restore still in progress) before its pin is dropped


def prune_pins(pins, agent_ids, space_ids, memo, now):
    """Drop pins for agents and spaces that have been gone for PIN_GRACE seconds, from the file too."""
    missing = memo.setdefault("pin_missing", {})
    live = {"agents": agent_ids, "spaces": space_ids}
    changed = False
    for kind in ("agents", "spaces"):
        keep = []
        for x in pins[kind]:
            if x in live[kind]:
                missing.pop(x, None)
                keep.append(x)
            elif now - missing.setdefault(x, now) < PIN_GRACE:
                keep.append(x)
            else:
                missing.pop(x, None)
                changed = True
        pins[kind] = keep
    if changed:
        save_pins(pins)
    return pins


def seconds(s):
    m = DURATION.fullmatch(str(s).strip())
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else 3600


BLANK = "\u2800"  # braille blank: renders empty but is not whitespace, so herdr does not trim it


def cells(s):
    """Terminal cells: wide characters 2; combining marks, format characters (zero-width space, joiners) and
    variation selectors 0; the character a zero-width joiner glues on is drawn inside the one before it."""
    n, glued, last = 0, False, 0
    for c in s:
        if glued:
            glued = False
            continue
        if c == "\u200d":
            glued = True
        elif c == "\ufe0f":
            if last == 1:  # emoji presentation: a narrow symbol (❤, ✔) drawn as a 2-cell emoji
                n, last = n + 1, 2
        elif unicodedata.category(c) in ("Mn", "Me", "Cf") or unicodedata.combining(c):
            continue
        else:
            last = 2 if unicodedata.east_asian_width(c) in "WF" else 1
            n += last
    return n


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


def cut(text, room):
    """`text` shortened to at most `room` cells including the …, at a word boundary when one falls in the
    second half."""
    if cells(text) <= room:
        return text
    t = text
    while t and cells(t) > room - 1:
        t = t[:-1]
    if " " in t[len(t) // 2:] and not text[len(t):len(t) + 1].isspace():
        t = t.rsplit(" ", 1)[0]
    return t.rstrip(" ·") + "…"


def pinned_row(glyph, text, age, width, suffix=""):
    """One composed row: 'glyph text' left, age flush right. Usable cells = width - 1 indent - 1 divider.
    `suffix` (the pin star) is kept whole: the text is shortened to make room for it."""
    usable = min(width - 3, TOKEN_MAX)   # 1 indent + 1 gap + 1 divider; never past herdr's 80-character cap
    right = (" " + age) if age else ""
    room = usable - cells(right) - 2 - cells(suffix)          # glyph + space
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
            text = cut(text, room)
    text += suffix
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
        for p in glob.glob(os.path.join(root, slug, "*.jsonl")):
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass  # removed between the glob and the stat
    files.sort(reverse=True)
    best = None
    for mtime, p in files[:25]:
        if file_contains(p, title):
            best = mtime
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


WAIT_FALLBACK = "waiting for you"


def read_bottom(pane_id, lines=40):
    # "recent" is the bottom of the pane whatever herdr's scroll position ("visible" follows a scrolled-up view)
    try:
        return call("pane.read", {"pane_id": pane_id, "source": "recent", "lines": lines}).get("read", {}).get("text", "")
    except Exception:
        try:
            return subprocess.run([HERDR, "pane", "read", pane_id], capture_output=True, encoding="utf-8", errors="replace", timeout=5, **NOWIN).stdout
        except Exception:
            return ""


def app_scrolled(txt):
    """Claude Code's fullscreen view scrolls inside the app (herdr's offset stays 0): scrolled up, it puts
    `Jump to bottom (ctrl+End) ↓` on its last row, or in the rule above a prompt it only partly hides."""
    return any("Jump to bottom" in l for l in [l for l in txt.splitlines() if l.strip()][-4:])


def wait_reason(pane_id):
    """The question on screen, the fallback when none is found, or None while the app is scrolled away from it."""
    txt = read_bottom(pane_id)
    if app_scrolled(txt):
        return None
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
    return WAIT_FALLBACK


def scrolled_up(pane_id):
    """True while the pane's view is scrolled away from the bottom: herdr's own scrollback (a herdr without
    `scroll` reports none), or an app that scrolls its own fullscreen view."""
    try:
        if (call("pane.get", {"pane_id": pane_id})["pane"].get("scroll") or {}).get("offset_from_bottom", 0) > 0:
            return True
    except Exception:
        pass
    return app_scrolled(read_bottom(pane_id, 8))


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
LOOKUPS_PER_TICK = 3  # transcript lookups (each may read several large files) per tick; the rest wait


def compute(opt, memo):
    memo["wake_at"] = None  # recomputed below; a tick that fails part-way must not leave an old one behind
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
    # a state change first seen after a gap (the daemon was stopped or down) happened at some unknown point in it
    gap = now - (memo.get("tick_at") or now) > max(30, 2 * int(opt["tick_seconds"]) + 5)
    memo["tick_at"] = now
    lookups = LOOKUPS_PER_TICK
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
        if rec and rec.get("st") == "blocked" and st in ("idle", "done", "unknown") and scrolled_up(pid):
            # herdr reads blocked off the screen: scrolled up, the prompt is out of view and the agent looks
            # idle (done, if not selected). Working is real progress and is never held. Hold the row (its age and its reason) until the pane is back at the bottom.
            st, seq, rec["held"] = "blocked", rec["seq"], True
        elif rec and rec.pop("held", False) and st == "blocked":
            # back at the bottom, the same prompt: herdr counts a new change, we don't
            snap = memo.get("waits", {}).get(pid)
            if snap and snap[0] == rec["seq"]:
                snap[0] = seq
            rec["seq"] = seq
        if not rec or rec["seq"] != seq:
            # already in this state when we first saw it, or changed while we were away: the start is unknown ...
            rec = seen[pid] = {"seq": seq, "since": None if rec is None or gap else now, "st": st,
                               "lookup": rec is None or gap}
        elif rec.get("st") not in (None, st):
            # the visible state changed without herdr counting a state change: a finished agent was viewed
            # (done -> idle). The timer restarts, so "idle" and "asleep" count from when you viewed it.
            rec["since"] = now
        rec["st"] = st
        if rec.get("lookup"):
            # ... unless the user lets us look it up (opt-in; Claude Code agents only, a few per tick)
            if opt["age_source"] != "claude-transcripts" or (a.get("agent") or "") != "claude":
                rec.pop("lookup")
            elif lookups > 0:
                lookups -= 1
                rec.pop("lookup")
                rec["since"] = transcript_mtime(a.get("cwd"), a.get("terminal_title_stripped"), tcache) or None
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
    pins = prune_pins(load_pins(), {r["pid"] for r in rows}, set(spaces), memo, now)
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
    # the first and last rows of each group, among the rows the view shows (a filtered view hides some)
    here = next((w for w, ws in spaces.items() if ws.get("focused")), None)
    shown = [r for r in order if opt["view"] == "triage" or r["st"] in ("blocked", "done")
             or (opt["view"] == "here+" and r["wsid"] == here)]
    firsts, lasts = set(), set()
    if group:
        prev = None
        for i, r in enumerate(shown):
            if r["g"] != prev:
                firsts.add(r["pid"])
                if i:
                    lasts.add(shown[i - 1]["pid"])
            prev = r["g"]
    # disambiguation
    dup = {}
    for r in rows:  # agents that would render identically: same space AND same visible glyph
        r["vis"] = "stale" if r["stale"] else r["st"]
        dup.setdefault((r["space"], r["tab"], r["vis"]), []).append(r)

    waits = memo.setdefault("waits", {})
    for pid in list(waits):
        if not any(r["pid"] == pid and r["st"] == "blocked" for r in rows):
            waits.pop(pid)
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
        body = " · ".join(parts)
        icon, where = ICON.get(r["kind"], ICON_OTHER), opt["kind_icon"]
        if where == "inline":
            # [status] [kind] [label]: inside $head, so it takes the row's state colour
            body = f"{icon} {body}"
        elif where in ("left", "right"):
            # its own token, so the layout colours it by brand; herdr joins tokens with an unconfigurable " · ",
            # so it costs 4 cells. Brand colour while the agent is live, idle included (a restart restores every
            # agent idle, and an all-grey sidebar reads as broken); unknown rows grey it (one ZW prefix), asleep
            # rows dim it like the rest (two) — see the layouts' rules
            quiet = 2 if r["stale"] else 1 if r["st"] == "unknown" else 0
            t["icon" if where == "left" else "icon_r"] = ZW * quiet + icon
        t["head"] = pinned_row(glyph, body, fmt_age(r["age"]), width - (4 if where in ("left", "right") else 0),
                               " ★" if r["pinned"] else "")
        if r["pinned"]:
            t["pin"] = "0"
        if mode == "kind" and r["pid"] in lasts and len(kinds) > 1:
            t["rule"] = "─" * max(1, min(width - 3, TOKEN_MAX))
        if opt["show_task"] != "never" and r["st"] == "blocked":
            # read once per prompt, then kept until it is answered (re-read while it is only the fallback)
            snap = waits.get(r["pid"])
            if not snap or snap[0] != r["seq"] or snap[1] == WAIT_FALLBACK:
                reason = wait_reason(r["pid"])  # None: scrolled away, keep what this prompt already has
                if reason is not None or not snap or snap[0] != r["seq"]:
                    snap = waits[r["pid"]] = [r["seq"], reason or WAIT_FALLBACK]
            t["wait"] = clip("↳ " + snap[1])
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
        cwd_of = {}
        try:
            panes = call("pane.list")
            panes = panes.get("panes", panes) if isinstance(panes, dict) else panes
        except Exception:
            panes = agents
        for pn in panes:
            cwd_of.setdefault(pn["workspace_id"], pn.get("cwd"))
        by_cwd = {}
        for w in spaces.values():
            cwd = (w.get("worktree") or {}).get("checkout_path") or cwd_of.get(w["workspace_id"])
            if cwd:
                by_cwd.setdefault(cwd, []).append(w["workspace_id"])
        old, dirty = memo.get("dirty") or {}, {}
        if by_cwd:
            deadline = time.time() + 2  # one budget for all repos: a slow one keeps its last mark instead of stalling the tick
            with ThreadPoolExecutor(max_workers=min(8, len(by_cwd))) as pool:
                for cwd, d in zip(by_cwd, pool.map(lambda c: git_dirty(c, deadline), by_cwd)):
                    for wid in by_cwd[cwd]:
                        if d is None and old.get(wid):
                            dirty[wid] = True  # git too slow or failed this time: keep what we knew
                        elif d:
                            dirty[wid] = True
        memo["dirty"] = dirty
    for wid in memo.get("dirty", {}):
        if wid in spaces:
            wout.setdefault(wid, {})["dirty"] = "±"
    for wid in pins["spaces"]:
        if wid in spaces:
            wout.setdefault(wid, {})["spin"] = "★"
    memo["space_plan"] = plan_spaces(opt, spaces, rows, pins, memo, now)
    memo["space_ids"] = list(spaces)

    view = view_params(opt, mode, len(rows), width)
    return out, wout, view


GIT_FILTER = re.compile(r"^filter\.(.+)\.(clean|smudge|process)$")


def git_dirty(cwd, deadline=None):
    """Does the checkout at `cwd` have uncommitted changes to tracked files? None when git fails or is slow.
    A repo's own config must not run commands here: core.fsmonitor and filter drivers (clean / process, which
    git status runs on a touched file) are switched off, and submodules are not entered. A background
    status must never take index.lock from under the user's git (--no-optional-locks). Files that have a filter
    attribute are left out of the check (without their filter, git would see every touched one as changed).
    Both git calls share one deadline (2 s from the call by default)."""
    deadline = deadline or time.time() + 2
    left = lambda: max(0.05, deadline - time.time())
    try:
        base = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false"]
        names = subprocess.run(base + ["-C", cwd, "config", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
                               capture_output=True, encoding="utf-8", errors="replace", timeout=left(), **NOWIN).stdout
        drivers = sorted({m.group(1) for m in map(GIT_FILTER.match, names.splitlines()) if m})
        if any("=" in d for d in drivers):
            return None  # `-c` splits at the first "=", so such a driver can't be switched off: don't run status
        for name in drivers:
            base += ["-c", f"filter.{name}.clean=", "-c", f"filter.{name}.smudge=", "-c", f"filter.{name}.process=",
                     "-c", f"filter.{name}.required=false"]
        # the whole repo (":/", not just the pane's subdirectory), minus files that use one of those drivers
        # (git refuses some characters in that pathspec; a driver named with them is switched off but not left out)
        spec = ["--", ":/"] + [f":(top,exclude,attr:filter={d})" for d in drivers if re.fullmatch(r"[A-Za-z0-9_ -]+", d)] \
            if drivers else []
        r = subprocess.run(base + ["-C", cwd, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"] + spec,
                           capture_output=True, encoding="utf-8", errors="replace", timeout=left(), **NOWIN)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


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
    pinned = []
    for w in pins["spaces"]:  # a pinned worktree space leads by its parent (children always follow their parent)
        w = parent.get(w, w)
        if w in top and w not in pinned:
            pinned.append(w)
    base = pinned + [w for w in base if w not in pinned]
    want = []
    for w in base:
        want.append(w)
        want += [c for c in cur if parent.get(c) == w]
    want += [w for w in cur if w not in want]
    if sort == "manual" and not pinned and not restoring:
        want = cur                          # never touch the user's order unless asked (pins / other sorts)
    return {"cur": cur, "want": want}


def view_params(opt, mode, count=None, width=None):
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
    if count is not None:
        # every open agent, whatever the view shows. herdr right-aligns the label after its own "agents", so blanks
        # it doesn't trim push the count left to sit beside the word: "agents [12]         triage"
        # herdr draws a label too long for its row over the word "agents", so it must fit width - 9 cells
        # (1 indent + "agents" + 1 space + 1 divider); when it doesn't, the view name is cut, never the count
        n, fit = f"[{count}]", (width or 26) - 9
        room = fit - cells(n) - cells(p["label"])
        if room >= 2:
            p["label"] = n + BLANK * room + p["label"]
        else:
            view = p["label"]
            while view and cells(n) + 3 + cells(view) > fit - (0 if view == p["label"] else 1):
                view = view[:-1]
            view = view.rstrip(" ·")
            p["label"] = n + (" · " + view + ("" if view == p["label"] else "…") if view else "")
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
    """True when a still-live agent shows none of the tokens we pushed to it, or has lost its $head: its
    server restarted (or handed off, or refused our reports) and dropped them, so all of it must be pushed
    again. (Not any token: herdr may drop a single value it sanitises to empty, and that must not turn into
    a full resync every tick. $head never is empty: it always starts with the state glyph. And not only
    "none": a token herdr kept, like $icon_r, must not hide that the row itself is gone.)"""
    live = memo.get("live") or {}
    for pid, t in (memo.get("pushed") or {}).items():
        if t and pid in live and (not set(t) & live[pid] or ("head" in t and "head" not in live[pid])):
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
        for k in ("pushed", "wpushed", "view", "winit"):
            memo.pop(k, None)
    last = memo.setdefault("pushed", {})
    live = memo.get("live") or {}
    for pid, t in out.items():
        if pid not in last:
            # first push from this daemon (a start, reload or resync): tokens an earlier daemon left that the
            # new set lacks (an unpinned pin, an answered wait) would otherwise stay forever
            stray = [k for k in TOKENS if k in live.get(pid, ()) and k not in t]
            report("pane", pid, t, stray)
            last[pid] = t
            continue
        prev = last[pid]
        if prev != t:
            changed = {k: v for k, v in t.items() if prev.get(k) != v}
            gone = [k for k in prev if k not in t]
            report("pane", pid, changed, gone)
            last[pid] = t
    for pid in list(last):
        if pid not in out:
            # no longer an agent (it exited and the pane stayed a shell, or the pane closed): take our tokens
            # back, or a later agent in the same pane would inherit the ones its rows don't set (a stale reason)
            if last[pid]:
                report("pane", pid, {}, list(last[pid]))
            last.pop(pid)
    wl = memo.setdefault("wpushed", {})
    winit = memo.setdefault("winit", set())
    for wid in memo.get("space_ids") or []:
        if wid not in winit:
            # same for spaces: clear what an earlier daemon may have left, once per space
            winit.add(wid)
            t = wout.get(wid, {})
            report("workspace", wid, t, [k for k in WTOKENS if k not in t])
            if t:
                wl[wid] = t
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


def take_lock(path=None, tries=20):
    """Become this session's one daemon, or return None if another process already is.
    Retries briefly so a concurrent alive() probe (a momentary shared lock) cannot make us give up."""
    private_dir(RUN_DIR)
    fd = os.open(path or LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    for i in range(tries):
        if _try_lock(fd, exclusive=True):
            return fd
        if i + 1 < tries:
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
        private_dir(RUN_DIR)
        os.replace(LEGACY_MEMO, MEMO)


def plugin_enabled():
    """False once the user disables or unlinks the plugin (herdr does not stop our daemon for us)."""
    for p in call("plugin.list").get("plugins", []):
        if p.get("plugin_id") == PID:
            return bool(p.get("enabled"))
    return False


def server_unreachable(e):
    """Only the socket itself counts: a missing herdr binary or a vanished file is an ordinary error."""
    return isinstance(e, ServerGone)


def code_stamp():
    """Changes when the plugin's code is updated in place (reinstall, git pull): the daemon then restarts
    itself, since a long-running process would otherwise keep running the old code forever."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return tuple(os.stat(os.path.join(here, f)).st_mtime_ns for f in ("covrd.py",))
    except OSError:
        return None


def stopped():
    """The user stopped the daemon (stop action, or s in the popup): hooks must not start it again.
    Only start (the action or the popup) and a herdr start or restart ([[startup]]) clear it."""
    return os.path.exists(STOPPED)


def set_stopped(on):
    try:
        if on:
            private_dir(RUN_DIR)
            write_atomic(STOPPED, "")
        else:
            os.remove(STOPPED)
    except OSError:
        pass


def run():
    memo = {}
    woke = {"flag": False, "stop": False}
    wakeup = None
    if not WIN:
        # handlers before the lock and the pidfile: a hook that signals the moment the pidfile appears must
        # not kill us with SIGUSR1's default action. The wakeup pipe ends the wait between ticks at once.
        signal.signal(signal.SIGUSR1, lambda *_: woke.update(flag=True))
        signal.signal(signal.SIGUSR2, lambda *_: (memo.update(resync=True), woke.update(flag=True)))
        r, w = os.pipe()
        os.set_blocking(r, False)
        os.set_blocking(w, False)
        signal.set_wakeup_fd(w)
        wakeup = r
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    lock = take_lock()
    if lock is None:
        return  # another daemon already serves this session
    if stopped():
        os.close(lock)
        return  # stopped while we were starting (a hook racing the stop action)
    DAEMON["on"] = True
    try:
        os.chmod(RUN_DIR, 0o700)  # created world-readable by versions before 0.6
    except OSError:
        pass
    stamp = code_stamp()
    reload_code = False
    write_atomic(PIDFILE, str(os.getpid()))
    old = read_json(MEMO, {})
    if isinstance(old, dict):
        memo["since"], memo["tcache"] = old.get("since") or {}, old.get("tcache") or {}
        memo["focus"] = old.get("focus") or {}
        if old.get("manual"):
            memo["manual"] = old["manual"]
        memo["last_sort"] = old.get("last_sort")
        memo["tick_at"] = old.get("tick_at")
        SEQ["last"] = int(old.get("seq") or 0)
    if WIN:
        try:
            os.remove(WAKE)  # requests addressed to an earlier daemon are stale
        except OSError:
            pass
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
                keep = {k: memo.get(k) for k in ("since", "tcache", "focus", "manual", "last_sort", "tick_at")}
                write_atomic(MEMO, json.dumps(dict(keep, seq=SEQ["last"])))
                last_tick = time.time()
                down_since = None
            except Exception as e:
                memo["wake_at"] = None  # never wait on a deadline from before the failure (it would spin)
                if server_unreachable(e):
                    down_since = down_since or time.time()
                    if time.time() - down_since >= GONE_AFTER:
                        log(f"server unreachable for {GONE_AFTER}s: exiting")
                        return
                    memo["resync"] = True  # whatever comes back up has none of our tokens
                else:
                    log("error", repr(e))
                    memo.pop("view", None)
            try:
                tick = int(options()["tick_seconds"])
            except Exception:
                tick = DEFAULTS["tick_seconds"]
            wait(woke, memo, wakeup, time.time() + tick, last_tick)
            if woke["stop"]:
                log("stop requested")
                return
            woke["flag"] = False
    except SystemExit:
        raise
    except BaseException:
        log("crashed:", traceback.format_exc())
        raise
    finally:
        try:
            if int(read_text(PIDFILE, "")) == os.getpid():
                os.remove(PIDFILE)
        except (OSError, ValueError):
            pass
        os.close(lock)  # releases the lock; the lock file itself stays (unlinking it would race a new daemon)
        if reload_code:
            relaunch()


def wait(woke, memo, wakeup, deadline, last_tick):
    """Sleep until the next tick, a hook's wake (bursts coalesce: at most one recompute per MIN_GAP), or the
    moment a selected, finished agent is due to be marked viewed. Unix sleeps in select() on the signal
    wakeup pipe; Windows polls its wake file every 0.1 s."""
    while True:
        if WIN:
            take_wake(woke, memo)
            if woke["stop"]:
                return
        now = time.time()
        if woke["flag"] and now - last_tick >= MIN_GAP:
            return
        due = memo.get("wake_at")
        if (due and now >= due) or now >= deadline:
            return
        if WIN or wakeup is None:
            time.sleep(0.1)
            continue
        t = deadline - now
        if due:
            t = min(t, due - now)
        if woke["flag"]:
            t = min(t, MIN_GAP - (now - last_tick))
        try:
            select.select([wakeup], [], [], max(0.01, t))
            while os.read(wakeup, 512):
                pass
        except (BlockingIOError, InterruptedError):
            pass


def relaunch():
    """Run the updated code: the lock is already released and the new process takes it again. Unix replaces
    this process (same pid, same env). Windows' execv would start an attached console window and mangle paths
    with spaces, so there the daemon starts its successor the way spawn() does and exits."""
    if WIN:
        launch()
        return
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


def launch():
    """Start a daemon process, detached from the caller, its output going to covrd.log."""
    argv = [sys.executable, os.path.abspath(__file__), "run"]
    out = open_log()
    try:
        if WIN:
            # detached, no console, own process group; break away from the hook's job object when herdr allows
            # it, so the daemon outlives the hook that started it
            base = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            # no log handle for its whole life: Windows can't rename a file someone holds open, so covrd.log could
            # never rotate (run() logs a crash's traceback itself)
            for flags in (base | 0x01000000, base):  # | CREATE_BREAKAWAY_FROM_JOB
                try:
                    subprocess.Popen(argv, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, env=os.environ.copy())
                    break
                except OSError:
                    continue
        else:
            subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, env=os.environ.copy())
    finally:
        os.close(out)  # the child keeps its own copy of the handle


def spawn():
    """Start this session's daemon unless it runs; safe to call from many hooks at once (only the hook that
    takes the spawn lock starts one, the others wait for it)."""
    set_stopped(False)
    if alive():
        return alive()
    gate = take_lock(SPAWN_LOCK, tries=1)
    if gate is not None:
        try:
            if not lock_held():
                retire_legacy()
                launch()
            for _ in range(30):
                if alive():
                    break
                time.sleep(0.1)
        finally:
            os.close(gate)
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
    set_stopped(True)  # first: a hook firing now must not start a new daemon
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
            report("workspace", w["workspace_id"], {}, WTOKENS)
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
        set_stopped(False)
        if not signal_daemon("resync"):
            spawn()
        print("running", alive())
    elif cmd == "spawn":
        print("running", spawn())
    elif cmd == "poke":
        if not signal_daemon() and not stopped():
            spawn()
    elif cmd == "stop":
        stop()
        print("stopped")
    elif cmd == "settings":
        sys.exit(open_settings())
    elif cmd == "pin":
        kind = sys.argv[2] if len(sys.argv) > 2 else ""
        if kind not in ("agents", "spaces"):
            sys.exit("usage: covrd.py pin agents|spaces")
        target, on = toggle_pin(kind)
        signal_daemon()
        if not target:
            notify("nothing focused")
        elif on is None:
            notify(f"{target} is not an agent: nothing pinned")
        else:
            notify(f"{'pinned' if on else 'unpinned'} {target}")
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
