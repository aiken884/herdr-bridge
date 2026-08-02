# API Reference

This document is the authoritative reference for the public surface of herdr-bridge — function signatures, parameters, exceptions, audit log fields, and the semantics of the reserved fields that a future policy/governance layer may depend on. For a plain-language overview, installation instructions, and a quickstart, see the root [`README.md`](../README.md).

All public symbols can be imported from the top level of the package: `from herdr_bridge import connect, AgentInfo, AgentOutput, SendResult, WaitResult, ...` (see `herdr_bridge.__all__` for the complete list).

## `connect()`

```python
def connect(
    socket_path: str | None = None,
    *,
    audit_path: str | Path | None = None,
    herdr_bin: str = "herdr",
) -> BridgeActions
```

Builds a ready-to-use `BridgeActions`: resolves the socket path, constructs a `SocketClient`, performs a one-time protocol compatibility check, starts a `SessionCache` (including the initial snapshot and subscription), and finally wraps everything in an `AuditLogger`. This is the only recommended way to obtain a `BridgeActions` instance.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `socket_path` | `str \| None` | `None` | The herdr socket path. When `None`, resolution is attempted in order: the `HERDR_SOCKET_PATH` environment variable → the `socket:` line parsed from the output of `herdr status` |
| `audit_path` | `str \| Path \| None` | `None` | Path to the audit log file. When `None`, defaults to `$XDG_STATE_HOME/herdr-bridge/audit.jsonl` (falls back to `~/.local/state/...` when `XDG_STATE_HOME` is unset) |
| `herdr_bin` | `str` | `"herdr"` | Name/path of the herdr CLI executable to invoke, used for socket detection and the protocol check |

**Returns**: A `BridgeActions` instance with the session cache already started, ready to call the five public functions below.

**Raises**:
- `HerdrConnectionError` — the socket path could not be found, or the connection failed (the single retry performed during the connect phase was exhausted).
- `SchemaVersionError` — the server protocol is below `MIN_SUPPORTED_PROTOCOL` (16). **Note**: a protocol above the tested upper bound does not raise — it only logs a warning and proceeds with `protocol_compat="untested"`; see Compatibility.

```python
from herdr_bridge import connect

actions = connect()  # equivalent to connect(socket_path=None, audit_path=None, herdr_bin="herdr")
```

## `BridgeActions` Methods

The following functions are all methods of `BridgeActions`. Every one of them takes `actor_id: str` as its first parameter — a caller-identity marker whose semantics are covered in the Reserved Fields section below; the tool layer never validates it and never blocks a call because of it.

### `list_agents`

```python
def list_agents(self, actor_id: str) -> list[AgentInfo]
```

Lists every agent currently known to the session cache.

- **Parameters**: `actor_id` — caller identity (recorded only).
- **Returns**: `list[AgentInfo]`, sorted by the `agent_id` string.
- **Raises**: None (an empty cache returns an empty list; this is not treated as an error).
- **Audit record**: `action="list_agents"`, with an extra `count=<number of results>` field.

```python
agents = actions.list_agents("rule:dashboard")
for a in agents:
    print(a.agent_id, a.status)
```

### `read_agent`

```python
def read_agent(
    self, actor_id: str, agent_id: str,
    mode: str = "recent-unwrapped", *,
    since_revision: int | None = None,
) -> AgentOutput
```

Reads the current output text of the specified agent (or its pane).

| Parameter | Type | Default | Description |
|---|---|---|---|
| `actor_id` | `str` | — | Caller identity |
| `agent_id` | `str` | — | Accepts either `AgentInfo.agent_id` (`terminal_id`) **or** `pane_id` — both resolve to the same agent |
| `mode` | `str` | `"recent-unwrapped"` | Read mode, mapped to the underlying socket's `source`/`format`/`strip_ansi` parameters; see the table below |
| `since_revision` | `int \| None` | `None` | **0.2.2 experimental**: returns only the output changes after this revision (natively supported by the herdr protocol); when `None`, returns the full output (same behavior as 0.2.1) |

Valid values for `mode`:

| `mode` | socket `source` | `format` | `strip_ansi` |
|---|---|---|---|
| `recent-unwrapped` (default) | `recent_unwrapped` | `text` | `True` |
| `recent_unwrapped` | `recent_unwrapped` | `text` | `True` |
| `raw-ansi` | `recent` | `ansi` | `False` |
| `visible` | `visible` | `text` | `True` |
| `recent` | `recent` | `text` | `True` |
| `detection` | `detection` | `text` | `True` |

- **Returns**: `AgentOutput` — `agent_id` / `text` / `source` (the underlying socket source actually used) / `status_at_read` (the `AgentInfo.status` in the cache at the moment of the read; **not** a live query against herdr).
- **Raises**:
  - `AgentNotFoundError` — `agent_id` could not be resolved (on a cache miss, one `session.snapshot` refresh is attempted before giving up).
  - `ValueError` — `mode` is not one of the values listed above.
- **Audit record**: `action="read_agent"`, with extra fields `agent_id`, `mode`, `chars=<len(text)>`. **Note**: the full output text is never persisted to the audit log — only its character count is recorded.

```python
out = actions.read_agent("rule:dashboard", "term_abc123")
print(out.text[-2000:])

out_raw = actions.read_agent("rule:dashboard", "term_abc123", mode="raw-ansi")
```

### `send_to_agent`

```python
def send_to_agent(
    self, actor_id: str, agent_id: str, text: str,
    priority: int = 0,
) -> SendResult
```

Sends text to the specified agent's input (equivalent to typing into that pane and submitting it — no trailing newline is appended automatically; include one in `text` if needed).

- **Parameters**: `actor_id`, `agent_id` (same as `read_agent`), `text` (the raw text to send), `priority` (a **reserved** arbitration-priority field; semantics covered in Reserved Fields — the tool layer records it verbatim and never reorders or preempts based on it).
- **Returns**: `SendResult` — `ok` / `agent_id` / `actor_id` / `priority` (echoed back exactly as passed in) / `sent_at` (a UTC-aware `datetime`).
- **Raises**: `AgentNotFoundError` — `agent_id` could not be resolved.
- **Audit record**: `action="send_to_agent"`, with extra fields `agent_id`, `priority`, `chars=<len(text)>` (again, only the character count is recorded, never the text itself).

```python
result = actions.send_to_agent("rule:dashboard", "term_abc123", "run tests", priority=3)
assert result.ok
```

### `get_agent_status`

```python
def get_agent_status(self, actor_id: str, agent_id: str) -> AgentStatus
```

Queries the specified agent's current Herdr status value, without applying any semantic interpretation to it.

- **Parameters**: `actor_id` — caller identity (recorded only); `agent_id` — same as `read_agent`.
- **Returns**: `AgentStatus` — `Literal["idle", "working", "blocked", "done", "unknown"]`. **Note**: `idle` does not guarantee the agent has actually "finished" — Claude Code's confirmation prompts also report as `idle` (see the README's Status semantics caveat). `blocked` means Herdr has detected the agent waiting on external input (e.g. an approval dialog). This function faithfully reports Herdr's native value only — whether it warrants intervention is left to the caller to decide.
- **Raises**: `AgentNotFoundError` — `agent_id` could not be resolved (including after a snapshot refresh).
- **Audit record**: `action="get_agent_status"`, with extra fields `agent_id`, `status`.

```python
from herdr_bridge import AgentStatus

status = actions.get_agent_status("rule:dashboard", "term_abc123")
if status == "blocked":
    print("agent is waiting for approval")
```

> **Added in 0.1.2** (additive; the five v0.1.0 function signatures are unchanged). `wait_until`'s early-exit-on-`blocked` mechanism is independent of this function — `wait_until` never calls `get_agent_status` internally; each reads `AgentInfo.status` from the cache on its own.

### `wait_until`

```python
def wait_until(
    self, actor_id: str, agent_id: str,
    predicate: Callable[[AgentOutput], bool],
    timeout_sec: int = 60,
    poll_interval_sec: int = 2, *,
    since_revision: int | None = None,
) -> WaitResult
```

Repeatedly re-reads the agent's output and applies `predicate` until it returns true, the call times out, the agent disappears, or the agent enters the `blocked` state; a triple-confirmation design (event-driven poll cadence + actively re-reading output + `predicate` evaluating the content + early exit on `blocked` + a timeout as the final backstop), whose rationale is covered in Precise Semantics and the README's Status semantics caveat.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `predicate` | `Callable[[AgentOutput], bool]` | — | Applied after each re-read; any exception raised by `predicate` itself is swallowed and turned into `reason="error"` (it will never crash `wait_until`) |
| `timeout_sec` | `int` | `60` | **A total deadline measured from the moment of the call**, not an idle timeout — see Precise Semantics |
| `poll_interval_sec` | `int` | `2` | Upper bound on the wait between each round; the effective floor is 0.1 seconds (smaller values are clamped up to 0.1), and it never exceeds the remaining time budget |
| `since_revision` | `int \| None` | `None` | **0.2.2 experimental**: returns only the output changes after this revision; when `None`, behaves the same as 0.2.1 (passed through to `read_agent` on every round) |

- **Returns**: `WaitResult` (the spec guarantees this function **never raises**; see Precise Semantics).
- **Raises**: None — an agent disappearing, `predicate` raising, or any underlying exception is caught and converted into `WaitResult(success=False, reason=..., error=...)`.
- **Implementation detail (for debugging)**: internally, `wait_until` reads via `self.read_agent(actor_id, info.agent_id)`, **always with the default `mode="recent-unwrapped"`** — custom modes are not supported. Every poll round issues its own `read_agent` call, so besides the single summary record written when `wait_until` finishes, the audit log will also contain one `read_agent` record per poll round.
- **Audit record**: one `action="wait_until"` record is written when the call finishes, with extra fields `agent_id`, `success`, `reason`, `elapsed_sec` (rounded to 3 decimal places) — each `wait_until()` call produces exactly this one summary record (not counting the per-round `read_agent` records mentioned above).

```python
result = actions.wait_until(
    "rule:ci-watcher", "term_abc123",
    predicate=lambda out: "PASSED" in out.text or "FAILED" in out.text,
    timeout_sec=300, poll_interval_sec=2,
)
if result.success:
    print(result.last_output.text)
else:
    print("gave up:", result.reason, result.error)
```

### `acquire_control`

```python
def acquire_control(
    self, actor_id: str, pane_id: str,
    mode: str = "control",
) -> ControlHandle
```

Acquires (or shares) a bridge-level lease on a pane; see the Reserved Fields section for `mode`/lease semantics.

- **Parameters**: `pane_id` (`AgentInfo.pane_id`, e.g. `"w1:p1"`), `mode` (`"observe"` = read-only/shared, `"control"` = exclusive; defaults to `"control"`).
- **Lease key normalization**: if an agent_id (terminal_id) is passed by mistake, it is always resolved to the canonical `pane_id` internally before the lease is registered — the same physical pane cannot bypass exclusivity just because a different identifier form was used (M1 gate fix X2).
- **Returns**: `ControlHandle` (see the section below).
- **Raises**:
  - `ValueError` — `mode` is neither `"observe"` nor `"control"`.
  - `AgentNotFoundError` (`code="pane_not_found"`) — `pane_id` does not exist (on a cache miss, one snapshot refresh is attempted before giving up).
  - `ControlLeaseError` — `mode="control"` and the pane is already held by another lease that has not been released.
- **Audit record**: acquisition records `action="acquire_control"` (`pane_id`, `mode`); release records `action="release_control"` (`pane_id`, `mode`); **a denied exclusivity conflict records `action="acquire_control_denied"`** (`pane_id`, `mode`, `held_by=<holder's actor_id>`) — a rejected takeover attempt is a core audit event for a governance-oriented consumer (M1 gate fix X3). `release()` is idempotent: concurrent duplicate releases produce only a single release record.

```python
with actions.acquire_control("human:aiken", "w1:p1", mode="control") as handle:
    actions.send_to_agent("human:aiken", "term_abc123", "do not touch, I'm driving")
# leaving the with block automatically calls handle.release()
```

## `ControlHandle`

The return type of `acquire_control()`, representing a lease.

| Attribute/Method | Type | Description |
|---|---|---|
| `pane_id` | `str` | The pane this lease applies to |
| `actor_id` | `str` | Identity of the holder |
| `mode` | `str` | `"observe"` or `"control"` |
| `released` | `bool` | Whether it has already been released |
| `release()` | `-> None` | Releases the lease; **idempotent** (calling it again does not raise — the second call is a no-op) |
| `__enter__`/`__exit__` | — | Context manager support; `release()` is called automatically on exiting the `with` block |

## Exception Classes

```
HerdrBridgeError                    Common base class for every herdr-bridge exception
├── HerdrConnectionError            Could not connect to the socket, or the connection was interrupted / closed by the server
├── HerdrTimeoutError                A request did not get a response within the timeout
├── HerdrApiError(code, message)    herdr returned an {"error": {"code", "message"}} envelope
│   └── AgentNotFoundError          code is one of pane_not_found / agent_not_found / terminal_not_found
├── SchemaVersionError              Server protocol is below this library's minimum supported version
└── ControlLeaseError               acquire_control conflict (the pane is held by an exclusive lease)
```

`HerdrApiError` (including its subclass `AgentNotFoundError`) carries two public attributes: `code: str` (the error code returned by herdr, preserved verbatim) and `message: str`.

## Data Models

All are `@dataclass(frozen=True)` — immutable once constructed, safe to pass across layers.

### `AgentInfo`

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | herdr's `terminal_id`; can be used directly as the `target` for `agent.*` methods |
| `brand` | `str` | herdr's `agent` field (e.g. `"claude"`, `"codex"`) |
| `status` | `AgentStatus` | `Literal["idle", "working", "blocked", "done", "unknown"]` |
| `pane_id` | `str` | e.g. `"w1:p1"` |
| `workspace_id` | `str` | |
| `tab_id` | `str` | |
| `cwd` | `str \| None` | |
| `session_ref` | `dict[str, Any] \| None` | Preserved verbatim from herdr's `agent_session` (e.g. Claude Code's native session UUID); the fallback identifier to use when `agent_id` becomes invalid across a server restart |
| `focused` | `bool` | |

### `AgentOutput`

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | |
| `text` | `str` | |
| `source` | `str` | The socket read source actually used (underlying format); see the `mode` table under `read_agent` |
| `status_at_read` | `AgentStatus` | The cached status at the moment of the read, not a live query |
| `revision` | `int \| None` | **0.2.2 experimental**: the monotonic revision counter from the herdr response; defaults to `None` |
| `normalized_text` | `str` (property, 0.1.1) | `text` with hard PTY line-wraps merged (orphaned newlines removed, blank-line paragraph breaks preserved); a narrow pane can hard-wrap a marker across two lines, so use this field for marker/long-string matching. The semantics of `text` remain frozen and unchanged |

### `SendResult`

| Field | Type | Description |
|---|---|---|
| `ok` | `bool` | |
| `agent_id` | `str` | |
| `actor_id` | `str` | |
| `priority` | `int` | Echoed back exactly as passed in at call time |
| `sent_at` | `datetime` | UTC-aware |

### `WaitResult`

| Field | Type | Description |
|---|---|---|
| `success` | `bool` | |
| `agent_id` | `str` | |
| `reason` | `WaitReason` | `Literal["predicate", "timeout", "agent_gone", "error", "blocked"]` — a stable API, see Precise Semantics |
| `elapsed_sec` | `float` | |
| `last_output` | `AgentOutput \| None` | The last successfully read output; may still be non-`None` on timeout/error |
| `error` | `str \| None` | The exception description string when `reason="error"` (`"{type name}: {message}"`); `None` in all other cases |

## Reserved Fields (Forward Design for a Future Policy Layer)

- **`actor_id`** (all functions): a caller-identity marker, conventionally `<category>:<name>` (four categories — `human` / `rule` / `agent` / `system` — all lowercase, exactly one colon, total length ≤64 characters; `system:` is reserved internally for the bridge and external callers must not use it). The tool layer **never validates it and never blocks the call itself** because of it — it is simply written to the audit log verbatim, with an added `actor_id_status` classification field (`empty` / `malformed` / `reserved_violation`; omitted when the format is valid, which is equivalent to `valid`). Detailed rules are defined in the governance conventions memo v1.0, rule 1.
- **`priority`** (`send_to_agent`): a reserved arbitration-priority field, where **a larger number means higher priority**; the valid range is **−5 to 9**, with five named semantic anchors: `BACKGROUND=-1`, `NORMAL=0` (default), `AUTOMATION=5`, `HUMAN_ELEVATED=7`, `HUMAN_URGENT=9`. The tool layer **never reorders and never preempts** based on this value — it is recorded verbatim; rejecting and warning on out-of-range values is the responsibility of a future policy layer, and the tool layer currently records such values as-is without validating them. Detailed rules are defined in the governance conventions memo v1.0, rule 2.
- **`mode`** (`acquire_control`): `observe` = read-only/shared; `control` = an exclusive lease **held within the bridge process**. This lease is **not** a server-side lock on the herdr server — herdr 0.7.4 has no corresponding server-side locking API yet (`pane.clear_agent_authority` / `pane.release_agent` carry integration-reporting semantics, not locking semantics). Should herdr add a server-side lock in the future, the underlying implementation will be swapped out while this function's interface stays the same.

## Precise Semantics [PPLX R-18]

- `wait_until`'s `timeout_sec` is **a total deadline measured from the moment the function is called**, not an "idle timeout since the last state change." For long-running tasks (e.g. Claude Code performing a large refactor), scale this value up accordingly.
- `wait_until` returning instead of raising is a spec guarantee; the five `reason` values (`predicate` / `timeout` / `agent_gone` / `error` / `blocked`) form a stable API — changing them requires a major version bump. `blocked` was added additively in 0.1.2: when the agent enters Herdr's `blocked` state (waiting on an approval / on input) and the predicate has not matched, `wait_until` exits early instead of waiting out the full `timeout_sec` — it detects this via the `status_at_read` returned by `read_agent`, independently of `get_agent_status()`.
- State consistency: `AgentInfo.status` is an **eventually consistent** view; intermediate state transitions during a subscription rebuild may not be observed, and long-term drift is corrected by a full `session.snapshot` reconciliation every 5 minutes — this is an **upper bound**, not a typical delay.
- `agent_id` lifetime: valid only **for the lifetime of a single herdr server process**; across a server restart, re-map identity via `AgentInfo.session_ref`.
- **Known limitations (outside the coverage of the triple-confirmation mechanism, PPLX F-05)**:
  - **Idempotency of post-timeout retries is not protected**: terminal commands are not idempotent, so after `wait_until` times out (`reason="timeout"`), the caller **must not automatically resend** a previously sent command; consistent with the governance conventions memo v1.0, rule 3 ("no automatic resending on write failure"), a resend must only be initiated after an explicit decision by a human or a policy layer.
  - **No heartbeat detection**: there is currently no active health check for "the agent is unresponsive / stuck with no output change." `wait_until` relies purely on the `timeout_sec` backstop to infer an anomaly — it performs no heartbeat probing. Heartbeat detection and the resulting degradation chain (extend retries → flag for human review → circuit break) belong to a policy layer's degradation responsibilities, and are outside the scope of the tool layer.


## Audit Event Overview (Reference for a Governance-Oriented Consumer)

| action | extra fields | trigger |
|---|---|---|
| `list_agents` | `count` | Every call |
| `read_agent` | `agent_id`, `mode`, `chars` | Every call (the full output text is never persisted) |
| `send_to_agent` | `agent_id`, `priority`, `chars` | After a successful send |
| `get_agent_status` | `agent_id`, `status` | Every call |
| `wait_until` | `agent_id`, `success`, `reason`, `elapsed_sec` | When waiting ends (recorded for all five `reason` values) |
| `acquire_control` | `pane_id`, `mode` | Lease successfully acquired |
| `acquire_control_denied` | `pane_id`, `mode`, `held_by` | Exclusivity conflict on `control` was denied |
| `release_control` | `pane_id`, `mode` | Lease released (idempotent, only one record) |

Common fields on every record: `ts` (ISO8601 UTC), `actor_id`, `action`; `actor_id_status` is additionally attached when the `actor_id` format is invalid. File format: JSONL, mode 0600, defaulting to `~/.local/state/herdr-bridge/audit.jsonl`.

---

## 0.1.1 Additions to the Public Surface (additive; all v0.1.0 signatures unchanged)

Fixes for tool-layer friction points uncovered through real-world usage across downstream projects (PPLX consensus) — all additive, none change existing semantics:

### `get_audit_log_path() -> pathlib.Path`

A public, read-only query for the default audit log path (importable from the top level of the package). Pure query, no side effects (it does not create directories or change permissions). Consumers (e.g. a downstream audit-viewing tool) should always use this function to obtain the path — **do not** reach into `AuditLogger` internals; its internal layout is private and is not guaranteed to survive the 0.2 refactor.

### `AgentOutput.normalized_text`

See the `AgentOutput` data model table above. Marker matching should always use `out.normalized_text` (noted in the `wait_until` docstring as well).

### `BridgeActions.resolved_socket_path` / `BridgeActions.socket_source`

F-2, an observable connection target (automated callers can assert against it to guard against connecting to the wrong session):

- `resolved_socket_path: str` — the herdr socket path actually connected to (read-only property).
- `socket_source: str` — where the path came from: `'explicit'` (passed explicitly) | `'env'` (`HERDR_SOCKET_PATH`) | `'detected'` (detected via `herdr status`) | `'unknown'` (constructed without the `connect()` factory, source unknowable). Automated scenarios should use `'explicit'`.

### `SocketClient.subscribe()`'s new `on_state` value `"degraded"`

When the subscription reader's consecutive reconnect failures reach a threshold (by default, 10 consecutive failures **or** 60 seconds of continuous failure; adjustable via the new keyword-only parameters `degraded_after_failures` / `degraded_after_sec`), it reports `"degraded"` **exactly once** through the existing `on_state` callback and logs a warning. **Reconnection does not stop** — `degraded` is informational (letting a long-lived consumer distinguish "a temporary restart" from "possibly gone for good"), not a termination signal; the flag resets after a successful reconnect and can fire again. The semantics of the existing values (`connected` / `reconnected` / `disconnected`) are unchanged.

---

## 0.1.2 Additions to the Public Surface (additive; all v0.1.0/v0.1.1 signatures unchanged, 2026-07)

### `get_agent_status(actor_id, agent_id) -> AgentStatus`

The sixth public method (a `BridgeActions` method). Queries the agent's current Herdr status (`idle` / `working` / `blocked` / `done` / `unknown`) without applying any semantic interpretation to it — its purpose is to let a caller obtain Herdr's native status, whether by polling or in an event-driven fashion, and decide for itself whether a `blocked` state needs intervention. See the `get_agent_status` section above for the full signature and usage.

### `wait_until`'s early exit on `blocked`

After every round of `read_agent`, `wait_until` checks whether `status_at_read == "blocked"`: when Herdr judges the agent to be waiting on external input (e.g. an approval dialog) and `predicate` has not matched, it immediately exits with `WaitResult(success=False, reason="blocked")` instead of waiting out the full `timeout_sec` — an out-of-the-box way to avoid sitting idle on a stuck agent.

The semantics of the existing four `reason` values (`predicate` / `timeout` / `agent_gone` / `error`) are unchanged; `blocked` is an additive fifth value. The `WaitReason` type has been updated to `Literal["predicate", "timeout", "agent_gone", "error", "blocked"]`.

This mechanism is independent of `get_agent_status()` — `wait_until`'s internal `blocked` detection goes through `read_agent`'s `status_at_read` and never calls `get_agent_status` separately; each reads `AgentInfo.status` from the cache on its own.

---

## 0.2.2 Additions to the Public Surface (additive; all v0.2.1 signatures unchanged, 2026-07)

WP4: the D6 revision cursor — lets a consumer do incremental reads keyed on a monotonic revision counter (avoiding repeatedly pulling the full output).

### `AgentOutput.revision`

`AgentOutput` gains a `revision: int | None` field (defaulting to `None`). Existing calls that don't pass this field behave the same as in 0.2.1.

### `read_agent(since_revision=...)` / `wait_until(since_revision=...)`

`read_agent` and `wait_until` gain a keyword-only parameter `since_revision: int | None = None`. When a non-`None` value is passed, herdr returns only the output changes after that revision (this maps to the `since_revision` parameter native to the herdr protocol's `agent.read`). When omitted, behavior matches 0.2.1 (full output).

`wait_until`'s `since_revision` is forwarded to its internal `read_agent` call on every poll round.

### `_RevisionAdapter` (experimental)

An internal helper that normalizes the revision value from a herdr response to `int | None`: only accepts `int` (excluding `bool` — in Python, `bool` is a subclass of `int`, but revision semantics are not boolean); every other type (including `None`, `float`, `str`) is downgraded to `None`.

This is an experimental feature (0.2.2); its interface may change as the herdr protocol evolves. Consumers should not depend on this function directly.

### `@pytest.mark.empirical`

The four hardware-in-the-loop semantics (monotonic, stable, since-filtering, session-reset) are marked with `@pytest.mark.empirical` and deselected in CI — they require a real herdr server environment to run.

---

# API 參考

本文件是 herdr-bridge 公開介面的權威版本——函式簽名、參數、例外、audit 記錄欄位，以及未來可能建立在這個套件之上的政策層將依賴的預留欄位語意。白話總覽、安裝與 Quickstart 請見根目錄 [`README.md`](../README.md)。

全部公開符號皆可從套件頂層匯入：`from herdr_bridge import connect, AgentInfo, AgentOutput, SendResult, WaitResult, ...`（完整清單見 `herdr_bridge.__all__`）。

## `connect()`

```python
def connect(
    socket_path: str | None = None,
    *,
    audit_path: str | Path | None = None,
    herdr_bin: str = "herdr",
) -> BridgeActions
```

建立一個可用的 `BridgeActions`：偵測 socket 路徑、建立 `SocketClient`、執行一次 protocol 相容性檢查、啟動 `SessionCache`（含初始 snapshot 與訂閱），最後包上 `AuditLogger`。是取得 `BridgeActions` 的唯一建議路徑。

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `socket_path` | `str \| None` | `None` | 指定 herdr socket 路徑；`None` 時依序解析：`HERDR_SOCKET_PATH` 環境變數 → 執行 `herdr status` 解析輸出中的 `socket:` 行 |
| `audit_path` | `str \| Path \| None` | `None` | audit log 檔案路徑；`None` 時預設 `$XDG_STATE_HOME/herdr-bridge/audit.jsonl`（`XDG_STATE_HOME` 未設定時為 `~/.local/state/...`） |
| `herdr_bin` | `str` | `"herdr"` | 呼叫的 herdr CLI 執行檔名稱／路徑，供 socket 偵測與 protocol 檢查使用 |

**回傳**：`BridgeActions` 實例，session cache 已啟動，可直接呼叫下方五個公開函式。

**例外**：
- `HerdrConnectionError`——找不到 socket 路徑，或連線失敗（connect 階段的單次重試已用盡）。
- `SchemaVersionError`——server protocol 低於 `MIN_SUPPORTED_PROTOCOL`（16）。**注意**：protocol 高於已測試上限不會拋例外，只記 warning 並以 `protocol_compat="untested"` 放行，見〈Compatibility〉。

```python
from herdr_bridge import connect

actions = connect()  # 等同 connect(socket_path=None, audit_path=None, herdr_bin="herdr")
```

## `BridgeActions` 逐函式

以下五個函式皆為 `BridgeActions` 的方法。所有函式的第一個參數皆為 `actor_id: str`——呼叫者身份標記，語意見〈預留欄位〉節；工具層一律不驗證、不阻擋呼叫本身。

### `list_agents`

```python
def list_agents(self, actor_id: str) -> list[AgentInfo]
```

列出目前 session cache 已知的所有 agent。

- **參數**：`actor_id`——呼叫者身份（僅記錄）。
- **回傳**：`list[AgentInfo]`，依 `agent_id` 字串排序。
- **例外**：無（cache 目前為空時回傳空列表，不視為錯誤）。
- **Audit 記錄**：`action="list_agents"`，附加欄位 `count=<結果筆數>`。

```python
agents = actions.list_agents("rule:dashboard")
for a in agents:
    print(a.agent_id, a.status)
```

### `read_agent`

```python
def read_agent(
    self, actor_id: str, agent_id: str,
    mode: str = "recent-unwrapped", *,
    since_revision: int | None = None,
) -> AgentOutput
```

讀取指定 agent（或其 pane）目前的輸出文字。

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `actor_id` | `str` | — | 呼叫者身份 |
| `agent_id` | `str` | — | 接受 `AgentInfo.agent_id`（`terminal_id`）**或** `pane_id`；兩者皆可解析到同一 agent |
| `mode` | `str` | `"recent-unwrapped"` | 讀取模式，對應底層 socket 的 `source`/`format`/`strip_ansi` 參數，見下表 |
| `since_revision` | `int \| None` | `None` | **0.2.2 experimental**：僅回傳該 revision 之後的輸出變更（herdr 協定原生支援）；`None` 時回傳完整輸出（與 0.2.1 行為相同） |

`mode` 合法值：

| `mode` | socket `source` | `format` | `strip_ansi` |
|---|---|---|---|
| `recent-unwrapped`（預設） | `recent_unwrapped` | `text` | `True` |
| `recent_unwrapped` | `recent_unwrapped` | `text` | `True` |
| `raw-ansi` | `recent` | `ansi` | `False` |
| `visible` | `visible` | `text` | `True` |
| `recent` | `recent` | `text` | `True` |
| `detection` | `detection` | `text` | `True` |

- **回傳**：`AgentOutput`——`agent_id`／`text`／`source`（實際使用的底線格式 socket source）／`status_at_read`（讀取當下 cache 內的 `AgentInfo.status`，**非**即時查詢 herdr）。
- **例外**：
  - `AgentNotFoundError`——`agent_id` 無法解析（cache miss 時會先補一次 `session.snapshot` 再判死）。
  - `ValueError`——`mode` 不在上表範圍內。
- **Audit 記錄**：`action="read_agent"`，附加欄位 `agent_id`、`mode`、`chars=<len(text)>`。**注意**：輸出全文不落地 audit log，只記字數。

```python
out = actions.read_agent("rule:dashboard", "term_abc123")
print(out.text[-2000:])

out_raw = actions.read_agent("rule:dashboard", "term_abc123", mode="raw-ansi")
```

### `send_to_agent`

```python
def send_to_agent(
    self, actor_id: str, agent_id: str, text: str,
    priority: int = 0,
) -> SendResult
```

把文字送進指定 agent 的輸入（等同在該 pane 打字並送出，末尾不自動附加換行——`text` 內容需自行包含）。

- **參數**：`actor_id`、`agent_id`（同 `read_agent`）、`text`（送出的原始文字）、`priority`（仲裁優先權**預留欄位**，語意見〈預留欄位〉節；工具層原樣記錄、不排序不插隊）。
- **回傳**：`SendResult`——`ok`／`agent_id`／`actor_id`／`priority`（原樣回傳呼叫時傳入的值）／`sent_at`（UTC-aware `datetime`）。
- **例外**：`AgentNotFoundError`——`agent_id` 無法解析。
- **Audit 記錄**：`action="send_to_agent"`，附加欄位 `agent_id`、`priority`、`chars=<len(text)>`（同樣只記字數，不記文字本文）。

```python
result = actions.send_to_agent("rule:dashboard", "term_abc123", "run tests", priority=3)
assert result.ok
```

### `get_agent_status`

```python
def get_agent_status(self, actor_id: str, agent_id: str) -> AgentStatus
```

查詢指定 agent 目前的 Herdr 狀態值，不對狀態做任何語意判斷。

- **參數**：`actor_id`——呼叫者身份（僅記錄）；`agent_id`——同 `read_agent`。
- **回傳**：`AgentStatus`——`Literal["idle", "working", "blocked", "done", "unknown"]`。**注意**：`idle` 不保證 agent 已「完成」；Claude Code 的等待確認提示也會回報 `idle`（見 README〈Status semantics caveat〉）。`blocked` 表示 Herdr 偵測到 agent 正在等待外部輸入（如審批對話框）。本函式僅忠實回報 Herdr 原生值——是否需介入處理由呼叫端自行判斷。
- **例外**：`AgentNotFoundError`——`agent_id` 無法解析（含補快照後仍不存在）。
- **Audit 記錄**：`action="get_agent_status"`，附加欄位 `agent_id`、`status`。

```python
from herdr_bridge import AgentStatus

status = actions.get_agent_status("rule:dashboard", "term_abc123")
if status == "blocked":
    print("agent is waiting for approval")
```

> **0.1.2 新增**（additive；v0.1.0 五函式簽名不動）。`wait_until` 的 `blocked` 提前退出機制獨立於本函式——`wait_until` 不會自動呼叫 `get_agent_status`，兩者各自讀取 cache 中的 `AgentInfo.status`。

### `wait_until`

```python
def wait_until(
    self, actor_id: str, agent_id: str,
    predicate: Callable[[AgentOutput], bool],
    timeout_sec: int = 60,
    poll_interval_sec: int = 2, *,
    since_revision: int | None = None,
) -> WaitResult
```

反覆讀取 agent 輸出並套用 `predicate`，直到成立、逾時、agent 消失、或 agent 進入 `blocked` 狀態為止；三重確認機制（事件觸發輪詢節拍 + 主動重讀輸出 + `predicate` 判斷內容 + `blocked` 提前退出 + 逾時兜底），設計依據見〈語意精確定義〉與 README 的〈Status semantics caveat〉。

| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `predicate` | `Callable[[AgentOutput], bool]` | — | 每次重新讀取後套用；`predicate` 本身拋出的例外會被吞下、轉為 `reason="error"`（不會讓 `wait_until` 崩潰） |
| `timeout_sec` | `int` | `60` | **自呼叫起算的總時限**，非閒置時限，見〈語意精確定義〉 |
| `poll_interval_sec` | `int` | `2` | 每輪等待節拍上限；實際下限為 0.1 秒（傳入更小的值一律夾到 0.1），且不會超過剩餘時限 |
| `since_revision` | `int \| None` | `None` | **0.2.2 experimental**：僅回傳該 revision 之後的輸出變更；`None` 時行為與 0.2.1 相同（每輪 `read_agent` 皆傳遞此值） |

- **回傳**：`WaitResult`（規格保證**不拋出例外**，見〈語意精確定義〉）。
- **例外**：無——agent 消失、`predicate` 拋錯、或底層任何例外，都會被捕捉並轉換為 `WaitResult(success=False, reason=..., error=...)`。
- **實作細節（供除錯用）**：`wait_until` 內部透過 `self.read_agent(actor_id, info.agent_id)` 讀取，**固定使用預設 `mode="recent-unwrapped"`**，不支援自訂 mode；且每一輪 poll 都會各自呼叫一次 `read_agent`，因此除了 `wait_until` 結束時記的那一筆摘要外，audit log 中還會看到對應輪數的 `read_agent` 記錄。
- **Audit 記錄**：呼叫結束時記一筆 `action="wait_until"`，附加欄位 `agent_id`、`success`、`reason`、`elapsed_sec`（四捨五入至 3 位小數）——每次 `wait_until()` 呼叫只記這一筆摘要（不含上述輪詢期間各自產生的 `read_agent` 記錄）。

```python
result = actions.wait_until(
    "rule:ci-watcher", "term_abc123",
    predicate=lambda out: "PASSED" in out.text or "FAILED" in out.text,
    timeout_sec=300, poll_interval_sec=2,
)
if result.success:
    print(result.last_output.text)
else:
    print("gave up:", result.reason, result.error)
```

### `acquire_control`

```python
def acquire_control(
    self, actor_id: str, pane_id: str,
    mode: str = "control",
) -> ControlHandle
```

取得（或共享）某個 pane 的 bridge 層 lease；`mode`／lease 語意見〈預留欄位〉節。

- **參數**：`pane_id`（`AgentInfo.pane_id`，例如 `"w1:p1"`）、`mode`（`"observe"`＝唯讀共享／`"control"`＝互斥，預設 `"control"`）。
- **lease key 正規化**：誤傳 agent_id（terminal_id）時，內部一律解析為 canonical `pane_id` 後才登記 lease——同一實體 pane 不會因識別字形式不同而繞過互斥（M1 閘門修正 X2）。
- **回傳**：`ControlHandle`（見下節）。
- **例外**：
  - `ValueError`——`mode` 不是 `"observe"` 或 `"control"`。
  - `AgentNotFoundError`（`code="pane_not_found"`）——`pane_id` 不存在（cache miss 時會先補一次 snapshot 再判死）。
  - `ControlLeaseError`——`mode="control"` 且該 pane 已被其他尚未釋放的 lease 持有。
- **Audit 記錄**：取得時記 `action="acquire_control"`（`pane_id`、`mode`）；釋放時記 `action="release_control"`（`pane_id`、`mode`）；**互斥衝突被拒時記 `action="acquire_control_denied"`**（`pane_id`、`mode`、`held_by=<持有者 actor_id>`）——被拒絕的搶奪嘗試是治理／稽核消費者的核心稽核事件（M1 閘門修正 X3）。`release()` 為冪等：並發重複釋放僅產生一筆 release 記錄。

```python
with actions.acquire_control("human:aiken", "w1:p1", mode="control") as handle:
    actions.send_to_agent("human:aiken", "term_abc123", "do not touch, I'm driving")
# 離開 with 區塊會自動呼叫 handle.release()
```

## `ControlHandle`

`acquire_control()` 的回傳型別，代表一個 lease。

| 屬性／方法 | 型別 | 說明 |
|---|---|---|
| `pane_id` | `str` | 此 lease 對應的 pane |
| `actor_id` | `str` | 持有者身份 |
| `mode` | `str` | `"observe"` 或 `"control"` |
| `released` | `bool` | 是否已釋放 |
| `release()` | `-> None` | 釋放 lease；**冪等**（重複呼叫不拋錯，第二次呼叫為 no-op） |
| `__enter__`／`__exit__` | — | context manager；離開 `with` 區塊時自動呼叫 `release()` |

## 例外類別

```
HerdrBridgeError                    所有 herdr-bridge 例外的共同基底
├── HerdrConnectionError            連不上 socket，或連線中途中斷／被伺服器關閉
├── HerdrTimeoutError                request 在逾時內未取得回應
├── HerdrApiError(code, message)    herdr 回了 {"error": {"code", "message"}} envelope
│   └── AgentNotFoundError          code 屬於 pane_not_found / agent_not_found / terminal_not_found
├── SchemaVersionError              server protocol 低於本函式庫最低支援版本
└── ControlLeaseError               acquire_control 衝突（pane 已被互斥 lease 持有）
```

`HerdrApiError`（含子類 `AgentNotFoundError`）帶兩個公開屬性：`code: str`（herdr 回傳的錯誤碼原樣保留）、`message: str`。

## 資料模型

全部為 `@dataclass(frozen=True)`——建立後不可變，跨層傳遞安全。

### `AgentInfo`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `agent_id` | `str` | herdr `terminal_id`；可直接作為 `agent.*` 方法的 `target` |
| `brand` | `str` | herdr `agent` 欄位（例如 `"claude"`、`"codex"`） |
| `status` | `AgentStatus` | `Literal["idle", "working", "blocked", "done", "unknown"]` |
| `pane_id` | `str` | 例如 `"w1:p1"` |
| `workspace_id` | `str` | |
| `tab_id` | `str` | |
| `cwd` | `str \| None` | |
| `session_ref` | `dict[str, Any] \| None` | herdr `agent_session` 原樣保留（例如 Claude Code 的原生 session UUID）；`agent_id` 跨 server 重啟失效時的替代識別依據 |
| `focused` | `bool` | |

### `AgentOutput`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `agent_id` | `str` | |
| `text` | `str` | |
| `source` | `str` | 實際使用的 socket read source（底線格式），見 `read_agent` 的 `mode` 對照表 |
| `status_at_read` | `AgentStatus` | 讀取當下的 cache 狀態，非即時查詢 |
| `revision` | `int \| None` | **0.2.2 experimental**：herdr 回應中的 monotonic revision counter；預設 `None` |
| `normalized_text` | `str`（property，0.1.1） | PTY 硬折行已合併的 `text`（孤立換行移除、空行段落保留）；窄 pane 會把 marker 從中間硬折成兩行，marker／長字串比對請用本欄位。`text` 語意凍結不變 |

### `SendResult`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `ok` | `bool` | |
| `agent_id` | `str` | |
| `actor_id` | `str` | |
| `priority` | `int` | 原樣回傳呼叫時傳入的值 |
| `sent_at` | `datetime` | UTC-aware |

### `WaitResult`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `success` | `bool` | |
| `agent_id` | `str` | |
| `reason` | `WaitReason` | `Literal["predicate", "timeout", "agent_gone", "error", "blocked"]`——穩定 API，見〈語意精確定義〉 |
| `elapsed_sec` | `float` | |
| `last_output` | `AgentOutput \| None` | 最後一次成功讀取的輸出；逾時／錯誤時仍可能非 `None` |
| `error` | `str \| None` | `reason="error"` 時的例外描述字串（`"{類型名}: {訊息}"`）；其餘情況為 `None` |

## 預留欄位（面向未來政策層的前置設計）

- **`actor_id`**（全函式）：呼叫者身份標記，慣例 `<類別>:<名稱>`（`human`／`rule`／`agent`／`system` 四類，全小寫，恰一個冒號，總長 ≤64 字元；`system:` 為 bridge 內部保留，外部呼叫者禁止使用）。工具層**不驗證、不阻擋呼叫本身**，只原樣寫入 audit log，並附加 `actor_id_status` 分級欄位（`empty`／`malformed`／`reserved_violation`；格式合法時省略此欄位，等同 `valid`）。
- **`priority`**（`send_to_agent`）：仲裁優先權預留欄位，方向為**數字越大越優先**；有效值域 **−5 至 9**，五個具名語意錨點：`BACKGROUND=-1`、`NORMAL=0`（預設）、`AUTOMATION=5`、`HUMAN_ELEVATED=7`、`HUMAN_URGENT=9`。工具層**不排序、不插隊**，只原樣記錄；超出值域的值由未來政策層負責拒絕並警告，工具層目前照樣記錄、不驗證。
- **`mode`**（`acquire_control`）：`observe`＝唯讀共享；`control`＝**bridge 進程內**互斥 lease。此 lease **不是** herdr server 端鎖——herdr 0.7.4 尚無對應的伺服器端鎖 API（`pane.clear_agent_authority`／`pane.release_agent` 屬 integration 回報語意而非鎖）；herdr 未來若補齊伺服器端鎖，底層實作將被替換、本函式介面不變。

## 語意精確定義【PPLX R-18】

- `wait_until` 的 `timeout_sec` 是**自函式呼叫起算的總時限**，不是「最後一次狀態變化後的閒置時限」。長任務（例如 Claude Code 執行一次大型重構）請自行放大這個值。
- `wait_until` 回傳而不拋出例外是規格保證；`reason` 五值（`predicate`／`timeout`／`agent_gone`／`error`／`blocked`）為穩定 API，變更需伴隨 major 版號。`blocked` 為 0.1.2 additive：agent 進入 Herdr `blocked` 狀態（等審批／等輸入）且 predicate 未命中時提前退出，不傻等 `timeout_sec`——`wait_until` 透過 `read_agent` 回傳的 `status_at_read` 偵測此狀態，與 `get_agent_status()` 獨立。
- 狀態一致性：`AgentInfo.status` 為**最終一致**視圖；訂閱重建瞬間的中間狀態轉換可能不可見，長期漂移由每 5 分鐘一次的全量 `session.snapshot` 核對修復——此為**上限**，不是典型延遲。
- `agent_id` 生命週期：僅在**同一 herdr server 執行期間**有效；跨 server 重啟請以 `AgentInfo.session_ref` 重新對應。
- **已知局限（不在三重確認機制的涵蓋範圍內，PPLX F-05）**：
  - **逾時後重試的冪等性未受保護**：terminal 指令非冪等，`wait_until` 逾時（`reason="timeout"`）後，呼叫端**不得自動重送**先前送出的指令；重送必須由人工或政策層明確決策後才發起。
  - **無心跳偵測**：目前沒有針對「agent 已無回應／卡死但無輸出變化」的主動健康檢查；`wait_until` 純靠 `timeout_sec` 逾時兜底判斷異常，不做心跳探測。心跳偵測與其後的降級（延長重試 → 標記人工複核 → 電路中斷）屬政策層降級鏈職責，不在工具層責任範圍內。


## Audit 事件一覽（供治理／稽核消費者對照）

| action | 附加欄位 | 觸發時機 |
|---|---|---|
| `list_agents` | `count` | 每次呼叫 |
| `read_agent` | `agent_id`、`mode`、`chars` | 每次呼叫（輸出全文不落地） |
| `send_to_agent` | `agent_id`、`priority`、`chars` | 送出成功後 |
| `get_agent_status` | `agent_id`、`status` | 每次呼叫 |
| `wait_until` | `agent_id`、`success`、`reason`、`elapsed_sec` | 等待結束（五種 reason 皆記） |
| `acquire_control` | `pane_id`、`mode` | lease 取得成功 |
| `acquire_control_denied` | `pane_id`、`mode`、`held_by` | control 互斥衝突被拒 |
| `release_control` | `pane_id`、`mode` | lease 釋放（冪等，僅一筆） |

每筆共通欄位：`ts`（ISO8601 UTC）、`actor_id`、`action`；actor_id 格式不合法時另附 `actor_id_status`。檔案：JSONL、0600、預設 `~/.local/state/herdr-bridge/audit.jsonl`。

---

## 0.1.1 新增公開面（additive；v0.1.0 全部簽名不變）

下游專案實際使用過程中揪出的工具層摩擦修復（PPLX 共識），皆為新增、不動既有語意：

### `get_audit_log_path() -> pathlib.Path`

預設 audit log 路徑的公開唯讀查詢（套件頂層可匯入）。純查詢、無副作用（不建目錄、不改權限）。消費者（如下游的稽核檢視工具）一律用本函式取路徑，**不要** reach into `AuditLogger` 內部——內部佈局屬私有，0.2 重構不保證不變。

### `AgentOutput.normalized_text`

見上方 `AgentOutput` 資料模型表。marker 比對建議一律用 `out.normalized_text`（`wait_until` docstring 同步註記）。

### `BridgeActions.resolved_socket_path` / `BridgeActions.socket_source`

F-2 可觀測連線目標（自動化呼叫端可 assert 防連錯 session）：

- `resolved_socket_path: str`——實際連上的 herdr socket 路徑（唯讀 property）。
- `socket_source: str`——路徑來源：`'explicit'`（顯式傳入）｜`'env'`（`HERDR_SOCKET_PATH`）｜`'detected'`（`herdr status` 偵測）｜`'unknown'`（未經 `connect()` 工廠建立，來源不可知）。自動化情境應為 `'explicit'`。

### `SocketClient.subscribe()` 的 `on_state` 新值 `"degraded"`

訂閱 reader 連續重連失敗達閾值（預設連續 10 次失敗**或**持續失敗 60 秒；可用新的 keyword-only 參數 `degraded_after_failures` / `degraded_after_sec` 調整）時，經既有 `on_state` 回呼上報 `"degraded"` **恰一次**並記 warning log。**重連不會停**——degraded 是告知（讓長命消費者區分「暫時重啟」vs「疑似永久消失」），不是終止；成功重連後旗標歸零、可再次觸發。既有值（`connected`／`reconnected`／`disconnected`）語意不變。

---

## 0.1.2 新增公開面（additive；v0.1.0/v0.1.1 全部簽名不變，2026-07）

### `get_agent_status(actor_id, agent_id) -> AgentStatus`

第六個公開方法（`BridgeActions` 方法）。查詢 agent 目前的 Herdr 狀態（`idle`／`working`／`blocked`／`done`／`unknown`），不對狀態做語意判斷——用途是讓呼叫端能以輪詢或事件驅動方式取得 Herdr 原生狀態，自行決定 `blocked` 是否需要介入。完整簽名與使用方式見上方〈`get_agent_status`〉節。

### `wait_until` 的 `blocked` 提前退出

`wait_until` 在每一輪 `read_agent` 後檢查 `status_at_read == "blocked"`：當 agent 被 Herdr 判定為等待外部輸入（如審批對話框），且 `predicate` 未命中時，立即以 `WaitResult(success=False, reason="blocked")` 退出，不再傻等 `timeout_sec`——開箱即用的「別乾等一個卡住的 agent」。

既有四種 `reason`（`predicate`／`timeout`／`agent_gone`／`error`）語意不變；`blocked` 為 additive 第五值。`WaitReason` 型別已更新為 `Literal["predicate", "timeout", "agent_gone", "error", "blocked"]`。

此機制獨立於 `get_agent_status()`——`wait_until` 內部的 blocked 偵測走 `read_agent` 的 `status_at_read`，不額外呼叫 `get_agent_status`，兩者各自讀取 cache 中的 `AgentInfo.status`。

---

## 0.2.2 新增公開面（additive；v0.2.1 全部簽名不變，2026-07）

WP4：D6 revision cursor——讓消費者以 monotonic revision counter 做增量讀取（避免反覆拉取完整輸出）。

### `AgentOutput.revision`

`AgentOutput` 新增 `revision: int | None` 欄位（預設 `None`）。既有呼叫不傳此欄位時行為與 0.2.1 相同。

### `read_agent(since_revision=...)` / `wait_until(since_revision=...)`

`read_agent` 與 `wait_until` 新增 keyword-only 參數 `since_revision: int | None = None`。傳入非 `None` 時，herdr 僅回傳該 revision 之後的輸出變更（herdr 協定原生 `agent.read` 的 `since_revision` 參數）。不傳時行為與 0.2.1 相同（完整輸出）。

`wait_until` 的 `since_revision` 會在每一輪輪詢中傳遞給內部的 `read_agent` 呼叫。

### `_RevisionAdapter`（experimental）

將 herdr 回應中的 revision 值正規化為 `int | None` 的內部輔助函式：僅接受 `int`（排除 `bool`——Python 中 `bool` 是 `int` 子類別，但 revision 語意非布林）；其他型別（含 `None`、`float`、`str`）一律降級為 `None`。

此為 experimental feature（0.2.2），介面可能隨 herdr 協定演進而調整。消費端不宜直接依賴此函式。

### `@pytest.mark.empirical`

真機四項語意（monotonic、stable、since-filtering、session-reset）以 `@pytest.mark.empirical` 標記，CI 中 deselected——需真實 herdr server 環境才執行。
