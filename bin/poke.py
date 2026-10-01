#!/usr/bin/env python3
"""Fast path for event hooks: wake this session's daemon. Hooks fire on every focus change, so this avoids
importing the whole daemon; when no daemon runs it hands over to `covrd.py poke`, which starts one.

Unix: SIGUSR1 to the pid in the pidfile. Windows (no SIGUSR1): append "wake" to the daemon's wake file.
Keep the paths in step with covrd.py."""
import hashlib, os, subprocess, sys

WIN = os.name == "nt"
PID = "covr.sidebar"


def herdr_dir(kind):
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


sock = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(herdr_dir("config"), "herdr.sock")
state = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(herdr_dir("state"), "plugins", PID)
run = os.path.join(state, "s", hashlib.sha1(sock.encode()).hexdigest()[:12])


def daemon_holds_lock():
    try:
        fd = os.open(os.path.join(run, "covrd.lock"), os.O_RDWR if WIN else os.O_RDONLY)
    except OSError:
        return False
    try:
        if WIN:
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # got it: nobody holds the daemon lock
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


if daemon_holds_lock():
    try:
        if WIN:
            with open(os.path.join(run, "wake"), "a", encoding="utf-8") as f:
                f.write("wake\n")
        else:
            import signal
            with open(os.path.join(run, "covrd.pid")) as f:
                os.kill(int(f.read()), signal.SIGUSR1)
        sys.exit(0)
    except (OSError, ValueError):
        pass
here = os.path.dirname(os.path.abspath(__file__))
sys.exit(subprocess.call([sys.executable, os.path.join(here, "covrd.py"), "poke"]))
