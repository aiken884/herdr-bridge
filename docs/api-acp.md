# ACP Command Plane API Reference

> **Status: provisional/experimental additive** (see [`BOUNDARIES.md`](../BOUNDARIES.md)) — not
> covered by the five-function semver-frozen surface described in [`api.md`](api.md); breaking
> changes may ship in a patch or minor release.
> The upstream `acpx` is alpha (0.12.0); this implementation drives a locally-patched opencode
> fork fix that has not yet been merged upstream
> (G1, [anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902)).
> Full design and verification history: ADR 0002, ADR 0003.

`herdr_bridge.acp` is the **command plane** that coexists with the five-function observation
plane (§4.4, two-plane architecture): the observation plane (the five top-level `herdr_bridge`
functions) gives humans visibility, layout control, and an emergency manual override; the command
plane drives opencode via ACP (Agent Client Protocol) plus the acpx CLI, giving callers a
structured `session/update` event stream and an explicit `stopReason` — replacing "blind-type into
a terminal and grep for a marker."

All public symbols can be imported from `herdr_bridge.acp`:
`from herdr_bridge.acp import connect, AcpActions, ...` (see `herdr_bridge.acp.__all__` for the
full list).

## `connect()`

```python
def connect(
    *,
    acpx_bin: str = "acpx",
    config_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    transcript_dir: str | Path | None = None,
    strict_version: bool = False,
) -> AcpActions
```

Builds a usable `AcpActions`: it wraps an `AcpxTransport` (which drives the acpx CLI subprocess)
and reuses the existing `AuditLogger` — the same JSONL file, not a new one; action names carry an
`acp.` prefix.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `acpx_bin` | `str` | `"acpx"` | Name/path of the acpx CLI executable to invoke |
| `config_path` | `str \| Path \| None` | `None` | Root directory for per-session permission config files (the `session_dir` used by `AcpxTransport`); when `None`, a temp directory that lives for the process's lifetime is used (`tempfile.mkdtemp`) |
| `audit_path` | `str \| Path \| None` | `None` | Same meaning as `audit_path` on `herdr_bridge.connect()` — this is the same audit log, not a separate file |
| `transcript_dir` | `str \| Path \| None` | `None` | **Currently a no-op.** §4.4 documents transcript mirroring as something the O1/O2 optimization spike needs; the O3 baseline (this implementation) does not. The parameter is accepted only to keep the signature shape stable |
| `strict_version` | `bool` | `False` | When `True`, calls `resolve_patched_opencode_binary()` to check whether `.vendor/opencode-patched/MANIFEST.json`'s `base_upstream_version` falls within the manually-verified `compatible_upstream_range`; if it falls outside that range, raises `AcpAdapterError` immediately (fail loud). This is **not** the acpx↔agent `protocolVersion` handshake check planned under M0-V9 — that check is not implemented at this stage |

**Returns**: an `AcpActions` instance.

**Raises**: `AcpAdapterError` — when `strict_version=True` and the version check fails, or when no
matching platform binary can be found under `.vendor/opencode-patched/`.

```python
from herdr_bridge.acp import connect

acp = connect(audit_path="/var/log/herdr-bridge/audit.jsonl")
```

## `AcpActions` methods

The nine methods below all belong to `AcpActions`. Except for `close()`, every method's first
parameter is `actor_id: str` (same semantics as the `herdr_bridge` five functions: recorded for
audit purposes only — never validated, never blocks the call).

### `list_acp_agents`

```python
def list_acp_agents(self, actor_id: str) -> list[AcpAgentSpec]
```

Lists the agent tiers that currently have a resolver wired up. **Currently reports two built-in
tiers, `"opencode"` and `"claude"`** (`builtin=True`) — additional tiers only appear once a named
agent entry is added to acpx's own `config.json` by whatever manages that configuration (§4.5:
"one acpx config entry per tier... configuration is written elsewhere, bridge only reads it").
Read-only; never writes.

- **Audit record**: `action="acp.list_acp_agents"`, with an extra `count` field.

### `ensure_session`

```python
def ensure_session(
    self, actor_id: str, agent: str, workdir: str, session_name: str,
    *, policy: AcpPolicy | None = None,
) -> AcpSessionInfo
```

Creates (or recovers an existing) named ACP session. **Idempotent** — if this `AcpActions`
instance already has the given `session_name` on record, it returns the existing
`AcpSessionInfo` as-is, without calling acpx again and without rewriting the permission config
file.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `agent` | `str` | — | Accepts `"opencode"` or `"claude"`; any other value raises `AcpSessionError` |
| `workdir` | `str` | — | The ACP session's working directory (`--cwd`) |
| `session_name` | `str` | — | A caller-chosen session identifier (a display alias, not the canonical join key — the canonical join key is `binding.py`'s `pane_id` plus the dispatch ledger; see "Pane↔session binding" below) |
| `policy` | `AcpPolicy \| None` | `None` (= `approve-all`) | Determines how the local opencode permission config gets mapped (`build_opencode_permission_config`); once the session is created this policy is fixed — switching policy mid-session requires `close_session` followed by a fresh `ensure_session` (hot-swapping isn't supported at this stage) |

- **Raises**: `AcpSessionError` — `agent` isn't supported, the underlying `acpx sessions ensure`
  returns a non-zero exit code, or (when `agent="opencode"`) `workdir` fails the workdir/worktree
  isolation check from ADR 0003 Decision #2 (see "Workdir isolation (`agent="opencode"`)" below).
- **Audit record**: `action="acp.ensure_session"`, with extra fields `session_name`, `agent`,
  `policy_mode` (on an idempotent hit, only `idempotent_hit=True` is recorded, without
  `policy_mode`). When the isolation check rejects the call, an additional `rejected_reason`
  field (str) records which of the two violations occurred — using the primary worktree, or
  sharing a workdir with another session.

#### Workdir isolation (`agent="opencode"`)

ADR 0003 Decision #2 plus known limitation #2: when `agent="opencode"`, `ensure_session()` (and
`exec_prompt()`, which also creates sessions directly) runs
`git -C <workdir> worktree list --porcelain` before calling the underlying
`acpx sessions ensure`, checking two things (both compared after `Path.resolve()`, never as raw
strings, to prevent symlink/hardlink bypasses):

1. `workdir` must not be the **primary worktree** of its git repo (the first `worktree` line in
   the porcelain output).
2. `workdir` must not be the same as the workdir already occupied by any **existing active
   session** (`closed=False`).

Either violation raises `AcpSessionError`, with the message containing `PRIMARY` or `SHARED`
respectively so the cause can be identified. If the `git worktree list --porcelain` call itself
fails (`workdir` isn't a git repo, `git` isn't installed, it times out, etc.), the check **fails
closed** — the call is always rejected; nothing is guessed.

`agent="claude"` is not subject to this restriction (Decision #3: ACP permission negotiation has
been confirmed to work for the claude adapter).

**Known limitation** (known limitation #1, not an oversight at this stage): this is a
workdir-level check performed once, at the moment `ensure_session()` is called — it is not a
process-level/runtime interception. Once a session exists, opencode can still `cd`/
`git checkout`/`git reset` its own state, or touch paths outside the workdir, through its own tool
calls; none of that is covered by this line of defense.

### `close_session`

```python
def close_session(self, actor_id: str, session_name: str) -> None
```

Closes a session: calls `acpx sessions close`, and removes that session's dedicated permission
config directory (the `session_dir/{session_name}/` subdirectory — see "Known limitations" item
N5 for the fix).

- **Raises**: `AcpSessionError` — `session_name` is unknown (never passed to `ensure_session()`,
  or already closed).
- **Audit record**: `action="acp.close_session"`, with an extra `session_name` field.

### `get_history`

```python
def get_history(self, actor_id: str, session_name: str) -> list[AcpEvent]
```

Reads a session's historical events (`acpx sessions read`).

- **Raises**: `AcpSessionError` — `session_name` is unknown.
- **Audit record**: `action="acp.get_history"`, with extra fields `session_name`, `count`.

### `prompt`

```python
def prompt(
    self, actor_id: str, session_name: str, text: str,
    *, priority: int = 0, policy: AcpPolicy | None = None,
    timeout_sec: float = 600, on_event: Callable[[AcpEvent], None] | None = None,
) -> PromptResult
```

Blocks until a `stopReason` is received, or until `timeout_sec` elapses.

- **Parameters**: `priority` — an arbitration-priority **reserved field** (following the same
  convention as `herdr_bridge.send_to_agent`); it is written to the audit log as-is and does not
  affect ordering or queue-jumping. `policy` — **currently unused**: policy can only be decided
  once, at `ensure_session()` time; this parameter is accepted here purely to match the frozen
  signature from §4.2 — passing it has no effect (this is not a no-op pretending to work, it is an
  honest lack of effect). `on_event` — called once per `AcpEvent` received, for live streaming
  display.
- **Returns**: `PromptResult`. `reason` is the bridge's own frozen vocabulary
  (`Literal["stop","timeout","error","canceled"]`); `stop_reason` is the protocol vocabulary
  passed through unchanged (a plain `str`, not constrained to a `Literal` — see the note on the
  two-tier vocabulary above).
- **Raises**: `AcpSessionError` — `session_name` is unknown. **A timeout does not raise** — it
  returns `PromptResult(reason="timeout", ...)` instead.
- **Audit record**: `action="acp.prompt"`, with extra fields `session_name`, `priority`, `chars`,
  `reason`, `stop_reason`.

### `exec_prompt`

```python
def exec_prompt(
    self, actor_id: str, agent: str, text: str,
    *, workdir: str, policy: AcpPolicy | None = None, timeout_sec: float = 600,
) -> PromptResult
```

Stateless and one-shot: internally generates a disposable `session_name` (`exec-<uuid4>`) and
always calls `close_session` once the call finishes — whether it succeeded, raised, or timed out
— leaving nothing behind for the caller to clean up. A good fit for "one task, no multi-turn
context needed."

`exec_prompt()` creates an opencode session directly, just like `ensure_session()`, and is subject
to the same ADR 0003 workdir isolation check (see "Workdir isolation (`agent="opencode"`)" above).

- **Raises**: `AcpSessionError` — when `agent="opencode"` and `workdir` fails the workdir/worktree
  isolation check.
- **Audit record**: `action="acp.exec_prompt"`, with extra fields `agent`, `chars`, `reason`,
  `stop_reason`; when the isolation check rejects the call, it additionally records `session_name`
  (the internally-generated disposable name) and `rejected_reason`.

### `start_prompt` / `wait_done`

```python
def start_prompt(self, actor_id: str, session_name: str, text: str) -> PromptHandle
def wait_done(self, actor_id: str, handle: PromptHandle, *, timeout_sec: float = 60) -> PromptResult
```

The non-blocking counterpart to `prompt`: `start_prompt` returns a `PromptHandle` immediately
(backed by a live acpx subprocess), and `wait_done` is what actually waits for completion.
**`wait_done` never raises** — everything is expressed through `PromptResult.reason` (mirroring
the existing `wait_until`/`WaitResult` philosophy); on a timeout you can simply call `wait_done`
again (the process isn't killed — you just didn't wait long enough this time).

Known simplification: events under a `PromptHandle` are **invisible until `wait_done()`
completes** (a single `Popen.communicate()` read, not a background thread accumulating output
line by line) — use `prompt(on_event=...)` if you need to see the stream mid-flight.

- **Audit record**: `start_prompt` records `action="acp.start_prompt"` (`session_name`, `chars`);
  `wait_done` records `action="acp.wait_done"` (`session_name`, `reason`, `stop_reason`).

### `cancel`

```python
def cancel(self, actor_id: str, handle: PromptHandle) -> None
```

Terminates an in-flight `PromptHandle`. **Known simplification**: this currently sends SIGTERM
directly to the underlying acpx subprocess (SIGKILL after a 5-second timeout), rather than acpx's
real `session/cancel` protocol message (which would require a separate named
`acpx <agent> cancel` call, depending on named-agent-tier addressing that hasn't landed yet — see
"Known limitations"). Blunt, but reliably effective; the cost is that you don't get opencode's
precise `stopReason="cancelled"` report (a known M0-spike protocol defect where `cancelled` is
misreported as `end_turn`).

- **Audit record**: `action="acp.cancel"`, with an extra `session_name` field.

### `close`

```python
def close(self) -> None
```

Closes **every** active session currently on record for this `AcpActions` instance (running the
same underlying logic as `close_session`, one by one). Call this before the process exits; it is
not audit-logged (there is no single, well-defined `actor_id` to attribute it to).

## Data models

All are `@dataclass(frozen=True)` (defined in `herdr_bridge.acp.models`).

### `AcpAgentSpec`

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Agent tier identifier (currently `"opencode"` or `"claude"`) |
| `command` | `str` | Descriptive, read-only field (at this stage a fixed resolver description string, not an executable command) |
| `builtin` | `bool` | `True` = this adapter already has a resolver wired up; `False` = a custom entry from acpx's own config (currently always empty at this stage) |

### `AcpSessionInfo`

| Field | Type | Description |
|---|---|---|
| `session_name` | `str` | Caller-chosen display alias |
| `agent` | `str` | Tier identifier |
| `workdir` | `str` | |
| `acp_session_id` | `str \| None` | Fixed at `None` at this stage (acpx's `sessions ensure` text output doesn't reliably return an ID; this can be filled in later if acpx adds a `--format json` output for `ensure`) |
| `closed` | `bool` | |

### `AcpEvent`

| Field | Type | Description |
|---|---|---|
| `type` | `str` | ACP protocol vocabulary (a `session/update` variant type such as `agent_message_chunk`/`tool_call`; or a top-level JSON-RPC method name); this is a tolerant reader — unknown values pass through and never raise |
| `session_id` | `str \| None` | |
| `text` | `str \| None` | |
| `raw` | `dict[str, Any]` | The raw JSON-RPC message |

### `PromptResult`

| Field | Type | Description |
|---|---|---|
| `reason` | `Literal["stop","timeout","error","canceled"]` | The bridge's own frozen vocabulary |
| `stop_reason` | `str \| None` | Protocol vocabulary passed through unchanged, not constrained to a `Literal` |
| `text` | `str` | The concatenated text of all `agent_message_chunk` events |
| `session_name` | `str` | |
| `usage` | `dict[str, Any] \| None` | |
| `error` | `str \| None` | Description string, present when `reason` is `"error"`/`"timeout"` |

### `AcpPolicy`

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | `str` | `"approve-all"` | `"approve-all"`/`"approve-reads"`/`"deny-all"`; an unrecognized value raises `AcpAdapterError` at `ensure_session()` time (fail closed) |
| `policy_enforced` | `bool \| None` | `None` | An audit-log-only field; not derived by this class itself |

## Exception hierarchy

```
AcpError                       Common base for every herdr_bridge.acp exception (independent of HerdrBridgeError)
├── AcpAdapterError            acpx subprocess returned a non-zero exit code; or the strict_version check failed; or no built platform binary could be found
├── AcpTransportError          NDJSON read/parse failure (not "unknown event type" — that's absorbed by the tolerant reader)
├── AcpSessionError            Session addressing failed: unknown session_name, unsupported agent, or ensure_session failed
├── AcpTimeoutError            (reserved for future call paths that need to raise on timeout rather than return a timeout reason)
└── AcpVersionError            acpx↔agent protocolVersion handshake mismatch (M0-V9; the corresponding check is not implemented at this stage)
```

## Audit events

| action | extra fields | triggered when |
|---|---|---|
| `acp.list_acp_agents` | `count` | every call |
| `acp.ensure_session` | `session_name`, `agent`, `policy_mode`\*, `idempotent_hit` | every call (\*absent on an idempotent hit) |
| `acp.close_session` | `session_name` | after a successful close |
| `acp.get_history` | `session_name`, `count` | every call |
| `acp.prompt` | `session_name`, `priority`, `chars`, `reason`, `stop_reason` | when the call finishes (including on timeout/error) |
| `acp.exec_prompt` | `agent`, `chars`, `reason`, `stop_reason` | when the call finishes |
| `acp.start_prompt` | `session_name`, `chars` | immediately after dispatch |
| `acp.wait_done` | `session_name`, `reason`, `stop_reason` | when the wait finishes |
| `acp.cancel` | `session_name` | after the termination signal is sent |

Same JSONL file (`AuditLogger`, via `connect(audit_path=...)`) — it shares the file and format
(`ts`/`actor_id`/`action` plus extra fields) with the audit records from the `herdr_bridge` five
functions; only the `action` values carry the `acp.` prefix.

## Pane↔session binding (`herdr_bridge.acp.binding`)

Policy-neutral pure functions, available for whatever coordinates dispatch to use at its own
discretion; `AcpActions` never calls these on its own. The canonical join key is the herdr
`pane_id` plus the dispatch ledger — **not** `session_name` (`session_name` is just a schema-free
string bridge; once a session is rebuilt, an old key silently loses its link).

- `record_dispatch(ledger, *, pane_id, session_name, actor_id, dispatched_at) -> list[LedgerEntry]`
  — append-only, returns a new list.
- `current_binding_for_pane(ledger, pane_id) -> LedgerEntry | None`
- `current_binding_for_session(ledger, session_name) -> LedgerEntry | None`
- `detect_drift(ledger, *, actual_pane_session_map) -> list[str]` — reconciles the two, returning
  the `pane_id`s where a mismatch was found.

## Known limitations (documented deliberately, not blocking)

- **Only `agent="opencode"` and `"claude"` are supported**: additional tiers await a named acpx
  agent entry being configured elsewhere (§4.5). claude goes through acpx's named subcommand
  (`acpx claude`), with global flags (`--cwd`/`--ttl`/policy flags) placed before `claude`;
  opencode-specific env vars such as `OPENCODE_CONFIG` don't apply.
- **`cancel()` kills the subprocess — it is not a protocol-level `session/cancel`**: see the
  `cancel` section above.
- **`start_prompt`/`wait_done` cannot see intermediate events**: the full event list only becomes
  visible once `wait_done()` completes; use `prompt(on_event=...)` for live streaming.
- **`prompt()`'s `policy` parameter has no effect**: policy can only be decided once, at
  `ensure_session()` time.
- **`strict_version` does not cover the M0-V9 protocolVersion handshake**: it only checks N1's
  `base_upstream_version` range.

### Building the patched opencode binary yourself (`agent="opencode"` tier only)

The `agent="opencode"` tier depends on a locally-built opencode binary carrying a fix for a real
upstream bug (child/subagent ACP sessions were never registered, hanging any prompt that needed to
ask permission for a delegated subagent's own action — see
[anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902), open upstream, not
yet merged as of this writing). Until that PR merges, `herdr_bridge.acp` looks for this binary
under `.vendor/opencode-patched/<target-triple>/opencode` (gitignored — you build it, it's not a
version-controlled artifact and not part of the published package).

To build it:

1. Clone the patched fork alongside this repo: `git clone https://github.com/aiken884/opencode ../opencode`
   (relative to wherever you checked out herdr-bridge).
2. Check out the fix branch: `git -C ../opencode checkout fix/acp-child-session-permission-hang`.
3. From this repo's root, run `bash scripts/rebuild-patched-opencode.sh`. It builds opencode with
   `bun`, detects your platform's target triple, copies the binary into
   `.vendor/opencode-patched/<target-triple>/`, and writes a `MANIFEST.json` recording the source
   commit and build time.
4. Re-run with `strict_version=True` on `connect()` if you want `AcpAdapterError` raised loudly
   should the fork's base version drift outside the manually-verified compatible range.

Requirements: `git`, `bun`, and a Unix-like OS (the build script is bash and assumes `uname`).
`agent="claude"`/`"copilot"`/`"grok-build"` tiers do not need any of this — they run through
`acpx`'s own native subcommands and are unaffected.

Note that neither `scripts/rebuild-patched-opencode.sh` nor this file currently ship in the
published PyPI package (only `docs/api.md` does, per `pyproject.toml`'s sdist manifest) — if you
only have a `pip install`ed copy of herdr-bridge, clone the GitHub repo to get the script and this
reference.

### Bypassing this module and shelling out directly to the native `opencode run` CLI: exit code is unreliable (observed 2026-07-24)

This module (`herdr_bridge.acp`) talks over the ACP protocol directly and is unaffected by this
issue; what follows documents a pitfall specific to dispatching via the **native `opencode run`
one-shot CLI** (not ACP), for any caller that shells out to `opencode run` (including any future
non-ACP dispatch wrapper) — this is itself one of the reasons this module chose ACP over parsing
raw CLI output.

- **Observed behavior**: running `opencode run "..." -m opencode-go/deepseek-v4-pro` (plain-text
  mode, without `--format json`) once the OpenCode Go plan's quota was exhausted printed
  `Error: Invalid API key.` to the screen, but **the command's exit code was still 0**. Any caller
  that judges dispatch success/failure purely by exit code will misread this as success.
- **Hypothesis ruled out**: triggering a genuine 401 against the Anthropic API directly with a
  fake key (`invalid x-api-key`) correctly returns exit code 1 (verified on both the local dev
  branch and the global Homebrew v1.18.4 build). So this is not a blanket failure of
  `opencode run`'s error handling — it's more likely tied to the retry/fallback path taken
  specifically when an `opencode-go/*` (OpenCode Go plan) quota is exhausted. The root cause
  hasn't been tracked down further, and fixing opencode itself is out of scope for now.
- **Reliable detection method (verified)**: add `--format json`, read the NDJSON event stream
  instead, and check for a line with `"type":"error"`:
  ```json
  {"type":"error","timestamp":...,"sessionID":"...","error":{"name":"APIError","data":{"message":"invalid x-api-key","statusCode":401,"isRetryable":false,...}}}
  ```
  Seeing `"type":"error"` anywhere means the dispatch failed, regardless of exit code;
  `error.data.message`/`statusCode`/`isRetryable` can be used to decide whether a retry is
  worthwhile. **Any future caller dispatching through the native CLI (not ACP) should always add
  `--format json` and check this field — never trust exit code alone.**

## Central Tower facade (Option A: the single recommended command-tower abstraction for external use)

To spare external callers from needing to understand too much internal surface area
(`create_herdr_router`, `prepare`, worktrees, and so on), a higher-level synchronous facade,
`CentralTower`, was added.

```python
from herdr_bridge import create_central_tower   # or from herdr_bridge.acp.router import ...

tower = create_central_tower(project="my-central-tower")
result = tower.dispatch("please research the latest regulatory changes", target=None)  # auto-routes by capability
print(result["routed_to"], result["task_id"], result["ok"])

results = tower.batch_dispatch([
    "echo hello",
    "research quantum",
    "implement helper func",
])
```

**API highlights**:
- `create_central_tower(project=..., additional_paths=...) -> CentralTower`
- `tower.dispatch(prompt: str, *, target: str|None = None) -> dict` (ok, routed_to, response, task_id, agent_id)
- `tower.batch_dispatch(prompts: list[str], *, target=None) -> list[dict]`
- `tower.register_agent(...)`, `tower.list_agents()`, `tower.get_registry_summary()`
- **Every code path enforces** Herdr Bridge Memory's `prepare_dispatch_text` (recall+augment) plus `store_memory`
- All internals are hidden: router details, Herdr Bridge Memory calls, ACP spawning, worktrees, asyncio

A lower-level `create_herdr_router()` / `AcpRouter` is still available for callers who need finer
control.

See `examples/central-tower-minimal.py` and the cross-project examples for updated usage. Docs
and tests have been aligned with the "single command-tower plug-in" goal.

---

# ACP 指揮面 API 參考

> **狀態：provisional/experimental additive**（見 [`BOUNDARIES.md`](../BOUNDARIES.md)）——不受
> [`api.md`](api.md) 描述的五函式 semver 凍結面約束，破壞性變更可能隨 patch/minor 版號發生。
> 上游 `acpx` 是 alpha（0.12.0）；本機驅動的是一個尚未合併上游的 opencode fork 修復
> （G1，[anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902)）。
> 設計與驗證全紀錄：ADR 0002、ADR 0003。

`herdr_bridge.acp` 是與五函式監看面並存的**指揮面**（§4.4 兩面架構）：監看面（`herdr_bridge`
頂層五函式）給人眼可視、佈局、緊急人工介入；指揮面用 ACP（Agent Client Protocol）+ acpx CLI
驅動 opencode，取得結構化的 `session/update` 事件流與明確的 `stopReason`，取代「盲打終端 +
grep marker」。

全部公開符號可從 `herdr_bridge.acp` 匯入：`from herdr_bridge.acp import connect, AcpActions, ...`
（完整清單見 `herdr_bridge.acp.__all__`）。

## `connect()`

```python
def connect(
    *,
    acpx_bin: str = "acpx",
    config_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    transcript_dir: str | Path | None = None,
    strict_version: bool = False,
) -> AcpActions
```

建立一個可用的 `AcpActions`：包一個 `AcpxTransport`（驅動 acpx CLI 子行程），復用既有
`AuditLogger`（同一支 JSONL，不是新建；action 名 `acp.` 前綴）。

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `acpx_bin` | `str` | `"acpx"` | 呼叫的 acpx CLI 執行檔名稱／路徑 |
| `config_path` | `str \| Path \| None` | `None` | 每個 session 的權限設定檔存放根目錄（`AcpxTransport` 的 `session_dir`）；`None` 時用一個隨行程存活的暫存目錄（`tempfile.mkdtemp`） |
| `audit_path` | `str \| Path \| None` | `None` | 同 `herdr_bridge.connect()` 的 `audit_path`——同一支 audit log，不是獨立檔案 |
| `transcript_dir` | `str \| Path \| None` | `None` | **目前是 no-op**——§4.4 明確記載 transcript 鏡像是 O1/O2 優化 spike 的配套，O3 baseline（本實作）不需要。接受這個參數只是保留簽名形狀 |
| `strict_version` | `bool` | `False` | `True` 時用 `resolve_patched_opencode_binary()` 檢查 `.vendor/opencode-patched/MANIFEST.json` 的 `base_upstream_version` 是否落在人工驗證過的 `compatible_upstream_range` 內，落在範圍外直接拋 `AcpAdapterError`（fail loud）。**不是** M0-V9 規劃的 acpx↔agent `protocolVersion` 握手驗證——那項本階段尚未實作 |

**回傳**：`AcpActions` 實例。

**例外**：`AcpAdapterError`——`strict_version=True` 且版本檢查未過；或 `.vendor/opencode-patched/`
下找不到對應平台的二進位。

```python
from herdr_bridge.acp import connect

acp = connect(audit_path="/var/log/herdr-bridge/audit.jsonl")
```

## `AcpActions` 逐方法

以下九個方法皆為 `AcpActions` 的方法。除 `close()` 外，第一個參數皆為 `actor_id: str`（語意同
`herdr_bridge` 五函式：僅記錄、不驗證、不阻擋呼叫）。

### `list_acp_agents`

```python
def list_acp_agents(self, actor_id: str) -> list[AcpAgentSpec]
```

列出目前已接好 resolver 的 agent tier。**目前回報 `"opencode"` 與 `"claude"` 兩個內建 tier**
（`builtin=True`）——其餘 tier 待有人在 acpx 自己的 `config.json` 設具名 agent 條目後才擴充
（§4.5：「每 tier 一 acpx config 條目……config 由外部維護、bridge 只讀」）。只讀不寫。

- **Audit 記錄**：`action="acp.list_acp_agents"`，附加欄位 `count`。

### `ensure_session`

```python
def ensure_session(
    self, actor_id: str, agent: str, workdir: str, session_name: str,
    *, policy: AcpPolicy | None = None,
) -> AcpSessionInfo
```

建立（或找回既有）一個具名 ACP session。**冪等**——同一個 `session_name` 已在本 `AcpActions`
實例記錄在案時，直接回傳既有的 `AcpSessionInfo`，不重新呼叫 acpx（不重寫權限設定檔）。

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `agent` | `str` | — | 接受 `"opencode"` 或 `"claude"`；其餘值拋 `AcpSessionError` |
| `workdir` | `str` | — | ACP session 的工作目錄（`--cwd`） |
| `session_name` | `str` | — | 呼叫端自訂的 session 識別字（display alias，非 canonical join key——canonical join key 是 `binding.py` 的 `pane_id` + ledger，見下方〈pane↔session 綁定〉） |
| `policy` | `AcpPolicy \| None` | `None`（= `approve-all`） | 決定 opencode 本地權限 config 的映射（`build_opencode_permission_config`）；session 建立後這個 policy 就固定了，同一個活著的 session 中途換 policy 需要 `close_session` 後重新 `ensure_session`（本階段不支援熱切換） |

- **例外**：`AcpSessionError`——`agent` 不支援、底層 `acpx sessions ensure` 非零 exit code，或
  `agent="opencode"` 時 `workdir` 未通過 ADR 0003 Decision #2 的 workdir/worktree 隔離檢查
  （見下方〈workdir 隔離（`agent="opencode"`）〉）。
- **Audit 記錄**：`action="acp.ensure_session"`，附加欄位 `session_name`、`agent`、`policy_mode`
  （冪等命中時只有 `idempotent_hit=True`，不含 `policy_mode`）。隔離檢查拒絕時額外記
  `rejected_reason`（str，訊息區分是主要工作樹還是與其他 session 共用 workdir 這兩種違規）。

#### workdir 隔離（`agent="opencode"`）

ADR 0003 Decision #2 + 已知邊界 #2：`agent="opencode"` 時，`ensure_session()`（以及內部同樣直接
建 session 的 `exec_prompt()`）在呼叫底層 `acpx sessions ensure` 之前，會先用
`git -C <workdir> worktree list --porcelain` 檢查兩件事（皆先 `Path.resolve()` 再比對，不做純
字串比對，防 symlink/hardlink 繞過）：

1. `workdir` 不得是其所屬 git repo 的**主要工作樹**（porcelain 輸出第一筆 `worktree` 行）。
2. `workdir` 不得與任何**既有 active session**（`closed=False`）已佔用的 workdir 相同。

兩項違規都拋 `AcpSessionError`，訊息分別含 `PRIMARY`／`SHARED` 字樣以利判斷。
`git worktree list --porcelain` 呼叫本身失敗（`workdir` 不是 git repo、`git` 不存在、逾時等）
**fail-closed**——一律拒絕，不嘗試猜測。

`agent="claude"` 不受此限制（Decision #3：ACP 權限協商對 claude adapter 確認有效）。

**已知限制**（已知邊界 #1，非本階段遺漏）：這是 workdir 層級、`ensure_session()` 呼叫當下的
一次性檢查，不是 process-level/執行期攔截——session 建立後 opencode 透過工具呼叫自行
`cd`/`git checkout`/`git reset` 改變自身狀態或動到 workdir 之外的路徑，不在這一層防線範圍內。

### `close_session`

```python
def close_session(self, actor_id: str, session_name: str) -> None
```

關閉 session：呼叫 `acpx sessions close`，並清除該 session 專屬的權限設定檔
（`session_dir/{session_name}/` 子目錄，見〈已知限制〉N5 的解法）。

- **例外**：`AcpSessionError`——`session_name` 未知（從未 `ensure_session()` 過，或已經關過）。
- **Audit 記錄**：`action="acp.close_session"`，附加欄位 `session_name`。

### `get_history`

```python
def get_history(self, actor_id: str, session_name: str) -> list[AcpEvent]
```

讀取 session 的歷史事件（`acpx sessions read`）。

- **例外**：`AcpSessionError`——`session_name` 未知。
- **Audit 記錄**：`action="acp.get_history"`，附加欄位 `session_name`、`count`。

### `prompt`

```python
def prompt(
    self, actor_id: str, session_name: str, text: str,
    *, priority: int = 0, policy: AcpPolicy | None = None,
    timeout_sec: float = 600, on_event: Callable[[AcpEvent], None] | None = None,
) -> PromptResult
```

阻塞至 `stopReason`（或 `timeout_sec` 逾時）。

- **參數**：`priority`——仲裁優先權**預留欄位**（同 `herdr_bridge.send_to_agent` 的慣例），原樣
  寫入 audit，不排序不插隊。`policy`——**目前未使用**：policy 只能在 `ensure_session()` 建立時
  決定一次，這裡接受這個參數只是符合 §4.2 凍結簽名，傳了不會生效（不是假裝生效的 no-op，是
  誠實地不理會）。`on_event`——每收到一筆 `AcpEvent` 就呼叫一次，用於即時串流顯示。
- **回傳**：`PromptResult`。`reason` 是 bridge 自有凍結詞彙
  （`Literal["stop","timeout","error","canceled"]`），`stop_reason` 是協定詞彙透傳（str，不鎖
  Literal——見上方〈詞彙雙層〉的說明）。
- **例外**：`AcpSessionError`——`session_name` 未知。**逾時不拋例外**——回傳
  `PromptResult(reason="timeout", ...)`。
- **Audit 記錄**：`action="acp.prompt"`，附加欄位 `session_name`、`priority`、`chars`、`reason`、
  `stop_reason`。

### `exec_prompt`

```python
def exec_prompt(
    self, actor_id: str, agent: str, text: str,
    *, workdir: str, policy: AcpPolicy | None = None, timeout_sec: float = 600,
) -> PromptResult
```

無狀態一次性：內部生成一個一次性 `session_name`（`exec-<uuid4>`），跑完（不論成功/例外/逾時）
一律 `close_session`——不留下需要呼叫端自己收拾的 session。適合「單次任務，不需要多輪對話上下文」
的情境。

`exec_prompt()` 跟 `ensure_session()` 一樣直接建立 opencode session，同一套 ADR 0003 workdir
隔離檢查（見上方〈workdir 隔離（`agent="opencode"`）〉）在這裡同樣生效。

- **例外**：`AcpSessionError`——`agent="opencode"` 時 `workdir` 未通過 workdir/worktree 隔離檢查。
- **Audit 記錄**：`action="acp.exec_prompt"`，附加欄位 `agent`、`chars`、`reason`、`stop_reason`；
  隔離檢查拒絕時額外記 `session_name`（內部生成的一次性名稱）與 `rejected_reason`。

### `start_prompt` / `wait_done`

```python
def start_prompt(self, actor_id: str, session_name: str, text: str) -> PromptHandle
def wait_done(self, actor_id: str, handle: PromptHandle, *, timeout_sec: float = 60) -> PromptResult
```

非阻塞版本的 `prompt`：`start_prompt` 立即回傳一個 `PromptHandle`（底下是一個活的 acpx 子行程），
`wait_done` 才真正等待完成。**`wait_done` 絕不拋例外**——一切以 `PromptResult.reason` 表達
（呼應 `wait_until`/`WaitResult` 的既有哲學）；逾時可以再呼叫一次 `wait_done`（行程沒被殺掉，
只是這次沒等到）。

已知簡化：`PromptHandle` 底下的事件在 `wait_done()` 完成前**看不到任何內容**（一次性
`Popen.communicate()` 讀取，不是背景執行緒逐行累積）——中途想看即時串流請用
`prompt(on_event=...)`。

- **Audit 記錄**：`start_prompt` 記 `action="acp.start_prompt"`（`session_name`、`chars`）；
  `wait_done` 記 `action="acp.wait_done"`（`session_name`、`reason`、`stop_reason`）。

### `cancel`

```python
def cancel(self, actor_id: str, handle: PromptHandle) -> None
```

終止一個進行中的 `PromptHandle`。**已知簡化**：目前是直接對底下的 acpx 子行程送 SIGTERM
（逾時 5 秒再 SIGKILL），不是送 acpx 真正的 `session/cancel` 協定訊息（那需要另開一個具名
`acpx <agent> cancel` 呼叫，依賴尚未落地的具名 agent tier 定址，見〈已知限制〉）。粗暴但確定
有效；代價是拿不到 opencode 精確的 `stopReason="cancelled"` 回報（M0 spike 已知 `cancelled`
誤報為 `end_turn` 的協定缺陷）。

- **Audit 記錄**：`action="acp.cancel"`，附加欄位 `session_name`。

### `close`

```python
def close(self) -> None
```

關閉這個 `AcpActions` 實例目前記錄在案的**所有**活動 session（依序呼叫 `close_session` 的底層
邏輯）。行程結束前呼叫；不記 audit（沒有明確的單一 `actor_id`）。

## 資料模型

全部為 `@dataclass(frozen=True)`（`herdr_bridge.acp.models`）。

### `AcpAgentSpec`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `name` | `str` | agent tier 識別字（目前有 `"opencode"`、`"claude"`） |
| `command` | `str` | 唯讀說明性欄位（本階段固定為 resolver 描述字串，非可執行指令） |
| `builtin` | `bool` | `True`＝這個 adapter 已接好 resolver；`False`＝acpx 自己 config 裡的自訂條目（本階段皆空） |

### `AcpSessionInfo`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `session_name` | `str` | 呼叫端自訂的 display alias |
| `agent` | `str` | tier 識別字 |
| `workdir` | `str` | |
| `acp_session_id` | `str \| None` | 本階段固定 `None`（acpx `sessions ensure` 的文字輸出未穩定回傳 ID，未來若 acpx 提供 `--format json` 的 ensure 輸出可補上） |
| `closed` | `bool` | |

### `AcpEvent`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `type` | `str` | ACP 協定詞彙（`session/update` 的變體型別，如 `agent_message_chunk`/`tool_call`；或頂層 JSON-RPC method 名）；tolerant reader，未知值透傳、不拋例外 |
| `session_id` | `str \| None` | |
| `text` | `str \| None` | |
| `raw` | `dict[str, Any]` | 原始 JSON-RPC 訊息 |

### `PromptResult`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `reason` | `Literal["stop","timeout","error","canceled"]` | bridge 自有凍結詞彙 |
| `stop_reason` | `str \| None` | 協定詞彙透傳，不鎖 Literal |
| `text` | `str` | 串起所有 `agent_message_chunk` 的文字內容 |
| `session_name` | `str` | |
| `usage` | `dict[str, Any] \| None` | |
| `error` | `str \| None` | `reason` 為 `"error"`/`"timeout"` 時的描述字串 |

### `AcpPolicy`

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `mode` | `str` | `"approve-all"` | `"approve-all"`／`"approve-reads"`／`"deny-all"`；未知值在 `ensure_session()` 呼叫時拋 `AcpAdapterError`（fail closed） |
| `policy_enforced` | `bool \| None` | `None` | audit 記錄用欄位，非本類別自行推導 |

## 例外類別

```
AcpError                       所有 herdr_bridge.acp 例外的共同基底（獨立於 HerdrBridgeError）
├── AcpAdapterError            acpx 子行程非零 exit code；或 strict_version 檢查未過；或找不到已建置的平台二進位
├── AcpTransportError          NDJSON 讀取/解析層失敗（非「未知事件型別」——那由 tolerant reader 吸收）
├── AcpSessionError            session 定址失敗：未知 session_name、agent 不支援、ensure_session 失敗
├── AcpTimeoutError            （保留給未來需要拋例外而非回傳 timeout reason 的呼叫路徑）
└── AcpVersionError            acpx↔agent protocolVersion 握手不一致（M0-V9，本階段尚未實作對應檢查）
```

## Audit 事件一覽

| action | 附加欄位 | 觸發時機 |
|---|---|---|
| `acp.list_acp_agents` | `count` | 每次呼叫 |
| `acp.ensure_session` | `session_name`、`agent`、`policy_mode`\*、`idempotent_hit` | 每次呼叫（\*冪等命中時無此欄位） |
| `acp.close_session` | `session_name` | 關閉成功後 |
| `acp.get_history` | `session_name`、`count` | 每次呼叫 |
| `acp.prompt` | `session_name`、`priority`、`chars`、`reason`、`stop_reason` | 呼叫結束（含逾時/錯誤） |
| `acp.exec_prompt` | `agent`、`chars`、`reason`、`stop_reason` | 呼叫結束 |
| `acp.start_prompt` | `session_name`、`chars` | 送出後立即 |
| `acp.wait_done` | `session_name`、`reason`、`stop_reason` | 等待結束 |
| `acp.cancel` | `session_name` | 送出終止訊號後 |

同一支 JSONL（`AuditLogger`，`connect(audit_path=...)`），與 `herdr_bridge` 五函式的 audit 記錄
共用檔案、共用格式（`ts`/`actor_id`/`action` + 附加欄位），只是 `action` 都掛 `acp.` 前綴。

## pane↔session 綁定（`herdr_bridge.acp.binding`）

policy-neutral 純函式，供負責協調派工的一方自行決定何時用；`AcpActions` 不會自動呼叫這些函式。
canonical join key 是 herdr `pane_id` + 派工 ledger，**不是** `session_name`（`session_name`
只是無 schema 約束的字串橋，session 重建後舊 key 會靜默失聯）。

- `record_dispatch(ledger, *, pane_id, session_name, actor_id, dispatched_at) -> list[LedgerEntry]`
  ——append-only，回傳新 list。
- `current_binding_for_pane(ledger, pane_id) -> LedgerEntry | None`
- `current_binding_for_session(ledger, session_name) -> LedgerEntry | None`
- `detect_drift(ledger, *, actual_pane_session_map) -> list[str]`——對帳，回傳有落差的 pane_id
  清單。

## 已知限制（明確記錄，非阻塞）

- **支援 `agent="opencode"` 與 `"claude"`**：其餘 tier 待外部設具名 acpx agent 條目後才擴充
  （§4.5）。claude 走 acpx 具名子指令（`acpx claude`），全域 flag（`--cwd`/`--ttl`/policy
  flag）放在 `claude` 之前；不套用 opencode 專屬的 `OPENCODE_CONFIG` 等 env var。
- **`cancel()` 是砍子行程，不是協定層 `session/cancel`**：見上方 `cancel` 節。
- **`start_prompt`/`wait_done` 拿不到中途事件**：只有 `wait_done()` 完成後才能看到完整事件
  列表；要即時串流請用 `prompt(on_event=...)`。
- **`prompt()` 的 `policy` 參數不生效**：policy 只能在 `ensure_session()` 決定一次。
- **`strict_version` 不含 M0-V9 的 protocolVersion 握手驗證**：只檢查 N1 的
  `base_upstream_version` 範圍。

### 自行編譯修過的 opencode 執行檔(僅 `agent="opencode"` 這一層需要)

`agent="opencode"` 這一層依賴一支本機編譯的 opencode 執行檔,修補了一個真實存在的上游 bug
(child/subagent 的 ACP session 從未被註冊,導致任何需要為被委派的 subagent 自身動作要求授權的
prompt 都會卡住——見 [anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902),
上游 open 中,撰寫本文當下尚未合併)。在那個 PR 合併之前,`herdr_bridge.acp` 會到
`.vendor/opencode-patched/<target-triple>/opencode` 找這支執行檔(已加入 gitignore——需要你自己
編,不是版控產物,也不隨發布套件一起出貨)。

編譯步驟:

1. 在這個 repo 旁邊 clone 那個修過的 fork:`git clone https://github.com/aiken884/opencode ../opencode`
   (相對於你 checkout herdr-bridge 的位置)。
2. checkout 修復分支:`git -C ../opencode checkout fix/acp-child-session-permission-hang`。
3. 在這個 repo 根目錄執行 `bash scripts/rebuild-patched-opencode.sh`。它會用 `bun` 建置
   opencode、偵測你機器的 target triple、把執行檔複製到
   `.vendor/opencode-patched/<target-triple>/`,並寫入記錄 source commit 與建置時間的
   `MANIFEST.json`。
4. 如果想在 fork 的 base 版本偏離人工驗證過的相容範圍時明確拋出 `AcpAdapterError`(而不是悄悄
   繼續跑),`connect()` 時帶 `strict_version=True`。

需求:`git`、`bun`,以及類 Unix 作業系統(建置腳本是 bash、依賴 `uname`)。`agent="claude"`／
`"copilot"`／`"grok-build"` 這幾層不需要上述任何步驟——它們走 `acpx` 自己原生的子指令,不受影響。

另外提醒:`scripts/rebuild-patched-opencode.sh` 跟這份文件本身,目前都**不會**隨 PyPI 正式套件
出貨(依照 `pyproject.toml` 的 sdist 清單,目前只有 `docs/api.md` 會打包進去)——如果你手上只有
`pip install` 裝的版本,要拿到這支腳本跟這份參考文件,得去 clone GitHub repo。

### 若繞過本模組、直接 shell 出去呼叫原生 `opencode run` CLI 派工：exit code 不可靠（2026-07-24 實測）

本模組（`herdr_bridge.acp`）走 ACP 協定直接對話，不受這個問題影響；以下記錄的是**原生
`opencode run` 一次性 CLI**（非 ACP）派工時的陷阱，供任何會 shell out 呼叫 `opencode run` 的
呼叫端（包含未來可能出現的非 ACP dispatch wrapper）參考——這也是本模組選擇 ACP 而非直接解析
CLI 輸出的理由之一。

- **實測現象**：`opencode run "..." -m opencode-go/deepseek-v4-pro`（純文字模式，未加
  `--format json`），在 OpenCode Go 方案額度用盡時，畫面印出 `Error: Invalid API key.`，但
  **整個指令 exit code 是 0**。任何只靠 exit code 判斷派工成功/失敗的呼叫端都會誤判成功。
- **已排除的假設**：直接對 Anthropic API 用假 key 觸發 401（`invalid x-api-key`）時，exit code
  正確回傳 1（本機 dev branch 與全域 Homebrew v1.18.4 皆已實測驗證）。因此這不是
  `opencode run` 錯誤處理機制整體失效，較可能與 `opencode-go/*`（OpenCode Go 方案）額度用盡時
  的重試/降級路徑有關——尚未進一步根因，暫不回頭修 opencode 本身。
- **可靠偵測方式（已實測驗證）**：加 `--format json`，改讀取 NDJSON 事件流，檢查是否出現
  `"type":"error"` 的那一行：
  ```json
  {"type":"error","timestamp":...,"sessionID":"...","error":{"name":"APIError","data":{"message":"invalid x-api-key","statusCode":401,"isRetryable":false,...}}}
  ```
  只要看到 `"type":"error"`，就代表這次派工失敗，不管 exit code 是什麼；`error.data.message`/
  `statusCode`/`isRetryable` 可用來判斷是否值得重試。**任何未來要用原生 CLI（非 ACP）派工的
  呼叫端，一律加 `--format json` 並檢查這個欄位，不要只信 exit code。**

## 中央指揮塔 Facade（Option A：唯一指揮塔抽象，推薦外部使用）

為解決外部呼叫端需了解太多內部（`create_herdr_router`、`prepare`、worktree 等），新增高階 sync
facade `CentralTower`。

```python
from herdr_bridge import create_central_tower   # 或 from herdr_bridge.acp.router import ...

tower = create_central_tower(project="my-central-tower")
result = tower.dispatch("請研究最新法規變更", target=None)  # auto 依 caps 路由
print(result["routed_to"], result["task_id"], result["ok"])

results = tower.batch_dispatch([
    "echo hello",
    "research quantum",
    "implement helper func",
])
```

**API 重點**：
- `create_central_tower(project=..., additional_paths=...) -> CentralTower`
- `tower.dispatch(prompt: str, *, target: str|None = None) -> dict`（ok, routed_to, response,
  task_id, agent_id）
- `tower.batch_dispatch(prompts: list[str], *, target=None) -> list[dict]`
- `tower.register_agent(...)`、`tower.list_agents()`、`tower.get_registry_summary()`
- **所有路徑強制** Herdr Bridge Memory `prepare_dispatch_text`（recall+augment）+ `store_memory`
- 隱藏所有內部：router 細節、Herdr Bridge Memory 呼叫、ACP spawn、worktree、asyncio

也保留低階 `create_herdr_router()` / `AcpRouter` 給需要細控者。

更新範例見 `examples/central-tower-minimal.py` 與 cross-project 範例。更新 docs 與測試已對齊
「唯一指揮塔 plug-in」目標。
