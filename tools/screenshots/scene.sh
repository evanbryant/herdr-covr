#!/usr/bin/env bash
# tools/screenshots/scene.sh <out-dir> — build a demo session in an ISOLATED herdr (the e2e sandbox) and
# capture its screen for the README images. Neutral names only; never your own session.
#
# Writes <out-dir>/<shot>.ans (tmux capture with colours) and .txt for each shot:
#   covr-light   covr on catppuccin-latte, the default triage view
#   covr-project the same agents grouped by project
#   covr-dark    covr on catppuccin-mocha
#   settings     the settings popup
#   herdr-default herdr's own sidebar for the same agents (layout removed, daemon stopped)
# Render them with tools/screenshots/render.py (HTML) and any headless browser (see README there).
set -u
OUT=$(mkdir -p "$1" && cd "$1" && pwd)
HERE=$(cd "$(dirname "$0")" && pwd)
E2E_LIB=1 . "$HERE/../../tests/e2e/run.sh"

W=${W:-110}; H=${H:-30}
shot() { # <name>
  sleep "${SETTLE:-4}"
  $TM capture-pane -t t:0 -e -p > "$OUT/$1.ans"
  $TM capture-pane -t t:0 -p > "$OUT/$1.txt"
  echo "captured $1"
}
theme() { # <theme name> — the demo's herdr config; the plugin installs its own layout block after this
  printf 'onboarding = false\n\n[theme]\nname = "%s"\n\n[ui]\nsidebar_width = %s\n' "$1" "${SBW:-34}" > "$(cfg)"
}
titled() { # <pane> <title> [screen line ...] — set the terminal title (herdr reports it as the task), then print
  local PANE=$1 f; f="$B/home/.demo/$(echo "$1" | tr -c 'A-Za-z0-9\n' _).sh"; mkdir -p "${f%/*}"
  { printf "printf '\\033]2;%s\\007'\nclear\n" "$2"; shift 2; for l in "$@"; do printf "echo '%s'\n" "$l"; done; } > "$f"
  sb "herdr pane run $PANE 'sh $f'" >/dev/null
}
age() { # <pane> <seconds> — pretend the agent entered its state this long ago (edits the daemon's memo)
  AGES="${AGES:-} $1=$2"
}

down
for p in $(daemons); do kill "$p" 2>/dev/null; done
rm -rf "$B"; mkdir -p "$B/home/.config/herdr" "$B/run"; chmod 700 "$B/run"
theme catppuccin-latte
sb "herdr plugin link '$PLUGIN_DIR'" >/dev/null
$TM kill-server 2>/dev/null
$TM new-session -d -s t -x "$W" -y "$H" "$ROOT/sb.sh 'cd \$HOME; herdr; sleep 3600'"
until_t 20 sb 'herdr workspace list' >/dev/null 2>&1
sleep 1; $TM send-keys -t t Escape
act install-layout

# repos: api has a worktree on a feature branch, infra has uncommitted changes
sb 'set -e; R=~/src; mkdir -p $R; cd $R
  g() { git -c user.email=demo@example.com -c user.name=demo "$@"; }
  for n in api web infra docs mobile data; do mkdir -p $n; (cd $n && git init -q -b main && echo "# $n" > README.md && g add . && g commit -qm init); done
  (cd api && git worktree add -q -b feat/auth ../api-auth)
  echo "draft" >> infra/README.md
  mkdir -p notes scratch' >/dev/null

j() { sed -n "s/.*\"pane_id\":\"\([^\"]*\)\".*/\1/p" | head -1; }
ws() { sb "herdr workspace create --cwd ~/src/$1 --label $1 --no-focus" | j; }
P_api=$(ws api)
P_auth=$(sb 'herdr worktree open --cwd ~/src/api --path ~/src/api-auth --label api-auth --no-focus --trust-repository' | j)
P_web=$(ws web)
P_web2=$(sb "herdr pane split $P_web --direction right --cwd ~/src/web --no-focus" | j)
P_infra=$(ws infra)
P_docs=$(ws docs)
P_mobile=$(ws mobile)
P_data=$(ws data)
P_notes=$(ws notes)
P_scratch=$(ws scratch)

titled "$P_api" "Migrate users table" "  Bash(npm run migrate:up)" "  Do you want to proceed?"
titled "$P_auth" "Rotate session keys"
titled "$P_web" "Fix checkout redirect"
titled "$P_web2" "Tidy CSS tokens"
titled "$P_infra" "Terraform plan"
titled "$P_docs" "Update API reference"
titled "$P_mobile" "Release notes"
titled "$P_data" "Backfill events"
T_infra=$(sb 'hsock pane.list' | python3 -c 'import json, sys
r = json.load(sys.stdin)["result"]; print([p["tab_id"] for p in r.get("panes", r) if p["pane_id"] == sys.argv[1]][0])' "$P_infra")
sb "herdr tab rename $T_infra plan" >/dev/null
sleep 1

rep() { sb "herdr pane report-agent $1 --source demo --agent ${3:-claude} --state $2" >/dev/null; }  # [kind]
rep "$P_web" working codex      # finishes below, while unseen: done
rep "$P_api" blocked; rep "$P_auth" working; rep "$P_web2" idle; rep "$P_infra" working gemini
rep "$P_docs" idle; rep "$P_mobile" idle grok; rep "$P_data" working opencode; rep "$P_notes" idle cursor
W_scratch=$(sb 'hsock workspace.list' | python3 -c 'import json, sys
print([w["workspace_id"] for w in json.load(sys.stdin)["result"]["workspaces"] if w["label"] == "scratch"][0])')
sb "herdr workspace focus $W_scratch" >/dev/null
sb "herdr workspace close w1" >/dev/null 2>&1
sleep 1; rep "$P_web" idle codex
until_t 20 all_heads >/dev/null

# believable ages: the daemon keeps "entered this state at" per pane in its memo
age "$P_api" 130; age "$P_auth" 840; age "$P_web" 250; age "$P_web2" 540; age "$P_infra" 2220
age "$P_docs" 7300; age "$P_mobile" 380; age "$P_data" 3900; age "$P_notes" 18500
for p in $(daemons); do kill "$p"; done; until_t 5 zero
python3 - "$(ls "$B"/home/.local/state/herdr/plugins/covr.sidebar/s/*/memo.json)" $AGES <<'EOF'
import json, sys, time
path, pairs = sys.argv[1], sys.argv[2:]
m = json.load(open(path))
for kv in pairs:
    pane, sec = kv.rsplit("=", 1)
    m["since"][pane]["since"] = time.time() - int(sec)
json.dump(m, open(path, "w"))
EOF
act start
until_t 20 all_heads >/dev/null

shot covr-light
opt group_by project; shot covr-project; opt group_by none
act settings; SETTLE=2 shot settings; $TM send-keys -t t:0 q; sleep 1
theme catppuccin-mocha; act install-layout; sb 'herdr server reload-config' >/dev/null; shot covr-dark
theme catppuccin-latte; act stop; sb 'herdr server reload-config' >/dev/null; shot herdr-default

down
for p in $(daemons); do kill "$p" 2>/dev/null; done
echo "shots in $OUT"
