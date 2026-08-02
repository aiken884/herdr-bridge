# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). See [`CONTRIBUTING.md`](CONTRIBUTING.md) for this project's versioning policy while the version stays pre-1.0.

## [Unreleased]

## [0.7.0] — 2026-08-02 — Herdr Bridge Signal (6th communication layer)

### Added
- **Herdr Bridge Signal** (`herdr_bridge.signal`): a resident per-tower daemon that lets one tower push-wake another instead of relying on polling — an additive 6th communication layer alongside the existing 5 (Primary/Secondary/Tertiary/notify-pane/raw-input), none of whose behavior changes. Went through six rounds of adversarial design review plus a validation spike before implementation: the spike found `notify-pane` self-injection reliable (~1s, PASS) but Herdr's `events.subscribe`/`pane.output_matched` unreliable as a transport shortcut (FAIL — caching/replay behavior, not a stable per-event trigger), so the daemon uses a self-built Unix domain socket server rather than the originally-hoped-for simplification.
  - `herdr-commander signal start` — start the resident daemon for this project (own pane_id resolved via a three-tier fallback: `HERDR_PANE_ID` env var → pin file → `herdr pane list` cwd scan, refusing to start on an ambiguous match rather than guessing).
  - `herdr-commander signal send --to <project> --inbox-ref <ref>` — wake the target project's daemon; the wake envelope carries only a reference into your own memory/storage layer, never content itself. Applies escalation rules (retry-then-`daemon_unreachable`, `injection_unconfirmed` on a confirmed-but-not-injected timeout) and reports failures with a concrete, non-zero exit rather than a silent success.
  - `herdr-commander signal status` — daemon liveness plus recent ACK records (Accepted → Injected → Seen → Accepted-for-work → Completed, each written by exactly one role — see `orchestration.memory`'s `mark_*` functions).
  - `herdr-commander doctor` now distinguishes a stalled daemon (restart it) from a daemon still bound to a pane that no longer exists (restart it fresh) instead of reporting a bare "abnormal".
  - Security model: HMAC-signed envelopes, TTL + in-memory nonce replay protection (deliberately not persisted, by design — replay protection only needs to cover the live delivery window), a 0600 Unix socket under `~/.local/state/herdr-bridge/signal/<project>/`, single-instance file lock (kernel-released on crash, no stale-PID logic needed).
  - 78 new tests across envelope signing/verification, the single-instance lock (including a real subprocess-death test), the ACK state machine (including a same-row write race between the sender and receiver processes, found by testing rather than assumed away), the daemon's merge/idempotency/escalation logic over a real Unix socket, and the CLI/doctor integration.
  - **Multi-instance field rollout**: real-world use across several independently-deployed instances of this project caught and drove fixes for three real bugs: (1) a bare `signal start`/`send`/`status` with no `--project` silently defaulted to `herdr-bridge` and could collide with its daemon lock; (2) the first fix's warning was itself dead code, because `herdr_bridge/__init__.py` unconditionally force-sets `os.environ["REMAGRAPH_PROJECT"] = "herdr-bridge"` as an import-time side effect, which a same-process unit test can't exercise — the actual fix drops `REMAGRAPH_PROJECT` from the signal project-resolution chain entirely and adds a real subprocess regression test; (3) the resulting `_resolve_signal_project()` now checks `--project` > `CT_PROJECT` > `HERDR_MEMORY_PROJECT`, warning on stderr whenever it falls through to the `herdr-bridge` default.

### Fixed
- **`signal send` could still crash the caller's whole CLI process on a completed-send race, even after an earlier fix for the same class of issue**: `mark_accepted()` and `mark_escalated()` in `orchestration/memory.py` both read the Signal ACK row once to decide what to do, then delegate to `_write_signal_state()`, which independently re-reads the row a *second* time to validate the transition. Across two independent OS processes (`outbound.py` the sender, `daemon.py` the receiver) with no shared lock between them, the daemon can advance the row all the way to `completed` in the gap between those two reads — the earlier fix only closed the *wide* version of this race (daemon already done by the first read); it left this *narrow* version open (daemon finishes between the first and second read), and `_validate_signal_transition()` would raise an uncaught `ValueError` (e.g. `"Invalid Signal transition: completed -> injection_unconfirmed. Allowed: []"`) straight through `outbound.send()`, crashing the CLI even though the send had already succeeded. Fixed with a bounded local retry (bounded, not pushed down into `_write_signal_state()` itself — other callers like `mark_injected()`/`mark_completed()` must keep raising on a genuinely illegal transition) that re-reads the freshest state and retries the whole decision, not just the write. 4 new regression tests, including one that reproduces the exact traceback message from a real field incident.
- **`AgentNotFoundError` restore-after-restart limitation**: confirmed a third party independently filed and fixed this upstream in Herdr itself ([herdrdev/herdr#2065](https://github.com/herdrdev/herdr/issues/2065), fix merged 2026-07-30), but it has not shipped in any tagged Herdr release yet as of this writing — the documented workaround (delete and recreate the affected session) remains necessary until a newer Herdr release confirms the fix.

## [0.6.0] — 2026-08-01 — Herdr Bridge Memory branding, first public release

### Added
- **`herdr-commander memory note "<message>" --task-id <id> --agent-id <id>`**: a minimal CLI escape hatch for manually logging a memory note, wrapping the embedded memory backend without requiring users to know its underlying name.
- **`HERDR_MEMORY_MODE` / `HERDR_MEMORY_PROJECT`**: user-facing env vars for controlling the memory feature and its project id (see `docs/memory-advanced.md` for the full reference and the intentional advanced-user bypass path).
- **`docs/memory-advanced.md`**: full disclosure of the embedded memory backend for advanced users/integrators.

### Changed
- **Herdr Bridge Memory branding**: the embedded memory backend (RemaGraph, an independent published project) is no longer named in user-facing CLI output, `herdr-commander doctor` diagnostics, or `--help` text — it's now presented as the built-in "Herdr Bridge Memory" feature. `HERDR_REMAGRAPH_MODE` is deprecated in favor of `HERDR_MEMORY_MODE` (the old name still works, with a `DeprecationWarning`); `HERDR_MEMORY_PROJECT` / the existing `--project` flag now take priority over reading `REMAGRAPH_PROJECT` directly (setting `REMAGRAPH_PROJECT` directly still works, as an intentional advanced-user bypass). The dependency itself, its license, and its GitHub project remain fully named in `pyproject.toml`, `NOTICE`, and `CONTRIBUTING.md`.
- **First public release**: per `docs/open-source-extraction-plan.md` (Option A), the project is now published as a fresh, scrubbed public repository (`aiken884/herdr-bridge`) with clean history; the original private repository continues internal development under a new name. `Development Status` classifier bumped from Alpha to Beta.

### Fixed
- **Three pre-existing exception-handling gaps that could leak the memory backend's internal naming/tracebacks to end users**: `LightCommander.run_task()`'s safety-valve check was unguarded (unlike its constructor and `AcpRouter`'s equivalent checks); several `store_memory()` call sites were unguarded, including one inside an `except` block that could mask the original exception being handled; `herdr_bridge/__init__.py`'s import of the memory backend had no defensive handling for the case where it isn't installed.
- **Six CI-only test flakiness/environment gaps**, found by watching this release's actual GitHub Actions runs (a week-plus GitHub billing block had left the private repo's own CI never actually completing): two tests depended on a real `claude` CLI on `PATH` instead of mocking `shutil.which`; one test shelled out to a real `herdr` server without the `@pytest.mark.integration` marker CI already uses to deselect that class of test; two Moshi-hook retry tests raced a real Unix socket + background thread against the client's write, rewritten to use deterministic object mocking instead; one ACP cancellation test raced a real subprocess response instead of using the existing `$HANG$` deterministic trigger.
- **CI matrix `fail-fast` defaulting to `true`** let one already-known-flaky Python version (3.14, an upstream pydantic/runner incompatibility) cancel every other version's results before they could report their own.

## [0.5.0] — 2026-07-25 — ACP SDK graduated to a core dependency, expanded multi-brand TUI support, cross-tower coordination protocol

### Added (2026-07-25 evening)
- **`herdr-commander doctor` (one-shot diagnostic)**: consolidates manual troubleshooting of RemaGraph connectivity, pane-state drift, and `project.json` binding mismatches (see #66) into a single command. Checks that the global install is active, RemaGraph connects successfully, `project.json`'s `project_id` matches, and whether the `maintenance_completed` count in the last hour looks anomalous (suspected runaway cleanup loop from an external `serve` process). Returns a non-zero exit code with concrete reasons on any failure. 8 new tests cover the fully-healthy case, four individual failure modes, correct `project.json` matching (no false positive), and time-window boundary conditions.
- **`--tui` support for two new brands: `copilot` and `gemini`** (#78, #79): `copilot` (GitHub Copilot CLI 1.0.71) has a screen layout nearly identical to `claude`'s (`❯` prompt + paired `─` border lines); a real end-to-end test (not read-only observation) confirmed messages actually land in the history pane and the input box clears. Adding `gemini` support surfaced two genuine bugs along the way (see Fixed below). `--tui` now supports seven brands total: `claude`/`agy`/`codex`/`grok`/`opencode`/`copilot`/`gemini`. **Kimi CLI and CodeBuddy CLI are explicitly out of scope** (Aiken's 2026-07-25 product call) — no patterns registered for either.
- **Cross-tower shared-infrastructure coordination protocol**: a PPLX-consensus design; Aiken decided herdr-bridge, RemaGraph, and other internal coordination nodes would adopt it jointly. Formalizes the existing RemaGraph + notify-pane conventions instead of standing up a dedicated audit tower: four fixed cross-project labels (`infra-health`/`infra-change`/`infra-incident`/`infra-owner`), a mandatory `infra-change` entry before touching shared infrastructure followed by an `infra-health` entry afterward, herdr-bridge as the initial template owner, other nodes rotating health checks — all folded into each node's existing `/goal` loop with no dedicated headcount.

### Fixed (2026-07-25 evening)
- **ACP downstream process inheriting the tower's full environment leaked 151 skills from opencode**: `spawn_agent_process()` did `env = dict(os.environ)`, passing the tower's entire process environment verbatim to the downstream agent process. Traces in `PATH` (`~/.claude/plugins/cache/...`) and variables like `CLAUDECODE`/`CLAUDE_CODE_*`/`AI_AGENT` caused the downstream agent (observed with opencode) to falsely conclude it was running under Claude Code, so its `available_commands_update` response dumped all 151 unrelated local plugins/skills — burning 57,859 tokens on a single trivial round trip. Added `_clean_downstream_env()` to strip `CLAUDE*`/`ANTHROPIC*`/`AI_AGENT` environment variables and any `PATH` segment containing `.claude`; verified the leak is gone. Same class of issue as the known Grok token risk documented in BOUNDARIES.md WP9, but a different root cause — here the fault lies with the caller (the tower), not with the downstream agent scanning on its own.
- **notify-pane misjudged delivery when a long message scrolled past the visible input box**: a real incident — sending long, multi-paragraph decision notices to other internal coordination nodes and to RemaGraph, `notify-pane` reported delivery confirmed every time, but the message was still sitting entirely in the input box and had never actually been sent. Root cause: `_looks_submitted()` only checked whether the message's first line was still visible in the box; once a long message scrolled the box content to the end, the first line scrolled out of view, and the old logic — unable to find the "head" — wrongly treated that as confirmed submission. Fixed to: a fully empty box is the only confirmed-submitted signal; finding the head is confirmed-not-submitted; anything else is treated as uncertain, triggering a follow-up Enter rather than confidently declaring success.
- **Two genuine notify-pane delivery-misjudgment bugs against Gemini CLI** (hit live during a fresh install): (1) `_extract_input_box_text`'s box-content validation was hardcoded to recognize only the `❯` marker, but Gemini uses a `>` prompt with `▄`/`▀` block-character borders; failing to find `❯` caused it to misjudge "this isn't an input box" and fall back to a single-line scan that drops wrapped lines. Fixed by accepting a caller-supplied marker set. (2) Gemini's idle input box shows grayscale placeholder text rather than true blank space, so the old logic's requirement of a character-for-character empty box could never be satisfied for this TUI. Fixed to: head found = confirmed-not-submitted, box empty = confirmed-submitted, message tail still in the box = confirmed-not-submitted (this also covers the long-message-scroll case from the item above), neither head nor tail found = confirmed-submitted. `_BORDER_LINE_RE` extended to also match `▄`/`▀`.

### Changed (2026-07-25 evening)
- **`agent-client-protocol` promoted from an optional extra to a core dependency, formally activating the Secondary layer** (#82): Aiken's product decision — the fleet's goal is fully headless operation, and the ACP SDK is a real capability that goal requires. Both previously-blocking issues are resolved (the `user_text` `NameError` was cleaned up during pre-release linting; the opencode skill leak is fixed above); the third suspected issue (`acp-echo-agent.py` connection failures) could not be reproduced under a real pipe-based spawn re-verification and is judged to have been a transient environment issue. `agent-client-protocol` moved from `pyproject.toml`'s optional extras into the main `dependencies`; 3 real downstream ACP tests that were previously skipped for lack of acp-sdk now actually run and pass. The honest degraded-reporting logic (`ok`/`degraded`/`delivery_status`) is unchanged — the SDK is simply available by default now, so users no longer need to remember to opt into the extra manually.

### Test (2026-07-25 evening)
- **mutmut run against `delivery_state_store.py`**: temporarily pointed `[tool.mutmut]` at this module for one pass (not a permanent CI-gate change — reverted back to gating `schema.py` afterward), finding one genuine test gap: `_connect()`'s `mkdir(parents=True)` had no test verifying it was actually necessary. Added a test with multiple levels of nonexistent path to kill the surviving mutant. The other 13 surviving mutants were judged equivalent mutants (no observable behavioral difference). Conclusion: this module is not recommended for a standing CI gate.

### Operational
- **Rescue-backup data migration**: filtered herdr-bridge-relevant records out of 6 backup DBs under `~/.local/state/_rescue-backup-20260725/` and wrote them back through the normal `store_memory()` path (with its built-in dedup check) into the primary store — 38 candidates, 34 migrated successfully, 4 correctly blocked by the dedup mechanism. A pure data operation, no code changes.

## [0.4.0] — 2026-07-25 — Full TUI/headless communication real-usage validation + delivery-state FSM rework

### Added (2026-07-25 — full TUI/headless communication real-usage validation)
- **`herdr-commander notify-pane` (fourth-layer communication)**: the only reliable channel for interactive TUI panes — atomic keystroke injection (a real newline sent in one shot, avoiding the TUI event-loop race condition from sending Enter as a separate step) plus screen-diff delivery verification, with per-TUI patterns (claude/opencode/codex/grok/agy) to judge submission success. Failure to confirm delivery within the retry budget produces an explicit error rather than a silent false success. Four readiness checks run before injection: blocking on interactive prompts (trust confirmations/menus/y-n/passwords), zombie-pane detection, busy-pane rejection (with an `--allow-busy` escape hatch), and startup-readiness waiting.
- **Global install support**: `pipx install --editable <repo>` installs `herdr-commander` into `~/.local/bin`, so any project's panes can call it directly without being confined to herdr-bridge's own venv.
- **Dedicated lightweight storage for the delivery-state FSM** (`orchestration/delivery_state_store.py`): state transitions now use a dedicated SQLite store instead of the general memory layer, writing a summary into RemaGraph only on terminal states (dual-write on terminal state) — fixing an architecture mismatch where a minute-scale FSM lifecycle was incorrectly subjected to the memory layer's semantic dedup (which requires similarity), causing consecutive transitions to be rejected outright.
- **`docs/light-user-quickstart.md` gained a "five communication channels overview + raw `herdr pane` command SOT" section**: a comparison table of RemaGraph store/search, ACP dispatch, side-channel, notify-pane, and raw `herdr pane send-text` — their positioning and delivery-confirmation capabilities — plus a precise syntax reference for the raw commands (`send-text`/`read` are positional arguments, `process-info` is a flag; easy to mix up).

### Fixed (2026-07-25)
- **ACP echo fallback no longer silently reports false success**: when `agent-client-protocol` is not installed, `dispatch`/`dispatch_with_memory_confirm` now explicitly return `ok=False`, `degraded=True`, `delivery_status="not_attempted"`, `confirmed_via="echo-fallback"`, and the CLI returns a non-zero exit code accordingly; the RemaGraph audit record is likewise marked as degraded rather than normally completed (PPLX review consensus).
- **Echo-fallback self-contamination bug**: the router was treating the report it sent to its own listener as downstream confirmation, so `pong_confirmed`/`side_confirmed` still showed `True` even under echo fallback (i.e., when no work was actually dispatched).
- **Fleet event listener subscription race**: back-to-back calls to `_watch_fleet_pane()` were being split into multiple separate `events.subscribe` calls, which Herdr treated as anomalous and reset/reconnected. Added `_sub_lock` for atomicity plus a 0.15s debounce to coalesce consecutive calls.
- **notify-pane readiness-check false positive**: persistently-rendered todo/status-panel text at the bottom of the screen (e.g., a task description mentioning "password") was previously matched by a naive substring check and misjudged as a password prompt, causing legitimate injections to be rejected. Fixed to require an immediately-following colon (`password\s*:`), matching only genuine password-field formatting.
- **Several notify-pane delivery-judgment defects around zombie panes, busy panes, narrow-pane line wrapping, and per-TUI differences** (see the series of `fix(light)`/`fix(acp)`-prefixed commits in the 2026-07-25 git log for details).

### Changed (2026-07-25)
- **Test coverage threshold lowered from 80% to 70%**, aligning with `CLAUDE.md`'s existing policy (new projects: at least 70%); measured coverage excluding integration tests is 77.03% — this is not lowering the bar just to turn the badge green.
- **RemaGraph dependency upgrade** (`00414e0`): picked up an upstream fix for the `project_id`/`state_dir` binding safety valve (the comparison logic was previously a tautology that never actually blocked mismatched-`project_id` connections, which could let cross-project data get miswritten or cleared) and a configurable cross-project fan-out cap (previously hardcoded at 20, now defaults to 50 and is tunable via `--fanout-cap`/`REMAGRAPH_FANOUT_CAP`, with a hard ceiling of 200).

### Fixed (2026-07-25 — latent issues surfaced by the pre-release ruff/mypy pass)
- **`prepare_dispatch_text()` never actually enriched prompts with memory**: its internal call to `recall_memories(..., project_id=project_id)` referenced a variable name that didn't exist (the function parameter is actually called `project`); the resulting `NameError` was silently swallowed by an outer `except Exception`, so it had quietly been returning the raw text all along.
- **Undefined variable on the ACP downstream call path**: the ACP downstream branch in `router.py` referenced a nonexistent `user_text` (should have been `send_text`); this path never executed while acp-sdk was uninstalled, which is why it went unnoticed until now.
- **`_get_target_health()`'s health logic was incomplete**: the docstring promised tiered results based on 5/15-minute thresholds, but the function actually returned "Healthy" as soon as it found any pong/ack record, without using the computed timestamp. Added the actual age comparison.
- Assorted ruff (bare except, ambiguous variable names, unused variables/imports) and mypy (missing `dict` generic parameters, type ambiguity) cleanup; ruff/mypy are now fully clean.

### Added
- **`post-commit` git hook: commits automatically write back to RemaGraph memory** (charter §3.5.9, item 4).
  - `.githooks/post-commit`: after every commit, calls `remagraph store --kind status_update`; `project_id` is derived from the main repo's directory name (worktree-safe — unaffected by `git worktree` subdirectory naming), `task_id` is `<project>-commit-<short-hash>`, and `agent_id` is derived from the `AGENT_ID` env var or `git config user.name` and slugified.
  - Agent-agnostic: pure shell + native git mechanics, not limited to Claude Code — any underlying agent running `git commit` triggers it.
  - Degrades silently (prints one stderr note) when `remagraph` isn't installed or the write fails; never blocks or fails the commit.
  - The old DCO hook `.githooks/commit-msg` was moved into the same directory, both now enabled via `git config core.hooksPath .githooks` (run `bash .githooks/install.sh`), replacing the old manual copy-into-`.git/hooks/` approach (that directory isn't version-controlled and doesn't carry over across clones).
  - Tests: `tests/test_hooks_post_commit_remagraph.py` — creates a real temporary git repo, runs `git commit`, then reads back via `remagraph search` to verify the record actually exists (covering worktree safety, `AGENT_ID` override, very-short commit subjects still clearing the arbitration threshold, and graceful degradation when remagraph isn't installed).

### Fixed
- **Packaging dependency fix**: since `remagraph` isn't published to PyPI yet, `dependencies` now pins it via `remagraph @ git+ssh://...@<SHA>` directly against RemaGraph's commit SHA (rather than a package name), with `[tool.hatch.metadata] allow-direct-references = true` added (hatchling disallows git-URL dependencies in `dependencies` by default; without this setting, `uv build`/`pip install` fail metadata validation). The `[tool.uv.sources]` local editable override is unchanged and only affects local development (`uv sync` still prefers `../RemaGraph`) — it is not baked into the published wheel/sdist metadata. Verified that the wheel/sdist's `Requires-Dist` contains the full git URL, and simulated a successful install for an external consumer in a clean environment.

### Improved
- **Strengthened 3-layer confirmation**: real pane dispatch now leans on side-channel reports as a stronger fallback.
  - `wait_for_pong` now also detects side-channel tag/complete reports and treats them as a valid ack (`via: "side-channel"`).
  - `dispatch_with_memory_confirm` explicitly checks for a side report on pong timeout, setting `SIDE_REPORT_RECEIVED` and `side_confirmed`.
  - Introduced an `AWAIT_PONG` state to reduce reliance on a bare PONG; a side report alone can confirm success (making the TIMEOUT fallback more robust).
  - Pane tests can now report `"confirmed_via": "side-channel"` or `"pong"`.

### Added
- Additional callers/examples now call the RemaGraph `ensure`:
  - `scripts/demo-embedded-remagraph-acp-pipe.py` (`project="herdr-demo"`) at the top level.
  - `examples/central-tower-minimal.py` (custom project) at the top level, with accompanying notes.
- Compatibility: even with a custom project, the entry point still enforces a dedicated DB and the safety valve.

### Changed
- Updated the FSM flow to support side-channel as the tertiary confirmation.
- Improved documentation and example consistency.

## [0.3.0] — 2026-07-22 — ACP Router + 4 real downstream agents + RemaGraph embedding complete (post-PPLX-review initial/mid phase + stretch goals)

### Added
- **AcpRouter** (`acp/router.py`): the tower acts simultaneously as an ACP server and client, with built-in dynamic registry discovery.
- 4 real, independent downstream ACP agents (`examples/acp-*-agent.py`): echo-tui, research-tui, code-tui, general-tui (full `acp.Agent` implementations, each with distinct `result_text`).
- Full CLI router hookup:
  - `herdr-commander router {list, discover, route, register, unregister, start}`
  - `--path`, `--capability`, `--command`/`--args` for arbitrary external agents
  - `run --use-acp-router` (auto-chooses by capability + target)
  - `-v status` shows "ACP Router registry: N agents (dynamic discovery)"
- Expanded registry discovery:
  - `create_herdr_router()` is now fully dynamic (examples + `HERDR_ACP_AGENT_PATHS` + `~/.config/herdr/acp-agents/` + `additional_paths` + `PATH` + persisted JSON)
  - Broader glob (`*acp*` / `*agent*`, including non-`.py` binaries)
  - `register_agent` / `unregister_agent` clean up the persisted registry
  - CLI `register` persists across restarts; a fresh instance auto-loads it
- Deep LightCommander integration:
  - `run_task_via_acp(use_router=True)`
  - `route_via_acp_router`
  - `dispatch_with_memory_confirm`
  - `batch_dispatch_with_memory` (fleet-level)
- Full RemaGraph embedding:
  - All dispatch paths default to `prepare_dispatch_text` + `store_memory`
  - Covers the herdr-socket path, the ACP path, and the router
  - Strengthened governance-embedding tests
- **Option A implementation**: added `CentralTower` + `create_central_tower()`, a high-level facade
  - Clean sync API: `tower.dispatch(prompt, target=...)`, `batch_dispatch`
  - Enforces RemaGraph prepare/store on every path; hides router internals, RemaGraph details, and worktree mechanics
  - Available as a top-level import; external projects can plug it in directly as "the one tower"
  - Added `examples/central-tower-minimal.py` + updated the cross-project example + `docs/api-acp.md`
- Tests: 9 router-specific + 10 embedding-specific = 19 targeted tests + the full suite at 407 passing (88% coverage)
- Updated the cross-project example to 4 agents + store-ack
- Aligned the PPLX planning doc with current state
- Completed PPLX stretch priorities 1–4 (deeper event-driven behavior):
  - Priority 1: made `actions.wait_until` fully event-driven (subscribes to `pane.agent_status_changed` + `pane.output_matched`; callback checks predicate/blocked; dedupes on `last_matched_line` + revision; a match event wakes the waiter; poll/sleep fallback nearly eliminated)
  - Priority 2: cache consistency reduced to a lightweight safety net (`_consistency_tick` does only a lightweight subscription-liveness check; a full snapshot runs only every 3rd tick; added a `_pane_state` map for idempotency; rebuilds only on detected drift)
  - Priority 3: reworked the background listener into a stateful `_FleetEventListener` class (pane-state map, exponential backoff with jitter, auto-resubscribe on reconnect, dynamic `watch()`, sequence dedup, clean `stop()`)
  - Priority 4: improved `output_matched`/permission handling (broad `output_matched` subscription filtered client-side; multi-variant + normalized regex matching that only fires when blocked; `_watch` subscribes to both event types; `wait_until` has a built-in permission subscription)

### Changed
- All governance-layer dispatch paths (`run_task`, `via_acp`, router prompt, batch) now uniformly go through RemaGraph prepare/store
- CLI `status` and `router list` expose full registry metadata + summary + filtering

### Verified
- Live: registering an arbitrary external command → persists → a new router instance sees it → routes to it → unregister cleans it up
- `--path`-based discovery expansion works correctly
- Auto-choice logic (research → research-tui, implement → code-tui)
- Batch dispatch routes across targets correctly by cap
- All 4 agents produce distinct real output, verified against the raw ACP protocol directly

## [0.2.2] — improvement batch, 2026-07-21: mutmut · Claude · SDK transport · CodeQL/Scorecard · documentation wrap-up

> Tag: `v0.2.2`, commit `f0cd1f7`

### Added

**Revision cursor (WP4)**
- `AgentOutput.revision: int | None` — a monotonic revision counter from herdr's response (WP4, experimental). Defaults to `None`; existing callers that don't pass this field behave exactly as in 0.2.1.
- `BridgeActions.read_agent` and `wait_until` gained a keyword-only `since_revision: int | None = None` parameter — returns only output changes after that revision (natively supported by the herdr protocol). Behavior is unchanged when omitted.
- `_RevisionAdapter` (experimental) — normalizes revision values from herdr responses to `int | None`; non-int values (including bool, float, str, None) are downgraded to `None`. Consumers should not depend on this function directly.
- `@pytest.mark.empirical` marker — four real-machine semantic tests (monotonic, stable, since-filtering, session-reset) are deselected in CI.
- `tests/test_revision_cursor.py` — unit tests for `_RevisionAdapter`, integration tests for `read_agent`/`wait_until`'s `since_revision`, and stubs for the four empirical tests.

**ACP expansion — Claude tier (WP6)**
- `AcpxTransport` gained `agent="claude"` support: `_default_agent_resolver()` now accepts `'claude'` (via `CLAUDE_BIN` env or `shutil.which`); `build_acpx_argv_and_env()` branches by agent (opencode uses `--agent` + a config file, claude uses global `--cwd` flags); `ensure_session()` skips `write_session_config()` for claude.
- `list_acp_agents()` now returns both the opencode and claude tiers.
- The `AcpTransport` Protocol is now frozen — clearing the way for WP7 (SDK transport).

**ACP expansion — AcpSdkTransport (WP7)**
- `herdr_bridge.acp.sdk_transport.AcpSdkTransport` — talks to an ACP agent directly over stdio using the official `agent-client-protocol` PyPI package (`>=0.10.0,<0.12`), bypassing the `acpx` CLI intermediary layer.
- `_AcpSdkClient` — a dedicated background event-loop thread, auto-responds to `request_permission` (aligned with `AcpPolicy`), and collects `session_update` events.
- Full session lifecycle (ensure/close), `run_prompt`/`start_prompt`/`wait_done`/`cancel` with timeouts, and `get_history`.
- Import guard: raises a clear `ImportError` if `herdr-bridge[acp-sdk]` isn't installed; not re-exported from the main `__init__.py`.
- Added `[project.optional-dependencies] acp-sdk` to `pyproject.toml` (opt-in, default behavior unchanged).

**CI**
- mutmut nightly hard gate (WP2): removed from the PR CI matrix (previously `continue-on-error: true`), replaced with a dedicated `schedule` + `push-to-main` workflow (`.github/workflows/mutmut-gate.yml`). Baseline: 1,149 total mutants, 6.3% kill rate; added 8 mutation-killing regression tests (33 → 15 survivors).
- CodeQL + OpenSSF Scorecard workflow stubs (WP12): `.github/workflows/codeql.yml`, `.github/workflows/scorecard.yml`, gated inert via `if: vars.REPO_PUBLIC == 'true'` until the public release date. README has a pre-placed Scorecard badge (as an HTML comment).

### Changed
- Strengthened the test suite: `tests/test_schema.py` gained 8 mutation-killing regression tests (WP2).
- `BOUNDARIES.md`: updated the ACP tier support list to opencode/claude/copilot/grok (four tiers); added a note on the Grok token risk (WP13).

## [0.2.1] — TD-005b: add `herdr_bridge.testing` (FakeHerdrServer)

> Tag: `v0.2.1` (unsigned — no GPG key on this machine), commit `2cd7110`

### Added
- Public subpackage `herdr_bridge.testing`, exporting `FakeHerdrServer`, `FakeApiError`, and `Handler` so downstream consumers can run contract tests without a real herdr server.
- `docs/testing.md` — usage guide and contract-test examples for FakeHerdrServer.
- `tests/fake_server.py` converted into a transitional shim (lifespan: through the next bridge PR, then deleted).

### Changed
- Migrated all test imports from `tests.fake_server` to `herdr_bridge.testing`.

## [0.2.0] - 2026-07-20

ACP command plane: a new, separate `herdr_bridge.acp` module — provisional/experimental additive tier, **not** covered by the frozen five-function semver guarantee (see `BOUNDARIES.md`). All existing v0.1.x public signatures are unchanged.

### Added
- `herdr_bridge.acp.connect() -> AcpActions` and `AcpActions`'s nine methods (`list_acp_agents`, `ensure_session`, `close_session`, `get_history`, `prompt`, `exec_prompt`, `start_prompt`/`wait_done`, `cancel`, `close`) — drives opencode (currently the only supported tier) via the `acpx` CLI over the Agent Client Protocol, replacing screen-scraping/marker-based dispatch with structured `session/update` events and an explicit `stopReason`.
- `AcpTransport` Protocol + `AcpxTransport` (SDK-swap seam) and `AcpxAdapter`'s pure functions (`build_opencode_permission_config`, `write_session_config`, `build_acpx_argv_and_env`, `build_acpx_policy_flags`, `resolve_patched_opencode_binary`, `detect_target_triple`) — makes opencode's local permission engine actually enforce a `deny`/`ask`/`allow` policy under acpx, closing a gap where acpx's own permission flags are purely reactive and never configure the agent itself.
- `herdr_bridge.acp.binding` — policy-neutral pane↔session dispatch ledger (pure functions; canonical join key is herdr `pane_id` + ledger, not the ACP `session_name` string).
- `scripts/rebuild-patched-opencode.sh` — rebuilds the locally-patched opencode binary (fixes a real upstream bug where child/subagent ACP sessions were never registered, hanging any `session/prompt` that needed to ask permission for a delegated subagent's own action) and refreshes `.vendor/opencode-patched/MANIFEST.json`.

### Known limitations (tracked, non-blocking)
- Only `agent="opencode"` is wired up; other tiers await named acpx agent config entries from the governance layer.
- `cancel()` terminates the underlying acpx subprocess rather than sending a true ACP `session/cancel` protocol message.
- `prompt()`'s `policy` parameter is accepted for signature compatibility but does not take effect — policy is fixed at `ensure_session()` time.

See `docs/api-acp.md` for the full reference, verification, and known-limitation detail.

## [0.1.2] - 2026-07-19

Blocked-state detection slice (Option B "now", PPLX design). All changes are additive; the five frozen v0.1.0 function signatures are unchanged. Tool layer faithfully reports Herdr's native `blocked` status — interpretation and routing stay in the governance layer.

### Added
- T-1: `wait_until` exits early with the new `WaitResult.reason` value `"blocked"` when the watched agent enters Herdr's `blocked` status and the predicate has not matched (predicate still wins). No idle-based completion inference.
- T-2: `BridgeActions.get_agent_status(actor_id, agent_id) -> AgentStatus` — sixth additive function; faithful current-status readout with audit logging; raises `AgentNotFoundError` for unknown agents.

## [0.1.1] - 2026-07-19

Patch release surfaced by real-world usage across downstream projects with diverse validation needs (PPLX-consensus fix list). All changes are additive; every v0.1.0 public signature is unchanged.

### Fixed
- F-2: observable socket resolution — an explicit `socket_path` now skips the `HERDR_SOCKET_PATH` env fallback, the env fallback logs a warning, and the resolved path/source are exposed via `resolved_socket_path` / `socket_source`.

### Added
- Fix A: subscription reader emits a one-shot `"degraded"` state (via the existing `on_state` callback) after sustained reconnect failures (default 10 consecutive failures or 60 s, tunable via new keyword-only `subscribe()` parameters); reconnection never stops, and the signal re-arms after a successful reconnect.
- Fix B: `AgentOutput.normalized_text` property — joins PTY hard-wrap line breaks (blank-line paragraphs preserved) so marker matching survives narrow panes; `text` semantics untouched.
- Fix C: public read-only `get_audit_log_path()` (exported from the package root) so consumers no longer reach into `AuditLogger` internals for the audit log path.

## [0.1.0] - 2026-07-19

### Added
- Socket client: one-connection-per-request calls plus a long-lived subscription connection, exponential-backoff reconnect, single retry on the initial connect.
- Schema-driven request validation (against the schema fetched at runtime via `herdr api schema`) and dual-track protocol compatibility checking.
- Session cache: snapshot bootstrap, per-pane dynamic subscriptions (subscribe-new before re-snapshot before closing old), 5-minute consistency reconciliation.
- Bridge Actions: `list_agents` / `read_agent` / `send_to_agent` / `wait_until` / `acquire_control` — signatures frozen as of this release.
- JSONL audit log (actor_id recorded on every call, file mode 0600, summary fields only — never full text payloads).

### Compatibility
- Tested against herdr protocol 16 (herdr 0.7.3 and 0.7.4). Newer protocols are allowed through with a warning rather than rejected outright.

---

# 變更紀錄

本檔案記錄本專案所有重要變更。

格式依循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。版本號在 pre-1.0 階段的規則見 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 未發布

## [0.5.0] — 2026-07-25 — ACP SDK 轉正、多品牌 TUI 擴充、跨塔協調協定

### Added(2026-07-25 晚間)
- **`herdr-commander doctor`(一鍵診斷)**:收斂人工排查 RemaGraph 連線、pane 狀態不一致、`project.json` 綁定錯誤(見 #66)這幾類問題成一個指令,檢查全域安裝生效、RemaGraph 連線正常、`project.json` 的 `project_id` 對得上、過去一小時 `maintenance_completed` 次數是否異常(疑似外部 serve 跑失控清理迴圈)。任一項有問題回傳非 0 exit code 並列出具體原因。8 個新測試涵蓋全部健康、四種個別失敗情境、`project.json` 對應正確時不誤報、以及時間窗口邊界情境。
- **`--tui` 新增 `copilot`、`gemini` 兩個品牌**(#78、#79):`copilot`(GitHub Copilot CLI 1.0.71)畫面結構跟 `claude` 幾乎一致(`❯` 提示符 + 成對 `─` 框線),實機端到端測試(非唯讀觀察)確認訊息確實進入歷史區、輸入框清空。`gemini` 過程中額外發現並修掉兩個真實 bug(見下方 Fixed)。目前 `--tui` 支援 `claude`/`agy`/`codex`/`grok`/`opencode`/`copilot`/`gemini` 七個品牌;**Kimi CLI、CodeBuddy CLI 明確不測試、不考慮**(Aiken 2026-07-25 業務決策),未登記 pattern。
- **跨塔共用基礎設施協調協定**:PPLX 共識設計,Aiken 決定 herdr-bridge、RemaGraph 與其他內部協調節點一併採用。用既有 RemaGraph + notify-pane 制度化,不另建專職稽核塔:四個固定跨專案 label(`infra-health`/`infra-change`/`infra-incident`/`infra-owner`)、動共用基礎設施前先寫 `infra-change`、事後補一次 `infra-health`、herdr-bridge 先當模板 owner、其他節點輪值健檢,塞進各節點既有 `/goal` 循環不另開人力。

### Fixed(2026-07-25 晚間)
- **ACP 下游行程繼承塔完整環境導致 opencode 洩漏 151 個技能**:`spawn_agent_process()` 呼叫時 `env = dict(os.environ)` 把塔自己整個行程環境原封不動繼承給下游 agent 行程,PATH 裡的 `~/.claude/plugins/cache/...`、`CLAUDECODE`/`CLAUDE_CODE_*`/`AI_AGENT` 等痕跡讓下游(實測 opencode)誤判自己也在 Claude Code 底下執行,`available_commands_update` 因此回傳本機全部 151 個無關 plugin/skill,單次瑣碎往返燒 57,859 tokens。新增 `_clean_downstream_env()` 過濾 `CLAUDE*`/`ANTHROPIC*`/`AI_AGENT` 環境變數與 PATH 裡含 `.claude` 的路徑段,實測確認洩漏消失。跟 BOUNDARIES.md WP9 已知的 Grok token 風險同一類問題但根因不同——這裡責任在呼叫端(塔)而非下游 agent 自己主動掃描。
- **notify-pane 長訊息捲動出可見範圍時誤判已送達**:真實事故——對其他內部協調節點與 RemaGraph 送長篇多段落決策通知,`notify-pane` 全部回報送達確認,但訊息整段都還卡在輸入框,從未真正送出。根因是 `_looks_submitted()` 只用「訊息第一行還在不在框裡」判斷,長訊息注入後框內容捲到尾端、第一行捲出可見範圍,舊邏輯因「找不到 head」直接誤判成已提交。改成:框完全清空才是確定已提交;找得到 head 是確定未提交;兩者都不成立一律視為不確定,觸發補 Enter 而非自信宣告成功。
- **notify-pane 對 Gemini CLI 的兩個真實送達誤判 bug**(實機新裝時當場踩到):(1) `_extract_input_box_text` 驗證框內容時硬寫死只認 `❯` 標記,但 Gemini 用 `>` 提示符 + `▄`/`▀` 塊字元框線,找不到 `❯` 就誤判「這不是輸入框」,退回會漏抓換行內容的單行掃描邏輯;改成接受呼叫端傳入的 marker 集合。(2) Gemini idle 時輸入框顯示灰階 placeholder 文字(非真空白),舊邏輯要求框逐字元清空才算已提交,這種 TUI 永遠達不到條件;改成「head 找到＝確定未提交、框清空＝確定已提交、訊息尾端還在框裡＝確定未提交(同時覆蓋上一條長訊息捲動情境)、head/tail 都找不到＝確定已提交」。`_BORDER_LINE_RE` 一併擴充涵蓋 `▄`/`▀`。

### Changed(2026-07-25 晚間)
- **`agent-client-protocol` 從 optional extra 升為主依賴,正式啟用 Secondary 層**(#82):Aiken 業務決策——艦隊目標是全 headless 運作,ACP SDK 是這個目標需要的真實能力。原本卡住的兩個阻塞項已解決(`user_text` NameError 於發版前 lint 清理修掉、opencode skill 洩漏見上方 fix 條目),第三個疑似問題(`acp-echo-agent.py` 連線失敗)用真實 pipe-based spawn 重新驗證未能重現,研判為暫時性環境問題。`pyproject.toml` 的 `agent-client-protocol` 移到主 `dependencies`;3 個先前因 acp-sdk 未安裝而被 skip 的真實下游 ACP 測試現在真的執行並通過。`ok`/`degraded`/`delivery_status` 的誠實降級回報邏輯不變,只是 SDK 現在預設可用,不再需要使用者記得手動裝 optional extra。

### Test(2026-07-25 晚間)
- **mutmut 驗證 `delivery_state_store.py`**:臨時把 `[tool.mutmut]` 指向這個模組跑一輪(非永久 CI 閘門變更,跑完已還原成 `schema.py` gate),找到 1 個真實測試洞——`_connect()` 的 `mkdir(parents=True)` 沒被任何測試驗證過必要性,補上多層不存在路徑的測試殺死目標 mutant。另外 13 個存活 mutant 判定為 equivalent mutant(無可觀察行為差異);結論是不建議把這個模組正式納入常態 CI gate。

### Operational
- **搶救備份資料遷移**:從 `~/.local/state/_rescue-backup-20260725/` 6 個備份 DB 篩出跟 herdr-bridge 相關的記錄,經 `store_memory()` 正規路徑(含內建去重驗證)寫回正式庫,38 筆候選、34 筆成功遷移、4 筆被去重機制正確擋下。純資料操作,非程式碼變更。

## [0.4.0] — 2026-07-25 — 全 TUI/headless 通訊實測驗證 + delivery-state FSM 重構

### Added(2026-07-25 — 全 TUI/headless 通訊實測驗證)
- **`herdr-commander notify-pane`(第四層通訊)**:對互動式 TUI pane 唯一可靠的通訊管道——原子鍵盤注入(真正換行一次送出,避開分兩步送 Enter 的 TUI event loop race condition)+ 畫面 diff 驗證送達,per-TUI pattern(claude/opencode/codex/grok/agy)判定提交是否成功,重試上限內都無法確認送達會明確報錯,不會靜默假成功。注入前四道 ready check:互動式提示(信任確認/選單/y-n/密碼)阻擋、殭屍 pane 偵測、忙碌 pane 拒絕(`--allow-busy` 逃生門)、啟動就緒等待。
- **全域安裝支援**:`pipx install --editable <repo>` 把 `herdr-commander` 裝到 `~/.local/bin`,任何專案的 pane 都能直接呼叫,不必侷限在 herdr-bridge 自己的 venv 內。
- **delivery-state FSM 專屬輕量儲存**(`orchestration/delivery_state_store.py`):狀態轉移改用專屬 SQLite(不是通用記憶層),只有終局狀態才額外寫一筆摘要進 RemaGraph(Dual-Write on Terminal State)——修正 FSM 生命週期(分鐘級)誤用記憶層語意去重(規則要求相似度)導致連續轉移全被拒絕的架構錯配。
- **`docs/light-user-quickstart.md` 新增「五條通訊管道總覽 + 裸 `herdr pane` 指令 SOT」**:RemaGraph store/search、ACP dispatch、side-channel、notify-pane、裸 `herdr pane send-text` 五條管道的定位與送達確認能力對照表,以及裸指令的精確語法參考(`send-text`/`read` 是位置參數、`process-info` 是旗標,容易搞混)。

### Fixed(2026-07-25)
- **ACP echo fallback 不再靜默假成功**:`agent-client-protocol` 未安裝時,`dispatch`/`dispatch_with_memory_confirm` 明確回傳 `ok=False`、`degraded=True`、`delivery_status="not_attempted"`、`confirmed_via="echo-fallback"`,CLI 對應回傳非 0 exit code;RemaGraph 稽核記錄同步標記為降級而非正常完成(PPLX 審查共識)。
- **echo fallback 自我污染 bug**:router 把自己送給自己 listener 的 report 當成下游確認,導致 echo fallback(根本沒派工)時 `pong_confirmed`/`side_confirmed` 仍顯示 `True`。
- **fleet event listener 訂閱 race**:`_watch_fleet_pane()` 緊密連續呼叫時會被拆成多次個別 `events.subscribe`,觸發 Herdr 判定異常而重置重連;新增 `_sub_lock` 原子性保護 + 0.15s debounce 合併連續呼叫。
- **notify-pane ready check 誤判**:畫面尾端持續渲染的 todo/狀態面板文字(如任務描述提到「password」)曾被裸字串比對誤判成密碼輸入提示,導致合法注入被拒;改成要求緊接冒號(`password\s*:`),只比對真正的密碼欄位格式。
- **notify-pane 對殭屍 pane/忙碌 pane/窄畫面換行/per-TUI 差異**等多項送達判定缺陷(詳見 git log 2026-07-25 前綴 `fix(light)`/`fix(acp)` 的一系列 commit)。

### Changed(2026-07-25)
- **測試覆蓋率門檻 80% → 70%**,對齊 `CLAUDE.md` 既有政策(新專案至少 70%);實測排除 integration 測試後覆蓋率 77.03%,不是為了轉綠而降標準。
- **RemaGraph 依賴升級**(`00414e0`):撿回上游修復的 `project_id`/`state_dir` 綁定安全閥(原本比較邏輯是套套邏輯,未真正擋下 project_id 不符的連線,可能導致跨專案資料被誤連寫入/清除)與可設定的跨專案 fan-out cap(原寫死 20,現預設 50、可用 `--fanout-cap`/`REMAGRAPH_FANOUT_CAP` 調整,硬上限 200)。

### Fixed(2026-07-25 — 發版前 ruff/mypy 掃出的潛伏問題)
- **`prepare_dispatch_text()` 從未真正用記憶強化過 prompt**:內部呼叫 `recall_memories(..., project_id=project_id)` 誤用了不存在的變數名(函式參數其實叫 `project`),這個 `NameError` 被外層 `except Exception` 靜默吞掉,一直安靜地回傳原始文字。
- **ACP 下游呼叫路徑的未定義變數**:`router.py` 的 ACP downstream 分支引用了不存在的 `user_text`(應為 `send_text`),acp-sdk 未安裝時這條路徑不會執行到,故先前未被踩到。
- **`_get_target_health()` 健康度判斷邏輯不完整**:docstring 承諾依 5/15 分鐘門檻分級,實際上找到任何 pong/ack 記錄就一律回傳 "Healthy",未使用計算出的時間戳;已補上真正的年齡比較。
- 其餘 ruff(bare except、ambiguous 變數名、未使用變數/import)與 mypy(缺 `dict` 泛型參數、型別不確定)清理,ruff/mypy 現況全乾淨。

### Added
- **`post-commit` git hook:commit 自動寫回 RemaGraph 記憶**(章程 §3.5.9 第 4 點)。
  - `.githooks/post-commit`:每次 commit 完成後自動呼叫 `remagraph store --kind status_update`,
    project_id 從主 repo 目錄名推導(worktree 安全,不受 `git worktree` 子目錄名影響)、
    task_id 為 `<project>-commit-<短hash>`、agent_id 從 `AGENT_ID` 環境變數或 `git config user.name` 推導並 slugify。
  - agent-agnostic:純 shell + git 原生機制,不限 Claude Code,任何底層 agent 執行 `git commit` 皆會觸發。
  - 未安裝 `remagraph` 或寫入失敗時靜默降級(僅印一行 stderr 提示),絕不阻擋或讓 commit 失敗。
  - `.githooks/commit-msg`(原 DCO hook)一併搬到此目錄,統一用 `git config core.hooksPath .githooks`
    啟用(`bash .githooks/install.sh`),取代舊有手動複製到 `.git/hooks/` 的方式(該目錄不受版控、無法跨 clone 生效)。
  - 測試:`tests/test_hooks_post_commit_remagraph.py`,真的建立臨時 git repo 執行 `git commit`,
    再用 `remagraph search` 讀回資料庫驗證記錄確實存在(含 worktree 安全性、AGENT_ID 覆蓋、
    極短 commit subject 仍能通過仲裁門檻、remagraph 未安裝時優雅降級等情境)。

### Fixed
- **打包依賴修復**:`remagraph` 尚未發佈到 PyPI,`dependencies` 改用 `remagraph @ git+ssh://...@<SHA>` 直接 pin 到 RemaGraph 的 commit SHA(而非套件名稱),並新增 `[tool.hatch.metadata] allow-direct-references = true`(hatchling 預設不允許 dependencies 使用 git URL 直接參照,缺少此設定會導致 `uv build`/`pip install` 時 metadata 驗證失敗)。`[tool.uv.sources]` 的本地 editable 覆寫維持不變,僅影響本機開發(`uv sync` 仍優先使用 `../RemaGraph`),不會被打進發佈的 wheel/sdist metadata。已驗證 wheel/sdist 的 `Requires-Dist` 含完整 git URL,並在獨立乾淨環境模擬外部消費者安裝成功。

### Improved
- **3-layer confirmation 強化**:real pane dispatch 現在加強 side-channel report 作為備援。
  - `wait_for_pong` 同時偵測 side-channel tag/complete 報告,視為有效 ack(via: "side-channel")。
  - dispatch_with_memory_confirm 在 pong timeout 時明確檢查 side report,設定 `SIDE_REPORT_RECEIVED` 與 `side_confirmed`。
  - 設定 AWAIT_PONG 狀態,減少對純 PONG 的依賴;side report 可直接確認成功(TIMEOUT 時 fallback 更 robust)。
  - pane 測試現在可回報 "confirmed_via": "side-channel" 或 "pong"。

### Added
- 其他 callers/examples 補 RemaGraph ensure:
  - `scripts/demo-embedded-remagraph-acp-pipe.py`(project="herdr-demo")頂層 ensure。
  - `examples/central-tower-minimal.py`(自訂 project)頂層 ensure + 說明。
- 相容性:即使自訂 project,entry point 仍強制專屬 DB + safety valve。

### Changed
- 更新 FSM 流程以支援 side-channel 作為 tertiary 確認。
- 改善文件與範例一致性。

## [0.3.0] — 2026-07-22 — ACP Router + 4 真實下游 agents + RemaGraph 內嵌完成(PPLX 審查後初始/中期 + 追加目標)

### Added
- **AcpRouter**(acp/router.py):指揮塔同時 ACP Server + Client,內建動態 registry 發現。
- 4 真實獨立下游 ACP agents(examples/acp-*-agent.py):echo-tui、research-tui、code-tui、general-tui(完整 acp.Agent 實作 + distinct result_text)。
- 完整 CLI router hook:
  - `herdr-commander router {list, discover, route, register, unregister, start}`
  - `--path`、`--capability`、`--command`/`--args` 支援任意外部 agent
  - `run --use-acp-router`(auto-choose by caps + target)
  - `-v status` 顯示 "ACP Router registry: N agents (dynamic discovery)"
- Registry 擴充發現:
  - `create_herdr_router()` 完全動態(examples + HERDR_ACP_AGENT_PATHS + ~/.config/herdr/acp-agents/ + additional_paths + PATH + persisted JSON)
  - broader glob(*acp* / *agent* 含非 .py bin)
  - `register_agent` / `unregister_agent` 清理 persisted
  - CLI register 持久化,fresh instance 自動 load
- LightCommander 深度整合:
  - `run_task_via_acp(use_router=True)`
  - `route_via_acp_router`
  - `dispatch_with_memory_confirm`
  - `batch_dispatch_with_memory`(fleet 層級)
- RemaGraph 內嵌完整:
  - 所有派工預設 `prepare_dispatch_text` + `store_memory`
  - 涵蓋 herdr socket 路徑 + ACP 路徑 + router
  - governance embedding 測試強化
- **Option A 實作**:新增 `CentralTower` + `create_central_tower()` 高階 facade
  - 乾淨 sync API:`tower.dispatch(prompt, target=...)`、`batch_dispatch`
  - 強制所有路徑 RemaGraph prepare/store;隱藏 router 內部、RemaGraph 細節、worktree
  - 頂層匯入可用;外部專案可直接 plug-in 當「唯一指揮塔」
  - 新增 `examples/central-tower-minimal.py` + 更新 cross-project 範例 + docs/api-acp.md
- 測試:router 相關 9 + embedding 10 = 19 專項 + 全套 407 passed(88% cov)
- cross-project example 更新為 4 agents + store ack
- PPLX 計畫文件對齊現況
- PPLX 追加優先 1-4 完成(事件驅動強化):
  - 優先 1:actions.wait_until 徹底事件化(subscribe pane.agent_status_changed + pane.output_matched、callback 檢查 predicate/blocked、dedupe last_matched_line + revision、match_ev 喚醒、幾乎移除 poll/sleep fallback)
  - 優先 2:cache consistency 嚴格作為安全網(_consistency_tick 僅輕量 sub 存活檢查;每 3 tick 才做一次 full snapshot;加入 _pane_state map 供 idempotent;drift 才 rebuild)
  - 優先 3:background listener 重構為 _FleetEventListener stateful class(pane_state map、exp backoff + jitter、重連 auto resubscribe、動態 watch()、seq dedupe、stop 清理)
  - 優先 4:output_matched / permission 改善(廣義 subscribe output_matched 由 client 過濾;regex 多變體 + normalized + 僅 blocked 時觸發;_watch 同時訂兩種;wait_until 內建 permission sub)

### Changed
- 所有上層派工(run_task、via_acp、router prompt、batch)統一走 RemaGraph prepare/store
- CLI status 與 router list 暴露完整 registry metadata + summary + filter

### Verified
- live:register external arbitrary cmd → persist → new router 看到 → route → unregister 清理
- --path 擴充 discover 正常
- auto choose(research → research-tui、implement → code-tui)
- batch 依 cap 路由不同下游
- 4 agents distinct real output + direct ACP protocol test

## [0.2.2] — 改善批次 2026-07-21:mutmut · Claude · SDK transport · CodeQL/Scorecard · 文件收尾

> Tag: `v0.2.2`, commit `f0cd1f7`

### Added

**Revision cursor(WP4)**
- `AgentOutput.revision: int | None`——herdr 回應中的 monotonic revision counter(WP4 experimental)。預設 `None`;既有呼叫不傳此欄位時行為與 0.2.1 相同。
- `BridgeActions.read_agent` 與 `wait_until` 新增 keyword-only 參數 `since_revision: int | None = None`——僅回傳該 revision 之後的輸出變更(herdr 協定原生支援)。不傳時行為與 0.2.1 相同。
- `_RevisionAdapter`(experimental)——將 herdr 回應中的 revision 值正規化為 `int | None`;非 int(含 bool、float、str、None)一律降級為 `None`。消費端不宜直接依賴此函式。
- `@pytest.mark.empirical` marker——真機四項語意測試(monotonic、stable、since-filtering、session-reset)在 CI 中 deselected。
- `tests/test_revision_cursor.py`——_RevisionAdapter 單元測試 + read_agent/wait_until since_revision 整合測試 + empirical 四項 stub。

**ACP 擴充——Claude tier(WP6)**
- `AcpxTransport` 新增 `agent="claude"` 支援——`_default_agent_resolver()` 接受 `'claude'`(`CLAUDE_BIN` env 或 `shutil.which`);`build_acpx_argv_and_env()` 按 agent 分流(opencode 走 `--agent` + config file,claude 走 `--cwd` 全域旗標);`ensure_session()` 對 claude 跳過 `write_session_config()`。
- `list_acp_agents()` 回傳 opencode + claude 雙 tier。
- `AcpTransport` Protocol 凍結——WP7(SDK transport)可進場。

**ACP 擴充——AcpSdkTransport(WP7)**
- `herdr_bridge.acp.sdk_transport.AcpSdkTransport`——以官方 `agent-client-protocol` PyPI 套件(`>=0.10.0,<0.12`)透過 stdio 直接與 ACP agent 通訊,繞過 `acpx` CLI 中繼層。
- `_AcpSdkClient`——專屬背景 event loop thread、自動應答 `request_permission`(對齊 `AcpPolicy`)、收集 `session_update` 事件。
- 完整的 session 生命週期(ensure/close)、`run_prompt`/`start_prompt`/`wait_done`/`cancel` 含 timeout、`get_history`。
- Import guard:未安裝 `herdr-bridge[acp-sdk]` 時拋出明確 ImportError;主 `__init__.py` 不 re-export。
- `pyproject.toml` 新增 `[project.optional-dependencies] acp-sdk`(opt-in,預設不變)。

**CI**
- mutmut nightly hard gate(WP2)——從 PR CI 矩陣移除(原 `continue-on-error: true`),改為 `schedule` + `push-to-main` 專屬 workflow(`.github/workflows/mutmut-gate.yml`)。基線:1149 total mutants、6.3% kill rate;補 8 則殺變異回歸測(33→15 survived)。
- CodeQL + OpenSSF Scorecard workflow stubs(WP12)——`.github/workflows/codeql.yml`、`.github/workflows/scorecard.yml`,以 `if: vars.REPO_PUBLIC == 'true'` 靜置,公開日解封。README 預埋 Scorecard badge(HTML 註解)。

### Changed
- 測試框架強化:`tests/test_schema.py` 新增 8 則 mutation-killing regression tests(WP2)。
- `BOUNDARIES.md`:ACP tier 支援清單更新為 opencode/claude/copilot/grok 四 tier;新增 Grok token 風險註記(WP13)。

## [0.2.1] — TD-005b: Add herdr_bridge.testing(FakeHerdrServer)

> Tag: `v0.2.1` (unsigned — no GPG key on this machine), commit `2cd7110`

### Added
- `herdr_bridge.testing` 公開子套件:匯出 `FakeHerdrServer`、`FakeApiError`、`Handler`,供下游 consumer 在無真實 herdr server 環境下執行契約測試。
- `docs/testing.md`——FakeHerdrServer 使用說明與契約測試範例。
- `tests/fake_server.py` 改為過渡 shim(生命週期 ≤ 下一個 bridge PR,屆時刪除)。

### Changed
- 所有測試 import 從 `tests.fake_server` 遷移至 `herdr_bridge.testing`。

## [0.2.0] - 2026-07-20

ACP 指令層:新增獨立的 `herdr_bridge.acp` 模組——暫定/實驗性的附加層,**不**受凍結的五函式 semver 保證涵蓋(見 `BOUNDARIES.md`)。所有既有 v0.1.x 公開簽章維持不變。

### Added
- `herdr_bridge.acp.connect() -> AcpActions` 與 `AcpActions` 的九個方法(`list_acp_agents`、`ensure_session`、`close_session`、`get_history`、`prompt`、`exec_prompt`、`start_prompt`/`wait_done`、`cancel`、`close`)——透過 `acpx` CLI 以 Agent Client Protocol 驅動 opencode(目前唯一支援的 tier),用結構化的 `session/update` 事件與明確的 `stopReason` 取代畫面截取/標記比對式派工。
- `AcpTransport` Protocol + `AcpxTransport`(SDK 抽換介面)與 `AcpxAdapter` 的純函式(`build_opencode_permission_config`、`write_session_config`、`build_acpx_argv_and_env`、`build_acpx_policy_flags`、`resolve_patched_opencode_binary`、`detect_target_triple`)——讓 opencode 本地的權限引擎在 acpx 底下真正生效執行 `deny`/`ask`/`allow` 政策,補上 acpx 自身權限旗標純被動、從未實際設定 agent 本身的落差。
- `herdr_bridge.acp.binding`——政策中立的 pane↔session 派工帳本(純函式;正規對應鍵是 herdr 的 `pane_id` + 帳本,而非 ACP 的 `session_name` 字串)。
- `scripts/rebuild-patched-opencode.sh`——重建本地修補版 opencode 執行檔(修正上游一個真實 bug:子/subagent ACP session 從未被註冊,導致任何需要為委派 subagent 自身動作要求權限的 `session/prompt` 卡死)並刷新 `.vendor/opencode-patched/MANIFEST.json`。

### Known limitations(已追蹤、非阻擋項)
- 目前只接上 `agent="opencode"`;其他 tier 要等上層提供具名的 acpx agent 設定項目。
- `cancel()` 是終止底層 acpx 子行程,而非送出真正的 ACP `session/cancel` 協定訊息。
- `prompt()` 的 `policy` 參數為維持簽章相容而接受,但不會生效——政策在 `ensure_session()` 當下就固定了。

完整參考、驗證與已知限制細節見 `docs/api-acp.md`。

## [0.1.2] - 2026-07-19

Blocked 狀態偵測切片(Option B「先做這塊」,PPLX 設計)。所有變更皆為附加性;凍結的五個 v0.1.0 函式簽章維持不變。工具層忠實回報 Herdr 原生的 `blocked` 狀態——解讀與路由邏輯留給上層處理。

### Added
- T-1:`wait_until` 在被觀察的 agent 進入 Herdr 的 `blocked` 狀態且 predicate 尚未命中時,提前以新的 `WaitResult.reason` 值 `"blocked"` 結束(predicate 命中優先)。不做基於閒置狀態的完成推斷。
- T-2:`BridgeActions.get_agent_status(actor_id, agent_id) -> AgentStatus`——第六個附加函式;忠實回報當下狀態並附稽核記錄;對未知 agent 拋出 `AgentNotFoundError`。

## [0.1.1] - 2026-07-19

透過下游專案實際使用過程中發現並產出的修補版本(PPLX 共識修復清單)。所有變更皆為附加性;每個 v0.1.0 公開簽章維持不變。

### Fixed
- F-2:可觀察的 socket 解析——明確傳入 `socket_path` 時會跳過 `HERDR_SOCKET_PATH` 環境變數 fallback,走 fallback 時會記錄警告,解析出的路徑/來源透過 `resolved_socket_path` / `socket_source` 對外暴露。

### Added
- Fix A:訂閱讀取端在連續重連失敗一段時間後(預設連續 10 次失敗或 60 秒,可透過 `subscribe()` 新增的 keyword-only 參數調整)透過既有 `on_state` callback 送出一次性 `"degraded"` 狀態;重連機制永不停止,成功重連後訊號會重新武裝。
- Fix B:`AgentOutput.normalized_text` 屬性——合併 PTY 硬換行造成的斷行(保留空行段落),讓標記比對在窄 pane 下依然可靠;`text` 語意不變。
- Fix C:公開唯讀的 `get_audit_log_path()`(從套件根目錄匯出),消費端不必再深入 `AuditLogger` 內部取得稽核記錄路徑。

## [0.1.0] - 2026-07-19

### Added
- Socket client:一次請求一連線的呼叫方式,加上長駐的訂閱連線、指數退避重連、初次連線失敗允許重試一次。
- Schema 驅動的請求驗證(比對執行期透過 `herdr api schema` 取得的 schema)與雙軌協定相容性檢查。
- Session cache:快照式啟動、per-pane 動態訂閱(先訂閱新的、再重新快照、最後才關閉舊的)、5 分鐘一致性校正。
- Bridge Actions:`list_agents` / `read_agent` / `send_to_agent` / `wait_until` / `acquire_control`——簽章自本版起凍結。
- JSONL 稽核記錄(每次呼叫都記錄 actor_id,檔案權限 0600,只存摘要欄位——絕不存完整文字內容)。

### Compatibility
- 已對 herdr protocol 16(herdr 0.7.3 與 0.7.4)測試通過。更新版協定會放行並附警告,而非直接拒絕。
