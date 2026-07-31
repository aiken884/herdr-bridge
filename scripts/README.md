# herdr-bridge helper scripts

This directory holds developer helper scripts unrelated to the `src/` Python logic.

## Script list

| Script | Purpose |
|------|------|
| `sandbox-up.sh` | One-click launcher for a clean Herdr sandbox used in alpha testing |
| `rebuild-patched-opencode.sh` | Rebuilds the G1-fix opencode binary under `.vendor/opencode-patched/` (see below) |

> Git hooks (the `commit-msg` DCO check and the `post-commit` RemaGraph memory write-back)
> have moved to [`.githooks/`](../.githooks/README.md), enabled via `git config core.hooksPath`,
> and no longer live in this directory.

## rebuild-patched-opencode.sh

herdr-bridge's ACP command plane (`herdr_bridge.acp`) uses an opencode binary with a
self-applied fix for a child/subagent session ACP permission hang (G1), sourced from the
`fix/acp-child-session-permission-hang` branch of the `aiken884/opencode` fork. When upstream
advances that fork's dev branch, this script rebuilds and updates `.vendor/opencode-patched/`.

```bash
bash scripts/rebuild-patched-opencode.sh [path to the opencode fork, default ../opencode]
```

After it runs:
- The binary lands at `.vendor/opencode-patched/{platform}-{arch}/opencode` (e.g. `darwin-arm64/`), excluded by `.gitignore` and not version-controlled.
- `MANIFEST.json`'s `source_commit`/`built_at_utc`/`base_upstream_version` update automatically, but `compatible_upstream_range` (the manually verified compatible version range) is never widened automatically — if `base_upstream_version` falls outside that range, `resolve_patched_opencode_binary()` returns a warning (non-blocking, but callers should surface it); only widen the range by hand after confirming that `build_opencode_permission_config`'s `"*"`/MCP tool-mapping assumptions still hold for the new version.

## Compatibility

- **bash 3.2+** (compatible with macOS's built-in bash)
- No external dependencies (pure bash + POSIX tools)
