#!/usr/bin/env python3
"""Fast path for event hooks: wake this session's daemon (SIGUSR1). Hooks fire on every focus change,
so this avoids importing the whole daemon; when no daemon runs it hands over to `covrd.py poke`."""
import fcntl, hashlib, os, signal, sys

sock = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
state = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.local/state/herdr/plugins/covr.sidebar")
run = os.path.join(state, "s", hashlib.sha1(sock.encode()).hexdigest()[:12])
try:
    fd = os.open(os.path.join(run, "covrd.lock"), os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # got it: nobody holds the daemon lock
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        with open(os.path.join(run, "covrd.pid")) as f:
            os.kill(int(f.read()), signal.SIGUSR1)
        sys.exit(0)
    finally:
        os.close(fd)
except (OSError, ValueError):
    pass
here = os.path.dirname(os.path.abspath(__file__))
os.execv(sys.executable, [sys.executable, os.path.join(here, "covrd.py"), "poke"])
