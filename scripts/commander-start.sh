#!/usr/bin/env bash
# One-click launcher for the herdr-bridge light-user command tower (Phase 1).
#
# Usage:
#   bash scripts/commander-start.sh              # check environment + status
#   bash scripts/commander-start.sh --sandbox    # auto-launch the test sandbox
#   bash scripts/commander-start.sh --run        # run the first task directly
#   bash scripts/commander-start.sh --run --dry-run
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

say() { printf '\033[1;35m[commander]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[commander]\033[0m %s\n' "$*" >&2; }

SANDBOX=0
RUN=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --sandbox) SANDBOX=1 ;;
    --run) RUN=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help)
      cat << EOF
herdr-commander light-mode launcher

  bash scripts/commander-start.sh              check environment
  bash scripts/commander-start.sh --sandbox    auto-launch the sandbox
  bash scripts/commander-start.sh --run        run the first task
  bash scripts/commander-start.sh --run --dry-run
EOF
      exit 0
      ;;
  esac
done

say "=== herdr-bridge command tower (light mode) ==="

if ! command -v herdr >/dev/null 2>&1; then
  err "herdr CLI not found"
  echo "Please install it first: https://herdr.dev"
  exit 1
fi

# Prefer the installed herdr-commander; fall back to running the uv module
run_cmd() {
  if command -v herdr-commander >/dev/null 2>&1; then
    herdr-commander "$@"
  else
    uv run python -m herdr_bridge.light "$@"
  fi
}

if [[ "$SANDBOX" -eq 1 ]]; then
  say "Starting sandbox environment..."
  bash "$ROOT/scripts/sandbox-up.sh" || {
    err "Sandbox startup failed"
    exit 1
  }
  export HERDR_SOCKET_PATH="${HOME}/.config/herdr/sessions/bridge-test/herdr.sock"
fi

if [[ "$RUN" -eq 1 ]]; then
  if [[ "$DRY" -eq 1 ]]; then
    run_cmd run --dry-run
  else
    run_cmd run
  fi
else
  EXTRA=()
  if [[ "$SANDBOX" -eq 1 ]]; then
    EXTRA+=(--auto-sandbox)
  fi
  run_cmd start "${EXTRA[@]+"${EXTRA[@]}"}"
fi
