# Screenshots

The images in `docs/images/` come from a real herdr, run headless in the e2e sandbox with a made-up session. They never come from anyone's own session.

```sh
tools/screenshots/scene.sh /tmp/shots        # builds the demo session, captures each view (.ans/.txt)
for s in covr-light covr-project herdr-default; do
  tools/screenshots/render.py /tmp/shots/$s.ans /tmp/shots/$s.html --sidebar
done
tools/screenshots/render.py /tmp/shots/covr-dark.ans /tmp/shots/covr-dark.html --sidebar --theme dark
tools/screenshots/render.py /tmp/shots/settings.ans /tmp/shots/settings.html
```

`scene.sh` uses the e2e helpers and the same environment (`HERDR_BIN`, `E2E_ROOT`, `PLUGIN_DIR`). The demo session:
- nine agents across repos, one of them a worktree
- one agent blocked on a prompt, and one that just finished
- a renamed tab, and a repo with uncommitted changes
- ages set in the daemon's memo, so the rows show realistic times

`render.py` turns a tmux capture into an HTML page that uses the terminal's colours. The images are screenshots of that page's `.term` element, taken at 2x with any headless browser.
