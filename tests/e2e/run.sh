#!/usr/bin/env bash
# tests/e2e/run.sh [test ...] — lifecycle tests for covr.sidebar against an ISOLATED herdr
# (own HOME / XDG dirs / socket, driven through tmux). Never touches your live herdr.
#
# env: HERDR_BIN   herdr binary (default: `command -v herdr`)
#      PLUGIN_DIR  plugin under test (default: the repo root)
#      E2E_ROOT    sandbox root; keep it short, unix sockets must stay < 108 chars (default: /tmp/covr-e2e)
#      PYTHON      interpreter the sandboxed herdr runs the plugin with, e.g. a python3.8 (default: python3 on PATH)
# tests: single tokens restart gone sessions disable popup notoml layout rows
#        events width seq cap validate hygiene fight reload gitsafe viewed stopstays scrollhold appscroll   (default: all)
# Linux and macOS (daemons are attributed to the sandbox by their environment: /proc on Linux, ps -E on macOS).
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PLUGIN_DIR=${PLUGIN_DIR:-$(cd "$HERE/../.." && pwd)}
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
if [ -n "${PYTHON:-}" ]; then ln -sfn "$PYTHON" "$ROOT/bin/python3"; else rm -f "$ROOT/bin/python3"; fi
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
    if [ -r /proc/$p/environ ]; then e=$(tr '\0' '\n' < /proc/$p/environ 2>/dev/null) || continue
    else e=$(ps -wwE -o command= -p "$p" 2>/dev/null | tr ' ' '\n') || continue; fi
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
  # spawners that lose the race exit within ~1 s of starting (slow CI runners start them late): a second
  # daemon that really holds the lock would never go away, so wait for things to settle, then count
  until_t 10 eval '[ "$(ndaemons)" = 1 ]'
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
  act settings
  popup_open() { $TM capture-pane -t t:0 -p | grep -q 'covr — settings'; }
  until_t 10 popup_open || { no popup "popup did not open"; return; }
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
  grep -q '^# >>> covr.sidebar layout' "$(cfg)" || why+="no block; "
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

t_rows() {    # G7b: your rows from rows.toml join the layout, herdr accepts it, and their tokens render
  if ! sb 'python3 -c "import tomllib"' 2>/dev/null; then   # rows.toml needs 3.11+ (macOS ships 3.9)
    echo "SKIP rows — the plugin's python3 has no tomllib"; return
  fi
  fresh
  local cdir="$B/home/.config/herdr/plugins/config/covr.sidebar"
  mkdir -p "$cdir"
  cat > "$cdir/rows.toml" <<'EOF'
# a bar under each agent, fed by another plugin
agents = [
  [{ token = "$bar", fg = "#4c4f69", rules = [{ starts_with = "\u200b", fg = "#df8e1d" }] }, { token = "$track", fg = "#9ca0b0" }],
]
agent_line = { tokens = [{ token = "$flag", fg = "#d20f39" }], width = 1 }
EOF
  act install-layout
  local why="" p
  grep -q 'token = "$bar"' "$(cfg)" || why+="row not installed; "
  python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$(cfg)" 2>/dev/null || why+="result is not valid TOML; "
  p=$(sb 'herdr agent list' | sed -n 's/.*"pane_id":"\([^"]*\)".*/\1/p' | head -1)
  sb "herdr pane report-metadata $p --source e2e --token 'bar=━━━━━━' --token 'track=· · ·'" >/dev/null
  until_t 10 eval '$TM capture-pane -t t:0 -p | grep -q "━━━━━━ · · · ·"' || why+="the row's tokens are not drawn; "
  grep -q 'token = "$flag"' "$(cfg)" || why+="agent_line token not installed; "
  sb "herdr pane report-metadata $p --source e2e --token 'flag=!'" >/dev/null   # covr left it room: drawn whole
  until_t 10 eval '$TM capture-pane -t t:0 -p | grep -qE " · !( |│|$)"' || why+="the agent_line token is not drawn at the end of the line; "
  act install-layout
  grep -q 'token = "$bar"' "$(cfg)" || why+="a reinstall dropped the row; "
  act uninstall-layout
  grep -q 'token = "$bar"' "$(cfg)" && why+="uninstall left the row; "
  [ -z "$why" ] && ok rows || no rows "$why"
}

# ---------------------------------------------------------------- hardening (0.3)
run_dir() { ls -d "$B"/home/.local/state/herdr/plugins/covr.sidebar/s/*/ 2>/dev/null | head -1; }
opt() { # <key> <value> — through the plugin's own CLI, like the actions do
  SOCK=$SOCK1 sb "cd '$PLUGIN_DIR' && python3 bin/covrd.py set $1 '$2'" >/dev/null 2>&1; }
head_of() { # <pane> -> its $head token
  sb 'hsock agent.list' | python3 -c 'import json, sys
print([(a.get("tokens") or {}).get("head", "") for a in json.load(sys.stdin)["result"]["agents"] if a["pane_id"] == sys.argv[1]][0])' "$1"; }
t_events() {  # H2: a tab rename shows up through the hook, long before the next tick
  fresh
  until_t 15 all_heads >/dev/null
  opt tick_seconds 60; sleep 6                  # ticks are now a minute apart: only a hook can be this fast
  local p tab; p=$(cut -d' ' -f1 < "$B/home/panes")
  tab=$(sb 'hsock agent.list' | python3 -c 'import json, sys
print([a["tab_id"] for a in json.load(sys.stdin)["result"]["agents"] if a["pane_id"] == sys.argv[1]][0])' "$p")
  sb "herdr tab rename $tab renamedtab" >/dev/null
  until_t 3 eval 'head_of "$p" | grep -q renamedtab' && ok events || no events "head after rename: $(head_of "$p")"
}
t_width() {   # H3: each session reads ITS OWN dragged width (fnv1a of its client socket)
  local py='import sys; sys.path.insert(0, sys.argv[1] + "/bin"); import covrd; print(covrd.sidebar_width())'
  local cs="$B/home/.local/state/herdr/client-shell"; mkdir -p "$cs"
  for pair in "$SOCK1:41" "$SOCK2:55"; do
    local sock=${pair%:*} w=${pair##*:}
    local h; h=$(python3 -c 'import sys
h = 0xcbf29ce484222325
for b in sys.argv[1].encode(): h = ((h ^ b) * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
print(format(h, "016x"))' "${sock%/*}/herdr-client.sock")
    echo "{\"sidebar_width\": $w}" > "$cs/local-$h.json"
  done
  echo '{"sidebar_width": 999}' > "$cs/local-0000000000000000.json"   # someone else's, newest: must be ignored
  local a b; a=$(SOCK=$SOCK1 sb "python3 -c '$py' '$PLUGIN_DIR'"); b=$(SOCK=$SOCK2 sb "python3 -c '$py' '$PLUGIN_DIR'")
  [ "$a" = 41 ] && [ "$b" = 55 ] && ok width || no width "default=$a two=$b (want 41 / 55)"
}
t_seq() {     # H4: even if herdr rejects our --seq (its last accepted is ahead of ours), rows come back
  fresh
  until_t 15 all_heads >/dev/null
  local far=99999999999999
  sb "for p in \$(cat ~/panes); do herdr pane report-metadata \$p --source covr --seq $far \
      --clear-token head --clear-token rank --clear-token age --clear-token pin --clear-token grp >/dev/null; done"
  until_t 40 all_heads && ok seq || no seq "rows still empty ($(heads)); log: $(tail -3 "$(run_dir)covrd.log" 2>/dev/null)"
}
t_cap() {     # H5: on a very wide sidebar the right-pinned age still survives herdr's 80-character cap
  fresh
  printf 'onboarding = false\n[ui]\nsidebar_width = 150\n' > "$(cfg)"
  until_t 15 all_heads >/dev/null                       # the daemon must see the agent before it changes state
  local p; p=$(cut -d' ' -f4 < "$B/home/panes")          # infra: idle -> working gives it a fresh age
  sb "herdr pane report-agent $p --source e2e --agent claude --state working" >/dev/null
  until_t 15 eval 'head_of "$p" | grep -q "m$"'
  local h n; h=$(head_of "$p"); n=${#h}
  [[ "$h" == *"<1m" ]] && [ "$n" -le 80 ] && ok cap || no cap "head is $n chars and ends with '${h: -6}' (age cut off?)"
}
t_validate() { # H7: bad values are refused by `set`, and ignored (with defaults) when hand-edited
  fresh
  until_t 15 all_heads >/dev/null
  local f="$B/home/.config/herdr/plugins/config/covr.sidebar/config.toml" why=""
  opt view triage
  local before; before=$(cat "$f")
  SOCK=$SOCK1 sb "cd '$PLUGIN_DIR' && python3 bin/covrd.py set tick_seconds abc" >/dev/null 2>&1 && why+="set accepted tick_seconds=abc; "
  SOCK=$SOCK1 sb "cd '$PLUGIN_DIR' && python3 bin/covrd.py set view bogus" >/dev/null 2>&1 && why+="set accepted view=bogus; "
  [ "$before" = "$(cat "$f")" ] || why+="options file changed; "
  printf 'view = "bogus"\ntick_seconds = "x"\nstale_after = "soon"\ngroup_by = 7\n' > "$f"
  sb "set -- \$(cat ~/panes); herdr pane report-agent \$3 --source e2e --agent claude --state working" >/dev/null
  sleep 8
  all_heads || why+="rows lost after bad hand edit ($(heads)); "
  [ "$(ndaemons)" = 1 ] || why+="daemon died; "
  local n; n=$(grep -c "ignored" "$(run_dir)covrd.log")
  [ "$n" -ge 1 ] && [ "$n" -le 4 ] || why+="$n 'ignored' log lines (want 1 per bad value, not per tick); "
  [ -z "$why" ] && ok validate || no validate "$why"
}
t_hygiene() { # H6 + H9: log rotates; memo keeps no raw paths or titles and only live ids; state is owner-only
  fresh
  until_t 15 all_heads >/dev/null
  local d why=""; d=$(run_dir)
  head -c 300000 /dev/zero | tr '\0' x >> "$d/covrd.log"
  python3 - "$d/memo.json" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
m.setdefault("tcache", {})["/work/secret-repo|Secret session title"] = 1.0
m.setdefault("focus", {})["w999"] = 1.0
m["manual"] = (m.get("manual") or []) + ["w999"]
json.dump(m, open(sys.argv[1], "w"))
EOF
  for p in $(daemons); do kill "$p"; done; until_t 5 zero
  act start; sleep 7
  [ -f "$d/covrd.log.1" ] && [ "$(wc -c < "$d/covrd.log")" -lt 262144 ] || why+="log not rotated; "
  grep -q "secret" "$d/memo.json" && why+="raw tcache key kept; "
  grep -q "w999" "$d/memo.json" && why+="closed workspace kept in focus/manual; "
  python3 -c 'import json, sys; t = json.load(open(sys.argv[1])).get("tcache") or {}; sys.exit(0 if all(k.startswith("h:") for k in t) else 1)' "$d/memo.json" || why+="unhashed tcache keys; "
  python3 -c 'import os, sys; d = sys.argv[1].rstrip("/")
bad = [n for n, m in ((d, 0o700), (d + "/covrd.log", 0o600), (d + "/covrd.lock", 0o600)) if os.stat(n).st_mode & 0o777 != m]
sys.exit(bad and print(bad) or 0)' "$d" || why+="state readable by others; "
  [ -z "$why" ] && ok hygiene || no hygiene "$why"
}
t_fight() {   # H8: dragging spaces against an active sort makes the daemon back off instead of fighting
  fresh
  until_t 15 all_heads >/dev/null
  opt space_sort alpha; sleep 7
  local order rev
  order() { sb 'hsock workspace.list' | python3 -c 'import json, sys
print(" ".join(w["workspace_id"] for w in sorted(json.load(sys.stdin)["result"]["workspaces"], key=lambda w: w["number"])))'; }
  rev=$(order | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read().split()[::-1]))')
  for _ in 1 2 3 4; do            # the user keeps putting THEIR order (reverse alpha) back
    sb "hsock workspace.move_block '{\"workspace_ids\": $rev}'" >/dev/null
    sb 'set -- $(cat ~/panes); herdr pane report-agent $3 --source e2e --agent claude --state working' >/dev/null
    sleep 3
  done
  local mine; mine=$(order); sleep 8
  if grep -q "pausing space sorting" "$(run_dir)covrd.log" && [ "$(order)" = "$mine" ]; then ok fight
  else no fight "no backoff (log: $(grep -c 'spaces reordered' "$(run_dir)covrd.log") reorders)"; fi
}

t_reload() {  # updating the plugin's files in place restarts the running daemon onto the new code, rows intact
  fresh
  until_t 15 all_heads >/dev/null
  local before; before=$(daemons)
  touch "$PLUGIN_DIR/bin/covrd.py"
  until_t 15 grep -q "plugin code updated" "$(run_dir)covrd.log"
  sleep 3
  local after; after=$(daemons)
  if [ "$(ndaemons)" = 1 ] && [ "$(grep -c '^.* started ' "$(run_dir)covrd.log")" -ge 2 ] && all_heads; then ok reload
  else no reload "daemons before=$before after=$after, heads $(heads)"; fi
}

t_gitsafe() { # a repo's own git config can't run commands through the daemon's background `git status`
  fresh
  until_t 15 all_heads >/dev/null
  # core.fsmonitor runs on any status; a clean filter runs on a stat-dirty tracked file (g: touched, same bytes)
  sb 'mkdir -p ~/r/hostile && cd ~/r/hostile && git init -q && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m i \
      && echo x > f && echo g > g && echo "g filter=evil" > .gitattributes && git add f g .gitattributes \
      && git -c user.email=t@t -c user.name=t commit -qm f && echo y >> f && touch -d "2001-01-01" g \
      && git config core.fsmonitor "sh -c \"touch $HOME/PWNED\" #" \
      && git config filter.evil.clean "sh -c \"touch $HOME/PWNED; cat\"" \
      && git config filter.evil.process "sh -c \"touch $HOME/PWNED\""
      herdr workspace create --cwd ~/r/hostile --label hostile --no-focus >/dev/null'
  local w; w=$(sb 'hsock workspace.list' | python3 -c 'import json, sys
print([x["workspace_id"] for x in json.load(sys.stdin)["result"]["workspaces"] if x["label"] == "hostile"][0])')
  for p in $(daemons); do kill "$p"; done; until_t 5 zero; act start      # a fresh daemon checks dirty state at once
  until_t 15 eval 'sb "hsock workspace.list" | grep -q "\"dirty\":\"±\""'
  local dirty; dirty=$(sb 'hsock workspace.list' | python3 -c 'import json, sys
print([(x.get("tokens") or {}).get("dirty") for x in json.load(sys.stdin)["result"]["workspaces"] if x["workspace_id"] == sys.argv[1]][0])' "$w")
  if [ -e "$B/home/PWNED" ]; then no gitsafe "the repo's core.fsmonitor or filter driver ran"
  elif [ "$dirty" != "±" ]; then no gitsafe "dirty marker missing ($dirty)"
  else ok gitsafe; fi
}

t_viewed() {  # a finished agent you already have selected is marked viewed after seen_after, and its timer restarts
  fresh
  until_t 15 all_heads >/dev/null
  local p w; p=$(cut -d' ' -f1 < "$B/home/panes"); w=${p%%:*}
  status() { sb 'hsock agent.list' | python3 -c 'import json, sys
print([a["agent_status"] for a in json.load(sys.stdin)["result"]["agents"] if a["pane_id"] == sys.argv[1]][0])' "$p"; }
  sb "herdr workspace focus $w" >/dev/null; sleep 1
  $TM send-keys -t t:0 -l "$(printf '\033[O')"; sleep 0.5       # the terminal window goes to the background
  sb "herdr pane report-agent $p --source e2e --agent claude --state working" >/dev/null; sleep 1
  sb "herdr pane report-agent $p --source e2e --agent claude --state idle" >/dev/null; sleep 2
  local before; before=$(status)                                  # herdr: finished, not seen, though it is selected
  local why=""
  [ "$before" = done ] || why+="expected done while the terminal is in the background, got $before; "
  [[ "$(head_of "$p")" == "✓"* ]] || why+="row is not ✓ before seen_after ($(head_of "$p")); "
  until_t 12 eval '[ "$(status)" = idle ]' || why+="still $(status) 12 s later; "
  sleep 1
  local h; h=$(head_of "$p")
  [[ "$h" == "○"* ]] || why+="row is not ○ after being viewed ($h); "
  [[ "$h" == *"<1m" ]] || why+="timer did not restart ($h); "
  [ -z "$why" ] && ok viewed || no viewed "$why"
}

t_stopstays() { # stop (the action or s in the popup) holds: hooks do not start the daemon again, start does
  fresh
  until_t 15 all_heads >/dev/null
  act stop
  local why=""
  until_t 10 zero || why+="daemon still running after stop; "
  sb 'set -- $(cat ~/panes); herdr pane report-agent $1 --source e2e --agent claude --state working; herdr pane report-agent $2 --source e2e --agent claude --state idle' >/dev/null
  sleep 3                                                         # hooks fired: nothing may come back
  zero || why+="a hook restarted the daemon ($(ndaemons)); "
  no_heads || why+="rows came back while stopped ($(heads)); "
  act start
  until_t 20 all_heads || why+="not back after start ($(heads), $(ndaemons) daemons); "
  [ -z "$why" ] && ok stopstays || no stopstays "$why"
}

t_scrollhold() { # a blocked row is a snapshot: scrolling the pane up keeps it × with the same reason
  fresh
  until_t 15 all_heads >/dev/null
  local p; p=$(cut -d' ' -f1 < "$B/home/panes")
  wait_of() { sb 'hsock agent.list' | python3 -c 'import json, sys
print([(a.get("tokens") or {}).get("wait", "") for a in json.load(sys.stdin)["result"]["agents"] if a["pane_id"] == sys.argv[1]][0])' "$1"; }
  rep() { sb "herdr pane report-agent $p --source e2e --agent claude --state $1" >/dev/null; }
  scroll() { sb "hsock pane.scroll '{\"pane_id\":\"$p\",\"offset_from_bottom\":$1}'" >/dev/null; }
  sb "herdr pane run $p 'seq 1 300; echo Bash\\(make deploy\\); echo Do you want to proceed\\?'" >/dev/null; sleep 1
  rep blocked
  local why=""
  until_t 10 eval '[[ "$(head_of "$p")" == "×"* ]]' || why+="never blocked ($(head_of "$p")); "
  until_t 10 eval '[ "$(wait_of "$p")" = "↳ Bash(make deploy)" ]' || why+="reason is '$(wait_of "$p")'; "
  local w0; w0=$(wait_of "$p")
  scroll 120; sleep 0.5
  rep idle; sleep 3                                               # what herdr says once the prompt is out of view
  [[ "$(head_of "$p")" == "×"* ]] || why+="scrolled up, the row turned $(head_of "$p"); "
  [ "$(wait_of "$p")" = "$w0" ] || why+="scrolled up, the reason became '$(wait_of "$p")'; "
  scroll 0; sleep 0.5; rep blocked; sleep 3                       # back at the bottom: the prompt again
  [[ "$(head_of "$p")" == "×"* ]] || why+="back at the bottom, the row is $(head_of "$p"); "
  rep idle                                                        # answered, at the bottom: released (✓, not selected)
  until_t 10 eval '[[ "$(head_of "$p")" == "✓"* ]]' || why+="answered, the row is $(head_of "$p"); "
  [ -z "$(wait_of "$p")" ] || why+="answered, the reason stayed; "
  [ -z "$why" ] && ok scrollhold || no scrollhold "$why"
}

t_appscroll() { # Claude Code's fullscreen view scrolls inside the app (herdr's offset stays 0): its marker holds the row
  fresh
  until_t 15 all_heads >/dev/null
  local p; p=$(cut -d' ' -f1 < "$B/home/panes")
  wait_of() { sb 'hsock agent.list' | python3 -c 'import json, sys
print([(a.get("tokens") or {}).get("wait", "") for a in json.load(sys.stdin)["result"]["agents"] if a["pane_id"] == sys.argv[1]][0])' "$1"; }
  rep() { sb "herdr pane report-agent $p --source e2e --agent claude --state $1" >/dev/null; }
  sb "herdr pane run $p 'clear; echo Bash\\(make deploy\\); echo Do you want to proceed\\?'" >/dev/null; sleep 1
  rep blocked
  local why=""
  until_t 10 eval '[ "$(wait_of "$p")" = "↳ Bash(make deploy)" ]' || why+="reason is '$(wait_of "$p")'; "
  sb "herdr pane run $p 'clear; seq 1 20; echo \"   Jump to bottom (ctrl+End) ↓\"'" >/dev/null; sleep 1
  rep idle; sleep 3                                               # scrolled inside the app: herdr says idle
  [[ "$(head_of "$p")" == "×"* ]] || why+="scrolled in the app, the row turned $(head_of "$p"); "
  [ "$(wait_of "$p")" = "↳ Bash(make deploy)" ] || why+="scrolled in the app, the reason became '$(wait_of "$p")'; "
  sb "herdr pane run $p 'clear; echo Bash\\(make deploy\\); echo Do you want to proceed\\?'" >/dev/null; sleep 1
  rep blocked; sleep 3; rep idle                                  # back at the bottom, then answered: released
  until_t 10 eval '[[ "$(head_of "$p")" == "✓"* ]]' || why+="answered, the row is $(head_of "$p"); "
  [ -z "$why" ] && ok appscroll || no appscroll "$why"
}

ALL="single tokens restart gone sessions disable popup notoml layout rows events width seq cap validate hygiene fight reload gitsafe viewed stopstays scrollhold appscroll"
[ -n "${E2E_LIB:-}" ] && return 0   # sourced for its helpers (tools/screenshots/scene.sh)
for t in ${*:-$ALL}; do "t_$t"; done
[ -n "${KEEP:-}" ] || down
[ -n "${KEEP:-}" ] || for p in $(daemons); do kill "$p" 2>/dev/null; done
echo "---- $PASS passed, $FAIL failed ${FAILED[*]:-}"
[ "$FAIL" = 0 ]
