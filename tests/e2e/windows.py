#!/usr/bin/env python3
"""tests/e2e/windows.py [test ...] — covr's lifecycle tests on native Windows, against an ISOLATED headless
herdr (its own USERPROFILE / APPDATA / LOCALAPPDATA; never your own herdr).

Run it with a Windows Python (3.8+) on Windows. The bash suite (run.sh) covers Linux and macOS; this one
covers what differs on Windows: the named-pipe transport, msvcrt locking, the wake file instead of
signals, detached daemons and Windows paths. The settings popup needs a terminal client, so it is not
covered here.

env: HERDR_BIN   herdr.exe (default: herdr on PATH)
     PLUGIN_DIR  plugin under test (default: the repo root)
     E2E_ROOT    sandbox root (default: %TEMP%\\covr-e2e)
tests: tokens single events restart gone disable layout validate reload gitsafe   (default: all)
"""
import json, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.abspath(os.environ.get("PLUGIN_DIR") or os.path.join(HERE, "..", ".."))
HERDR_BIN = os.environ.get("HERDR_BIN") or shutil.which("herdr") or "herdr"
ROOT = os.path.abspath(os.environ.get("E2E_ROOT") or os.path.join(os.environ.get("TEMP", "."), "covr-e2e"))
B = os.path.join(ROOT, "sb")
HOME = os.path.join(B, "home")
APPDATA, LOCALAPPDATA = os.path.join(HOME, "AppData", "Roaming"), os.path.join(HOME, "AppData", "Local")
CONFIG = os.path.join(APPDATA, "herdr")
SOCK = os.path.join(CONFIG, "herdr.sock")
NOWIN = 0x08000000  # CREATE_NO_WINDOW
RESULTS = []


def env():
    e = dict(os.environ, USERPROFILE=HOME, HOME=HOME, APPDATA=APPDATA, LOCALAPPDATA=LOCALAPPDATA, HERDR_DISABLE_SOUND="1")
    e["PATH"] = os.path.join(ROOT, "bin") + os.pathsep + e.get("PATH", "")
    for k in [k for k in e if k.startswith("HERDR_") and k != "HERDR_DISABLE_SOUND"]:
        del e[k]
    return e


def herdr(*args, check=False, timeout=30):
    r = subprocess.run([HERDR_BIN, *args], env=env(), capture_output=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NOWIN)
    if check and r.returncode:
        raise RuntimeError(f"herdr {' '.join(args)}: {r.stderr or r.stdout}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"text": r.stdout, "rc": r.returncode}


def call(method, params=None):
    with open("\\\\.\\pipe\\" + SOCK, "r+b", buffering=0) as f:
        f.write((json.dumps({"id": "t", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            c = f.read(1 << 16)
            if not c:
                break
            buf += c
    return json.loads(buf)


def until(seconds, fn):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.5)
    try:
        return bool(fn())
    except Exception:
        return False


def ok(name):
    print(f"PASS {name}", flush=True)
    RESULTS.append((name, True))


def no(name, why):
    print(f"FAIL {name} — {why}", flush=True)
    RESULTS.append((name, False))


# ---------------------------------------------------------------- sandbox
def daemons():
    """pids of covrd daemons running this plugin copy (Windows has no /proc environ: match the command line)."""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*covrd.py*run*' } | "
          "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, encoding="utf-8",
                       errors="replace", creationflags=NOWIN)
    want = os.path.join(PLUGIN_DIR, "bin", "covrd.py").lower()
    return [int(line.split("\t")[0]) for line in r.stdout.splitlines()
            if "\t" in line and want in line.lower() and line.rstrip().endswith(" run")]


def kill_daemons():
    for p in daemons():
        subprocess.run(["taskkill", "/F", "/PID", str(p)], capture_output=True, creationflags=NOWIN)
    until(10, lambda: not daemons())


def server_up():
    return "result" in call("ping")


def start_server():
    subprocess.Popen([HERDR_BIN, "server"], env=env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=NOWIN | 0x00000200)  # | CREATE_NEW_PROCESS_GROUP
    if not until(30, server_up):
        raise RuntimeError("herdr server did not start")


def stop_server():
    herdr("server", "stop")
    until(15, lambda: not os.path.exists(SOCK) or not _pings())


def _pings():
    try:
        return server_up()
    except OSError:
        return False


def heads():
    a = call("agent.list")["result"]["agents"]
    return sum(1 for x in a if (x.get("tokens") or {}).get("head")), len(a)


def all_heads():
    n, total = heads()
    return total > 0 and n == total


PANES = []


def report_agents():
    for p, state in zip(PANES, ("blocked", "working", "idle", "idle", "working")):
        herdr("pane", "report-agent", p, "--source", "e2e", "--agent", "claude", "--state", state)


def scenario():
    del PANES[:]
    for name in ("api", "web", "infra", "docs"):
        d = os.path.join(HOME, "r", name)
        os.makedirs(d, exist_ok=True)
        r = herdr("workspace", "create", "--cwd", d, "--label", name, "--no-focus", check=True)
        PANES.append(r["result"]["root_pane"]["pane_id"] if "root_pane" in r.get("result", {}) else
                     _first_pane(r))
    web2 = herdr("pane", "split", PANES[1], "--direction", "right", "--cwd", os.path.join(HOME, "r", "web"), "--no-focus")
    PANES.insert(2, _first_pane(web2))
    report_agents()


def _first_pane(r):
    text = json.dumps(r)
    i = text.find('"pane_id": "')
    if i < 0:
        raise RuntimeError(f"no pane id in {text[:300]}")
    return text[i + 12:text.index('"', i + 12)]


def fresh(config_text="onboarding = false\n"):
    try:
        stop_server()
    except Exception:
        pass
    kill_daemons()
    shutil.rmtree(B, ignore_errors=True)
    os.makedirs(CONFIG, exist_ok=True)
    os.makedirs(LOCALAPPDATA, exist_ok=True)
    with open(os.path.join(CONFIG, "config.toml"), "w", encoding="utf-8") as f:
        f.write(config_text)
    # plugin commands run `python3`: give this Python that name on the sandbox PATH (herdr resolves .cmd shims)
    os.makedirs(os.path.join(ROOT, "bin"), exist_ok=True)
    with open(os.path.join(ROOT, "bin", "python3.cmd"), "w", encoding="utf-8") as f:
        f.write(f'@"{sys.executable}" %*\r\n')
    start_server()
    r = herdr("plugin", "link", PLUGIN_DIR)
    if "error" in r:
        raise RuntimeError(f"plugin link: {r}")
    scenario()


def act(action):
    r = herdr("plugin", "action", "invoke", f"covr.sidebar.{action}")
    log_id = json.dumps(r).split('"log_id": "')[1].split('"')[0] if '"log_id"' in json.dumps(r) else None

    def done():
        logs = {l["log_id"]: l for l in herdr("plugin", "log", "list", "--plugin", "covr.sidebar")["result"]["logs"]}
        return logs.get(log_id, {}).get("status") not in (None, "running")
    return until(30, done) if log_id else False


def run_dir():
    for top, _, files in os.walk(LOCALAPPDATA):
        if "covrd.lock" in files:
            return top
    for top, _, files in os.walk(HOME):
        if "covrd.lock" in files:
            return top
    return None


def plugin_env():
    """The environment herdr gives plugin commands, reconstructed (for racing raw spawns like hooks do)."""
    rd = run_dir()
    state = os.path.dirname(os.path.dirname(rd))
    cfg = herdr("plugin", "config-dir", "covr.sidebar").get("text", "").strip() or \
        os.path.join(CONFIG, "plugins", "config", "covr.sidebar")
    return dict(env(), HERDR_SOCKET_PATH=SOCK, HERDR_BIN_PATH=HERDR_BIN, HERDR_PLUGIN_STATE_DIR=state,
                HERDR_PLUGIN_CONFIG_DIR=cfg, HERDR_PLUGIN_ID="covr.sidebar")


def log_text():
    rd = run_dir()
    try:
        with open(os.path.join(rd, "covrd.log"), encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, TypeError):
        return ""


# ---------------------------------------------------------------- tests
def t_tokens():
    fresh()
    if until(20, all_heads):
        ok("tokens")
    else:
        no("tokens", f"heads {heads()}; daemons {daemons()}; log: {log_text()[-300:]}")


def t_single():
    fresh()
    until(20, all_heads)
    e = plugin_env()
    kill_daemons()
    procs = [subprocess.Popen([sys.executable, os.path.join(PLUGIN_DIR, "bin", "covrd.py"), "spawn"], env=e,
                              cwd=PLUGIN_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=NOWIN) for _ in range(12)]
    for p in procs:
        p.wait(60)
    until(15, lambda: len(daemons()) == 1)
    n = len(daemons())
    ok("single") if n == 1 else no("single", f"{n} daemons for one session")


def t_events():
    fresh()
    until(20, all_heads)
    subprocess.run([sys.executable, os.path.join(PLUGIN_DIR, "bin", "covrd.py"), "set", "tick_seconds", "60"],
                   env=plugin_env(), capture_output=True, creationflags=NOWIN)
    time.sleep(6)
    p = PANES[0]
    tab = [a["tab_id"] for a in call("agent.list")["result"]["agents"] if a["pane_id"] == p][0]
    herdr("tab", "rename", tab, "renamedtab")

    def renamed():
        return any("renamedtab" in (a.get("tokens") or {}).get("head", "")
                   for a in call("agent.list")["result"]["agents"] if a["pane_id"] == p)
    ok("events") if until(4, renamed) else no("events", "the rename did not reach the row through a hook")


def t_restart():
    fresh()
    if not until(20, all_heads):
        return no("restart", f"no heads before the restart {heads()}")
    stop_server()
    start_server()
    report_agents()
    ok("restart") if until(30, all_heads) else no("restart", f"heads after restart {heads()}; log: {log_text()[-300:]}")


def t_gone():
    fresh()
    until(20, all_heads)
    stop_server()
    ok("gone") if until(120, lambda: not daemons()) else no("gone", f"{daemons()} still alive 120 s after server stop")


def t_disable():
    fresh()
    until(20, all_heads)
    herdr("plugin", "disable", "covr.sidebar")
    if not (until(60, lambda: not daemons()) and until(10, lambda: heads()[0] == 0)):
        return no("disable", f"after disable: daemons {daemons()}, heads {heads()}")
    herdr("plugin", "enable", "covr.sidebar")
    herdr("pane", "report-agent", PANES[3], "--source", "e2e", "--agent", "claude", "--state", "working")
    ok("disable") if until(30, all_heads) else no("disable", f"not back after enable: {heads()}, daemons {daemons()}")


def t_layout():
    orig = ('onboarding = false\r\n\r\n[theme]\r\nname = "catppuccin-mocha"\r\n\r\n# >>> covr.sidebar keys\r\n'
            '[[keys.command]]\r\nkey = "prefix+a"\r\ntype = "plugin_action"\r\ncommand = "covr.sidebar.cycle-view"\r\n'
            '# <<< covr.sidebar keys\r\n')  # CRLF, as Windows editors write it
    fresh(orig.replace("\r\n", "\n"))
    cfg = os.path.join(CONFIG, "config.toml")
    with open(cfg, "wb") as f:
        f.write(orig.encode())
    why = []
    act("install-layout")
    one = open(cfg, "rb").read()
    act("install-layout")
    two = open(cfg, "rb").read()
    if b"# >>> covr.sidebar layout" not in one:
        why.append("no block")
    if b"#f38ba8" not in one:
        why.append("not the mocha variant")
    if one != two:
        why.append("second install changed the file")
    act("uninstall-layout")
    if open(cfg, "rb").read() != orig.encode():
        why.append("uninstall did not restore the original bytes")
    with open(cfg, "ab") as f:
        f.write(b"\r\n[ui.sidebar.agents]\r\nrow_gap = 1\r\n")
    before = open(cfg, "rb").read()
    act("install-layout")
    if open(cfg, "rb").read() != before:
        why.append("wrote over a conflicting [ui.sidebar.agents]")
    ok("layout") if not why else no("layout", "; ".join(why))


def t_validate():
    fresh()
    until(20, all_heads)
    e, why = plugin_env(), []
    covrd = os.path.join(PLUGIN_DIR, "bin", "covrd.py")
    for k, v in (("tick_seconds", "abc"), ("view", "bogus")):
        if subprocess.run([sys.executable, covrd, "set", k, v], env=e, capture_output=True, creationflags=NOWIN).returncode == 0:
            why.append(f"set accepted {k}={v}")
    opts = os.path.join(e["HERDR_PLUGIN_CONFIG_DIR"], "config.toml")
    os.makedirs(os.path.dirname(opts), exist_ok=True)
    with open(opts, "w", encoding="utf-8") as f:
        f.write('view = "bogus"\ntick_seconds = "x"\nstale_after = "soon"\n')
    herdr("pane", "report-agent", PANES[3], "--source", "e2e", "--agent", "claude", "--state", "working")
    time.sleep(8)
    if not all_heads():
        why.append(f"rows lost after a bad hand edit {heads()}")
    if len(daemons()) != 1:
        why.append("daemon died")
    ok("validate") if not why else no("validate", "; ".join(why))


def t_reload():
    fresh()
    until(20, all_heads)
    os.utime(os.path.join(PLUGIN_DIR, "bin", "covrd.py"))
    until(20, lambda: "plugin code updated" in log_text())
    time.sleep(4)
    if "plugin code updated" in log_text() and len(daemons()) == 1 and log_text().count(" started ") >= 2 and all_heads():
        ok("reload")
    else:
        no("reload", f"daemons {daemons()}, heads {heads()}")


def t_gitsafe():
    git = shutil.which("git")
    if not git:
        return print("SKIP gitsafe — no git on PATH", flush=True)
    fresh()
    until(20, all_heads)
    d = os.path.join(HOME, "r", "hostile")
    marker = os.path.join(HOME, "PWNED")
    os.makedirs(d, exist_ok=True)
    g = lambda *a: subprocess.run([git, "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=d, capture_output=True, creationflags=NOWIN)
    g("init", "-q"); g("commit", "-q", "--allow-empty", "-m", "i")
    with open(os.path.join(d, "f"), "w") as f:
        f.write("x\n")
    g("add", "f"); g("commit", "-qm", "f")
    with open(os.path.join(d, "f"), "a") as f:
        f.write("y\n")
    g("config", "core.fsmonitor", f'cmd /c type nul > "{marker}" & rem')
    herdr("workspace", "create", "--cwd", d, "--label", "hostile", "--no-focus")
    kill_daemons()
    act("start")

    def dirty():
        return any((w.get("tokens") or {}).get("dirty") == "±" for w in call("workspace.list")["result"]["workspaces"]
                   if w["label"] == "hostile")
    got = until(40, dirty)
    if os.path.exists(marker):
        no("gitsafe", "the repo's core.fsmonitor ran")
    elif not got:
        no("gitsafe", "dirty marker missing")
    else:
        ok("gitsafe")


ALL = "tokens single events restart gone disable layout validate reload gitsafe".split()

if __name__ == "__main__":
    for name in sys.argv[1:] or ALL:
        try:
            globals()["t_" + name]()
        except Exception as e:
            no(name, f"harness error: {e!r}")
    try:
        stop_server()
    except Exception:
        pass
    kill_daemons()
    failed = [n for n, good in RESULTS if not good]
    print(f"---- {len(RESULTS) - len(failed)} passed, {len(failed)} failed {' '.join(failed)}", flush=True)
    sys.exit(1 if failed else 0)
