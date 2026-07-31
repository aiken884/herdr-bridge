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
mkdir -p "$WORKDIR"
herdr workspace create --cwd "$WORKDIR" --label sandbox >/dev/null
herdr agent start test-claude --cwd "$WORKDIR" -- claude >/dev/null
herdr agent start tester --cwd "$WORKDIR" -- bash >/dev/null
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
