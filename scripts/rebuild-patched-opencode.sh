#!/usr/bin/env bash
# Rebuilds the patched opencode binary that herdr-bridge relies on (G1 fix:
# child/subagent session ACP permission hang), and updates the vendor snapshot
# under .vendor/opencode-patched/.
#
# Usage: bash scripts/rebuild-patched-opencode.sh [path to the opencode fork directory]
#   Default fork path: ../opencode (relative to this repo's root)
#
# Maintenance convention: rerun this script whenever upstream advances the opencode
# fork's dev branch. Afterward, manually confirm whether MANIFEST.json's
# compatible_upstream_range still covers the new base_upstream_version — don't widen
# the range yourself unless you've manually verified that build_opencode_permission_config's
# "*"/MCP tool-mapping assumptions still hold (see docs/acpx-adapter-implementation-plan.md §5 N1).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENCODE_DIR="${1:-$REPO_ROOT/../opencode}"
BRANCH="fix/acp-child-session-permission-hang"
VENDOR_DIR="$REPO_ROOT/.vendor/opencode-patched"

say() { printf '\033[1;36m[rebuild]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[rebuild] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$OPENCODE_DIR" ] || die "opencode fork directory does not exist: $OPENCODE_DIR"

say "1/5 Confirming branch and that the worktree is clean..."
cd "$OPENCODE_DIR"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$CURRENT_BRANCH" = "$BRANCH" ] || die "Currently on branch $CURRENT_BRANCH, expected $BRANCH — please switch/confirm manually first"
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "Worktree has uncommitted changes; clean it up before rebuilding"
fi

say "2/5 Building (bun run script/build.ts --single --skip-embed-web-ui)..."
cd packages/opencode
bun run script/build.ts --single --skip-embed-web-ui

BUILT_DIR="$(find dist -maxdepth 1 -type d -name 'opencode-*' | head -1)"
[ -n "$BUILT_DIR" ] || die "Build output directory not found (dist/opencode-*)"
BUILT_BIN="$BUILT_DIR/bin/opencode"
[ -x "$BUILT_BIN" ] || die "Build output not found or not executable: $BUILT_BIN"

say "3/5 Detecting target triple..."
UNAME_S="$(uname -s | tr '[:upper:]' '[:lower:]')"
UNAME_M="$(uname -m)"
case "$UNAME_M" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64) ARCH="x64" ;;
  *) die "Unknown machine architecture: $UNAME_M" ;;
esac
TARGET_TRIPLE="${UNAME_S}-${ARCH}"
say "target_triple=$TARGET_TRIPLE"

say "4/5 Copying binary to the vendor directory..."
DEST_DIR="$VENDOR_DIR/$TARGET_TRIPLE"
mkdir -p "$DEST_DIR"
cp "$BUILT_BIN" "$DEST_DIR/opencode"
chmod +x "$DEST_DIR/opencode"

say "5/5 Updating MANIFEST.json..."
cd "$OPENCODE_DIR"
SOURCE_COMMIT="$(git rev-parse HEAD)"
BASE_VERSION="$(grep -m1 '"version"' packages/opencode/package.json | sed -E 's/.*"version": *"([^"]+)".*/\1/')"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

python3 - "$VENDOR_DIR/MANIFEST.json" "$SOURCE_COMMIT" "$BASE_VERSION" "$BUILT_AT" "$TARGET_TRIPLE" <<'PYEOF'
import json
import sys

path, source_commit, base_version, built_at, target_triple = sys.argv[1:6]
try:
    with open(path) as f:
        manifest = json.load(f)
except FileNotFoundError:
    manifest = {
        "source_repo": "https://github.com/aiken884/opencode",
        "source_branch": "fix/acp-child-session-permission-hang",
        "purpose": (
            "G1 fix (child/subagent session ACP permission hang) applied on top "
            "of opencode dev, not yet merged upstream. Tracked via "
            "docs/acpx-adapter-implementation-plan.md."
        ),
        "upstream_issue": "https://github.com/anomalyco/opencode/issues/12133",
        "upstream_pr": "https://github.com/anomalyco/opencode/pull/37902",
    }

manifest["source_commit"] = source_commit
manifest["built_at_utc"] = built_at
manifest["build_command"] = "bun run script/build.ts --single --skip-embed-web-ui"
manifest["target_triple"] = target_triple
manifest["base_upstream_version"] = base_version
manifest.setdefault(
    "compatible_upstream_range",
    {"min_inclusive": base_version, "max_inclusive": base_version},
)

with open(path, "w") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
PYEOF

say "Done. Output: $DEST_DIR/opencode (version $BASE_VERSION, commit ${SOURCE_COMMIT:0:12})"
say "⚠ If compatible_upstream_range isn't updated manually, it keeps the old value or treats this run's base_version as the sole value — only widen the range by hand after confirming the permission-mapping assumptions still hold."
