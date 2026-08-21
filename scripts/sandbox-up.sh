#!/usr/bin/env bash
# herdr-bridge alpha testing: one-click launcher for a clean sandbox
# Usage: bash scripts/sandbox-up.sh [--no-trust]
#   --no-trust  don't auto-press Enter through claude's trust-folder screen (use this to watch a misjudgment demo firsthand)
# Safety: only touches the bridge-test named session and /tmp/sandbox-test; never the main session.
set -euo pipefail

BT_SOCK="$HOME/.config/herdr/sessions/bridge-test/herdr.sock"
WORKDIR="/tmp/sandbox-test"
NO_TRUST="${1:-}"

say() { printf '\033[1;36m[sandbox]\033[0m %s\n' "$*"; }

say "1/6 Clearing old sandbox (if any)..."
herdr session stop bridge-test >/dev/null 2>&1 || true
sleep 1
herdr session delete bridge-test >/dev/null 2>&1 || true

say "2/6 Starting new bridge-test headless server..."
nohup herdr --session bridge-test server > /tmp/bridge-test-server.log 2>&1 &
for i in $(seq 1 15); do
  [ -S "$BT_SOCK" ] && break
  sleep 0.5
done
[ -S "$BT_SOCK" ] || { echo "ERROR: sandbox socket did not appear (see /tmp/bridge-test-server.log)"; exit 1; }

export HERDR_SOCKET_PATH="$BT_SOCK"

say "3/6 Creating workspace and test panes..."
# Herdr 0.8.0 changed `agent start`'s CLI/socket contract: it no longer opens a new
# pane from a raw --cwd + -- <executable> argv; it now requires an *existing* pane
# (already at an interactive shell prompt) plus an enumerated --kind, and attaches
# the agent there. `workspace create`'s root pane is already such a shell prompt, so
# we start the claude agent in it directly, then `pane split` for the second plain
# shell pane (no --kind: it isn't an agent).
mkdir -p "$WORKDIR"
ROOT_PANE=$(herdr workspace create --cwd "$WORKDIR" --label sandbox | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['root_pane']['pane_id'])")
# A freshly created pane's shell isn't immediately ready for `agent start` to attach
# to — without this, herdr 0.8.0 rejects it with agent_pane_busy ("not an available
# shell"). One second is enough in practice; retried once as a safety margin.
sleep 1
# Herdr 0.8.2: agent start returns agent_not_ready (non-zero) when Claude is
# blocked on the trust screen, but the named agent IS already launched.
# Retrying then hits agent_name_taken. Both mean "already up" — do not abort.
# Unknown start errors still fail the script (set -e stays on around this).
_start_agent() {
  local out rc
  set +e
  out=$(herdr agent start test-claude --kind claude --pane "$ROOT_PANE" 2>&1)
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if echo "$out" | grep -Eq 'agent_not_ready|agent_name_taken'; then
    say "agent start: already launched ($out)"
    return 0
  fi
  echo "$out"
  return "$rc"
}
if ! _start_agent; then
  say "agent start failed once, retrying..."
  sleep 2
  _start_agent
fi
herdr pane split --pane "$ROOT_PANE" --direction down --cwd "$WORKDIR" >/dev/null
sleep 3

if [ "$NO_TRUST" != "--no-trust" ]; then
  say "4/6 Passing claude's trust-folder screen (auto-Enter)..."
  CLAUDE_PANE=$(herdr agent list 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(next(a['pane_id'] for a in d['result']['agents'] if a['name']=='test-claude'))")
  herdr pane send-keys "$CLAUDE_PANE" enter >/dev/null 2>&1 || true
  sleep 4
else
  say "4/6 Skipping auto-trust confirmation (--no-trust) — claude stays on the confirmation screen for observation"
fi

say "5/6 Verifying the sandbox is ready via herdr-bridge probe..."
cd "$(dirname "$0")/.."
uv run python -m herdr_bridge.probe snapshot --socket "$BT_SOCK"

say "6/6 Ready. Next steps:"
cat << EOF

  Sandbox socket: $BT_SOCK
  Prefix subsequent commands with: export HERDR_SOCKET_PATH=$BT_SOCK
  Watch it live (in another terminal): herdr --session bridge-test    (detach to leave)
  Start testing: see docs/alpha-testing-guide.md section 5
  Start over completely: just rerun this script
EOF
