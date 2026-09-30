#!/usr/bin/env bash
# tests/e2e/run.sh [test ...] — lifecycle tests for covr.sidebar against an ISOLATED herdr
# (own HOME / XDG dirs / socket, driven through tmux). Never touches your live herdr.
#
# env: HERDR_BIN   herdr binary (default: `command -v herdr`)
#      PLUGIN_DIR  plugin under test (default: ../../plugin)
#      E2E_ROOT    sandbox root; keep it short, unix sockets must stay < 108 chars (default: /tmp/covr-e2e)
# tests: single tokens restart gone sessions disable popup notoml layout   (default: all)
# Linux only (reads /proc to attribute daemons to the sandbox).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PLUGIN_DIR=${PLUGIN_DIR:-$(cd "$HERE/../../plugin" && pwd)}
HERDR_BIN=${HERDR_BIN:-$(command -v herdr)}
ROOT=${E2E_ROOT:-/tmp/covr-e2e}
B=$ROOT/sb
TM="tmux -L covr-e2e -f /dev/null"
SOCK1=$B/home/.config/herdr/herdr.sock
SOCK2=$B/home/.config/herdr/sessions/two/herdr.sock
PASS=0; FAIL=0; FAILED=()
ok() { echo "PASS $1"; PASS=$((PASS + 1)); }
no() { echo "FAIL $1 — $2"; FAIL=$((FAIL + 1)); FAILED+=("$1"); }

mkdir -p "$ROOT/bin"
ln -sfn "$HERDR_BIN" "$ROOT/bin/herdr"
cp "$HERE/bin/hsock" "$ROOT/bin/hsock"
cat > "$ROOT/sb.sh" <<EOF
#!/usr/bin/env bash
# run a command inside the sandbox environment (optionally against SOCK=<socket>)
exec env -i PATH="$ROOT/bin:/usr/bin:/bin" HOME=$B/home XDG_CONFIG_HOME=$B/home/.config \\
  XDG_STATE_HOME=$B/home/.local/state XDG_DATA_HOME=$B/home/.local/share XDG_CACHE_HOME=$B/home/.cache \\
  XDG_RUNTIME_DIR=$B/run HERDR_DISABLE_SOUND=1 TERM=xterm-256color LANG=C.UTF-8 \${SOCK:+HERDR_SOCKET_PATH=\$SOCK} bash -c "\$*"
EOF
chmod +x "$ROOT/sb.sh"
sb() { "$ROOT/sb.sh" "$@"; }

daemons() { # [socket] -> pids of covrd daemons that belong to this sandbox (and to that socket)
  local p e
  for p in $(pgrep -f 'covrd.py run'); do
    e=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null) || continue
    grep -qx "HOME=$B/home" <<<"$e" || continue
    if [ -n "${1:-}" ] && ! grep -qx "HERDR_SOCKET_PATH=$1" <<<"$e"; then continue; fi
    echo "$p"
  done
}
ndaemons() { daemons "$@" | wc -l | tr -d ' '; }
heads() { # [socket] -> "<agents with a head token> <agents>"
  SOCK=${1:-} sb 'hsock agent.list' | python3 -c '
import json, sys
a = json.load(sys.stdin)["result"]["agents"]
print(sum(1 for x in a if (x.get("tokens") or {}).get("head")), len(a))'
}
all_heads() { local h; h=$(heads "$@"); set -- $h; [ "$2" -gt 0 ] && [ "$1" = "$2" ]; }
no_heads() { local h; h=$(heads "$@"); set -- $h; [ "$1" = 0 ]; }
until_t() { # <seconds> <command...> — poll every 0.5 s
  local end=$((SECONDS + $1)); shift
  while [ $SECONDS -lt $end ]; do "$@" && return 0; sleep 0.5; done
  "$@"
}
zero() { [ "$(ndaemons "$@")" = 0 ]; }

client() { # <tmux window target> [--session name]
  $TM new-window -t t "$ROOT/sb.sh 'cd \$HOME; herdr $*; sleep 900'"
}
up() {
  $TM kill-server 2>/dev/null
  $TM new-session -d -s t -x 140 -y 40 "$ROOT/sb.sh 'cd \$HOME; herdr; sleep 900'"
  until_t 20 sb 'herdr workspace list' >/dev/null 2>&1
  sleep 1; $TM send-keys -t t Escape; sleep 0.3; $TM send-keys -t t Escape
}
down() {
  sb 'herdr server stop' >/dev/null 2>&1
  SOCK=$SOCK2 sb 'herdr server stop' >/dev/null 2>&1
  $TM kill-server 2>/dev/null
  sleep 1
}
agents() { # [socket] — (re)report the scenario's agent states
  SOCK=${1:-} sb '
    set -- $(cat ~/panes)
    r() { herdr pane report-agent "$1" --source e2e --agent claude --state "$2" >/dev/null; }
    r $1 blocked; r $2 working; r $3 idle; r $4 idle; r $5 working'
}
scenario() { # [socket] — 4 spaces, 5 agents
  SOCK=${1:-} sb '
    mkdir -p ~/r/api ~/r/web ~/r/infra ~/r/docs
    j() { sed -n "s/.*\"pane_id\":\"\([^\"]*\)\".*/\1/p" | head -1; }
    a=$(herdr workspace create --cwd ~/r/api --label api --no-focus | j)
    w=$(herdr workspace create --cwd ~/r/web --label web --no-focus | j)
    w2=$(herdr pane split "$w" --direction right --cwd ~/r/web --no-focus | j)
    i=$(herdr workspace create --cwd ~/r/infra --label infra --no-focus | j)
    d=$(herdr workspace create --cwd ~/r/docs --label docs --no-focus | j)
    echo "$a $w $w2 $i $d" > ~/panes'
  agents "${1:-}"
}
fresh() {
  down
  for p in $(daemons); do kill "$p" 2>/dev/null; done
  rm -rf "$B"; mkdir -p "$B/home/.config/herdr" "$B/run"; chmod 700 "$B/run"
  if [ -n "${LAYOUT_CFG:-}" ]; then cp "$LAYOUT_CFG" "$B/home/.config/herdr/config.toml"
  else echo 'onboarding = false' > "$B/home/.config/herdr/config.toml"; fi   # no first-run wizard over the popup
  sb "herdr plugin link '$PLUGIN_DIR'" >/dev/null
  up
  scenario
}
act() { # <action> — invoke and wait for it to finish (herdr runs actions asynchronously)
  local id
  id=$(sb "herdr plugin action invoke covr.sidebar.$1" 2>/dev/null | sed -n 's/.*"log_id":"\([^"]*\)".*/\1/p')
  [ -n "$id" ] || return 1
  until_t 20 act_done "$id"
}
act_done() {
  sb 'herdr plugin log list --plugin covr.sidebar' | python3 -c '
import json, sys
logs = {l["log_id"]: l for l in json.load(sys.stdin)["result"]["logs"]}
sys.exit(0 if logs.get(sys.argv[1], {}).get("status") not in (None, "running") else 1)' "$1"
}
cfg() { echo "$B/home/.config/herdr/config.toml"; }

# ---------------------------------------------------------------- tests
t_single() {  # G3: a burst of concurrent spawns leaves exactly one daemon per session
  fresh
  until_t 15 all_heads >/dev/null
  for p in $(daemons); do kill "$p"; done; until_t 5 zero
  # herdr serialises action invocations, but event hooks run concurrently: race raw spawns like they do
  SOCK=$SOCK1 sb "cd '$PLUGIN_DIR'; for i in \$(seq 12); do python3 bin/covrd.py spawn >/dev/null & done; wait"
  sleep 3
  local n; n=$(ndaemons)
  [ "$n" = 1 ] && ok single || no single "$n daemons for one session"
}
t_tokens() {  # baseline: every agent row gets its head token
  fresh
  until_t 15 all_heads && ok tokens || no tokens "heads $(heads)"
}
t_restart() { # G1: after a server restart the daemon re-pushes everything, not just changed rows
  fresh
  until_t 15 all_heads || { no restart "no heads before restart ($(heads))"; return; }
  sb 'herdr server stop' >/dev/null 2>&1; sleep 2
  up
  agents                       # same states as before the restart
  until_t 20 all_heads && ok restart || no restart "heads after restart $(heads)"
}
t_gone() {    # G2: server gone for good -> daemon exits by itself
  fresh
  until_t 15 all_heads >/dev/null
  down
  until_t 120 zero && ok gone || no gone "$(ndaemons) daemon(s) still alive 120 s after server stop"
}
t_sessions() { # G3: a second named session gets its own daemon, tokens (and view)
  fresh
  client --session two
  until_t 20 env SOCK=$SOCK2 "$ROOT/sb.sh" 'herdr workspace list' >/dev/null 2>&1
  scenario "$SOCK2"
  until_t 20 all_heads "$SOCK2"
  # spawners that lose the lock race exit within ~1 s; count once things settle
  until_t 10 eval '[ "$(ndaemons "$SOCK1")" = 1 ] && [ "$(ndaemons "$SOCK2")" = 1 ]'
  local a b; a=$(ndaemons "$SOCK1"); b=$(ndaemons "$SOCK2")
  if [ "$a" = 1 ] && [ "$b" = 1 ] && all_heads "$SOCK1" && all_heads "$SOCK2"; then ok sessions
  else no sessions "daemons default=$a two=$b heads default=($(heads "$SOCK1")) two=($(heads "$SOCK2"))"; fi
}
t_disable() { # G4: disable -> daemon clears its tokens and exits; enable + any event -> back
  fresh
  until_t 15 all_heads >/dev/null
  sb 'herdr plugin disable covr.sidebar' >/dev/null
  if until_t 60 zero && no_heads; then
    sb 'herdr plugin enable covr.sidebar' >/dev/null
    sb 'set -- $(cat ~/panes); herdr pane report-agent $3 --source e2e --agent claude --state working' >/dev/null
    until_t 20 all_heads && ok disable || no disable "not back after enable ($(heads), $(ndaemons) daemons)"
  else no disable "after disable: $(ndaemons) daemon(s), heads $(heads)"; fi
}
t_popup() {   # G5: the settings popup is plugin-owned and drives this session's daemon
  fresh
  until_t 15 all_heads >/dev/null
  act settings; sleep 1
  if ! $TM capture-pane -t t:0 -p | grep -q 'covr — settings'; then no popup "popup did not open"; return; fi
  $TM send-keys -t t:0 s; until_t 10 zero || { no popup "s did not stop the daemon"; return; }
  $TM send-keys -t t:0 s; sleep 2
  local n; n=$(ndaemons "$SOCK1")
  $TM send-keys -t t:0 q; sleep 1
  if [ "$n" = 1 ] && ! $TM capture-pane -t t:0 -p | grep -q 'covr — settings'; then ok popup
  else no popup "restart from popup: $n daemon(s) on the session socket"; fi
}
t_notoml() {  # G6: options are read identically without tomllib
  mkdir -p "$B/home/.config/herdr/plugins/config/covr.sidebar"
  local f="$B/home/.config/herdr/plugins/config/covr.sidebar/config.toml"
  cat > "$f" <<'EOF'
# hand-edited
group_by = "project"   # trailing comment
space_sort = 'alpha'
disambiguate = false
tick_seconds = 7
stale_after = "30m"
view = "needs me"
EOF
  local py='import json, sys; sys.path.insert(0, sys.argv[1] + "/bin"); import covrd; print(json.dumps(covrd.options(), sort_keys=True))'
  local with without
  with=$(HERDR_PLUGIN_CONFIG_DIR=${f%/*} python3 -c "$py" "$PLUGIN_DIR")
  without=$(HERDR_PLUGIN_CONFIG_DIR=${f%/*} python3 -c "import sys; sys.modules['tomllib'] = None; $py" "$PLUGIN_DIR")
  if [ -n "$with" ] && [ "$with" = "$without" ] && grep -q '"tick_seconds": 7' <<<"$with"; then ok notoml
  else no notoml "with=$with without=$without"; fi
}
t_layout() {  # G7: install is idempotent, uninstall restores the original bytes, conflicts are refused
  local orig="$ROOT/orig.toml"
  cat > "$orig" <<'EOF'
onboarding = false

[theme]
name = "catppuccin-mocha"

[ui]
sidebar_width = 30

# >>> covr.sidebar keys
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "covr.sidebar.cycle-view"
# <<< covr.sidebar keys
EOF
  LAYOUT_CFG=$orig fresh
  act install-layout; local one; one=$(cat "$(cfg)")
  act install-layout; local two; two=$(cat "$(cfg)")
  local why=""
  grep -q '^# >>> covr.sidebar (covr)' "$(cfg)" || why+="no block; "
  grep -qi '#f38ba8' "$(cfg)" || why+="not the mocha variant; "
  [ "$one" = "$two" ] || why+="second install changed the file; "
  python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$(cfg)" 2>/dev/null || why+="result is not valid TOML; "
  grep -q 'command = "covr.sidebar.cycle-view"' "$(cfg)" || why+="keys block lost; "
  act uninstall-layout
  cmp -s "$orig" "$(cfg)" || why+="uninstall did not restore the original bytes; "
  printf '\n[ui.sidebar.agents]\nrow_gap = 1\n' >> "$(cfg)"; cp "$(cfg)" "$ROOT/conflict.toml"
  act install-layout
  cmp -s "$ROOT/conflict.toml" "$(cfg)" || why+="install wrote over a conflicting [ui.sidebar.agents]; "
  [ -z "$why" ] && ok layout || no layout "$why"
}

ALL="single tokens restart gone sessions disable popup notoml layout"
for t in ${*:-$ALL}; do "t_$t"; done
[ -n "${KEEP:-}" ] || down
[ -n "${KEEP:-}" ] || for p in $(daemons); do kill "$p" 2>/dev/null; done
echo "---- $PASS passed, $FAIL failed ${FAILED[*]:-}"
[ "$FAIL" = 0 ]
