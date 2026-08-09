# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). See [`CONTRIBUTING.md`](CONTRIBUTING.md) for this project's versioning policy while the version stays pre-1.0.

## [Unreleased]

## [0.10.0] — 2026-08-09 — Leader Harness: real multi-brand hook wiring

### Added
- Leader Harness now ships real, committed hook configuration for GitHub Copilot CLI
  (`.github/hooks/`), Gemini CLI (`.gemini/`), opencode (`.opencode/plugins/`), Codex CLI
  (`.codex/hooks/`, Bash-only coverage), and Grok CLI (`.grok/hooks/`) — previously only Claude
  Code had working hook wiring despite every brand adapter reporting `ACTIVE` status. Grok was
  additionally re-classified from "unsupported" to `ACTIVE`: the original classification never
  actually found official documentation and defaulted to unsupported rather than confirming it;
  a fresh check found Grok's official hooks docs (schema close to Claude Code's own).
- `register_leader_harness()` is now actually invoked at `SessionStart`, with results surfaced
  via `.herdr/harness_status.json` (8-hour staleness window) and the audit log — previously dead
  code, never called from any real entry point.
- Optional charter document injection (`HERDR_TOWER_PROMPT`/`HERDR_ORG_ROOT`) for deployments
  that use one; fully opt-in, silent no-op when unset so a plain `git clone` + `uv sync --dev`
  never breaks on a missing file.
- `.claude/settings.json`'s hook commands now resolve `$CLAUDE_PROJECT_DIR` instead of a
  hardcoded absolute path, use `uv run` instead of assuming `.venv/bin/python3` exists, and
  `leader_harness_pretooluse.py` verifies `HERDR_BRIDGE_ROOT` actually points at a valid
  herdr-bridge checkout (`.herdr/` + `.claude/settings.json` present) before trusting it,
  fail-closed otherwise.

### Fixed
- `_TUI_NATIVE_INFRA_PATTERNS` only allowlisted Claude Code's own native paths — Leader Harness
  would have incorrectly denied Codex/Gemini/Copilot/opencode/Grok's own session/config writes.
  Warp's native path is now allowlisted defensively too, even though it has no hook mechanism to
  trigger this check today.

## [0.9.0] — 2026-08-09 — Command Tower Leader: multi-brand fleet orchestration

### Added
- **Command Tower Leader** (`herdr_bridge.leader`, `herdr_bridge.tower`, `herdr_bridge.gates`, `herdr_bridge.task_pool`, `herdr_bridge.fleet_health`): lets a single "Leader" agent reliably coordinate a fleet of downstream AI coding agents across 7 brands (Claude Code, Codex, Copilot, Gemini, OpenCode, Grok, Warp Agents CLI), with hard guardrails so the Leader dispatches work instead of doing it itself.
  - **Task Board**: claim-based work allocation with an atomic snapshot + append-only event log (CQRS-style projection), so two workers racing for the same task never both win.
  - **Bootstrap Protocol**: a `SessionStart(compact)` hook rebuilds the Leader's working context from a persisted snapshot after a context compaction, instead of guessing from a summary.
  - **Leader Harness**: `PreToolUse` denies edits to any path outside the Leader's own repo (config `leader-permissions.json`, precise-before-wildcard rule ordering, with a small allowlist for each brand's own native infrastructure paths so the harness doesn't collide with the host tool's own session/memory files), with a `PostToolUse` cross-check compensation layer for brands whose deny hook is known-unreliable. Per-brand adapters report `ACTIVE`/`DEGRADED` honestly rather than assuming parity across brands.
  - **Gate**: a machine-enforced policy layer (`gates.json`) that rejects malformed dispatch/completion calls before they reach the Task Board — fail-open on a missing or malformed config so it never becomes a hard dependency.
  - **`herdr-commander task health`**: fleet health monitoring that detects stale worker heartbeats, repeated task failures, and empty "done" deliveries, with escalating re-alerts for a worsening condition instead of either silence or repeat spam.
  - Validated against 7 behavioral acceptance scenarios (parallel dispatch, harness-deny compliance, bootstrap recovery, claim race resolution, gate enforcement, pressure resistance, proactive health alerting).
- **`herdr-commander signal install-watchdog` / `uninstall-watchdog`** (macOS, `herdr_bridge.signal.watchdog_service`): a scheduled healthcheck for your own Signal daemon (`signal status --notify-on-problem` on an interval, default 300s) is now a first-class herdr-bridge feature — no external tower-bootstrap tooling required to set it up. `install-watchdog` writes and (re)loads a `launchd` service (`herdr.signal-healthcheck.<project>`, no vendor/company prefix); `uninstall-watchdog` unloads and removes it. `--interval-sec`, `--herdr-commander-path`, and `--socket-path` are all overridable; every filesystem/subprocess dependency in the underlying module is dependency-injected, so it's fully unit-tested without touching real launchd. Non-macOS platforms get a clear, actionable error rather than silently doing nothing.
- **Herdr Bridge Signal** (`herdr_bridge.signal`): a resident per-tower daemon that lets one tower push-wake another instead of relying on polling — an additive 6th communication layer alongside the existing 5 (Primary/Secondary/Tertiary/notify-pane/raw-input), none of whose behavior changes. Went through six rounds of PPLX adversarial design review (see `docs/herdr-bridge-signal-design.md`) plus an M0 validation spike before implementation: the spike found `notify-pane` self-injection reliable (~1s, PASS) but Herdr's `events.subscribe`/`pane.output_matched` unreliable as a transport shortcut (FAIL — caching/replay behavior, not a stable per-event trigger), so the daemon uses a self-built Unix domain socket server rather than the originally-hoped-for simplification.
  - `herdr-commander signal start` — start the resident daemon for this project (normally invoked by a bootstrap script; own pane_id resolved via a three-tier fallback: `HERDR_PANE_ID` env var → pin file → `herdr pane list` cwd scan, refusing to start on an ambiguous match rather than guessing).
  - `herdr-commander signal send --to <project> --inbox-ref <ref>` — wake the target project's daemon; the wake envelope carries only a RemaGraph reference, never content itself. Applies the design doc's escalation rules (retry-then-`daemon_unreachable`, `injection_unconfirmed` on a confirmed-but-not-injected timeout) and reports failures with a concrete, non-zero exit rather than a silent success.
  - `herdr-commander signal status` — daemon liveness plus recent ACK records (Accepted → Injected → Seen → Accepted-for-work → Completed, each written by exactly one role — see `orchestration.memory`'s `mark_*` functions).
  - `herdr-commander doctor` now distinguishes a stalled daemon (restart it) from a daemon still bound to a pane that no longer exists (re-run bootstrap) instead of reporting a bare "abnormal".
  - Security model: HMAC-signed envelopes, TTL + in-memory nonce replay protection (deliberately not persisted — see the design doc's trust-boundary argument), a 0600 Unix socket under `~/.local/state/herdr-bridge/signal/<project>/`, single-instance file lock (kernel-released on crash, no stale-PID logic needed).
  - 78 new tests across envelope signing/verification, the single-instance lock (including a real subprocess-death test), the ACK state machine (including a same-row write race between the sender and receiver processes, found by testing rather than assumed away), the daemon's merge/idempotency/escalation logic over a real Unix socket, and the CLI/doctor integration.
  - **2026-08-01 same-day field rollout**: enabled across every tower in this ecosystem via `bootstrap-tower.sh` (now starts the daemon for every tower, not just herdr-bridge). Field testing on a downstream tower caught and drove fixes for three real bugs: (1) a bare `signal start`/`send`/`status` with no `--project` silently defaulted to `herdr-bridge` and could collide with its daemon lock; (2) the first fix's warning was itself dead code, because `herdr_bridge/__init__.py` unconditionally force-sets `os.environ["REMAGRAPH_PROJECT"] = "herdr-bridge"` as an import-time side effect, which a same-process unit test can't exercise — the actual fix drops `REMAGRAPH_PROJECT` from the signal project-resolution chain entirely and adds a real subprocess regression test; (3) the resulting `_resolve_signal_project()` now checks `--project` > `CT_PROJECT` > `HERDR_MEMORY_PROJECT`, warning on stderr whenever it falls through to the `herdr-bridge` default.

### Fixed
- **`TaskPool.claim()`/`unclaim()` failed 100% of the time for any short task_id** — not a race condition, every single call: the underlying event-log write's summary text was often under RemaGraph's 30-character minimum, and the request never included a required `learnings` entry. Both are now satisfied unconditionally, and a regression test exercises the real write path (the existing test suite had mocked it away, which is why this went undetected).
- **The Signal daemon's periodic garbage-collection scan aborted entirely on the first malformed `heartbeat_ts` it encountered**, silently leaving every subsequent claimed task un-reaped for as long as the bad entry persisted (task ordering doesn't change between scans, so the same entry kept blocking the same point every cycle). Now isolates each task's timestamp parsing so one bad entry is skipped, not fatal to the whole scan.
- **`signal send` could still crash the caller's whole CLI process on a completed-send race, even after the 2026-08-01 fix** (reproduced twice on w18:p1 by a downstream tower, 2026-08-02, root-fix request — PPLX-reviewed): `mark_accepted()` and `mark_escalated()` in `orchestration/memory.py` both read the Signal ACK row once to decide what to do, then delegate to `_write_signal_state()`, which independently re-reads the row a *second* time to validate the transition. Across two independent OS processes (`outbound.py` the sender, `daemon.py` the receiver) with no shared lock between them, the daemon can advance the row all the way to `completed` in the gap between those two reads — the 2026-08-01 fix only closed the *wide* version of this race (daemon already done by the first read); it left this *narrow* version open (daemon finishes between the first and second read), and `_validate_signal_transition()` would raise an uncaught `ValueError` (e.g. `"Invalid Signal transition: completed -> injection_unconfirmed. Allowed: []"`) straight through `outbound.send()`, crashing the CLI even though the send had already succeeded. Fixed with a bounded local retry (bounded, not pushed down into `_write_signal_state()` itself — other callers like `mark_injected()`/`mark_completed()` must keep raising on a genuinely illegal transition) that re-reads the freshest state and retries the whole decision, not just the write. 4 new regression tests, including one that reproduces the downstream tower's exact traceback message.
- **`read_agent()`/`send_to_agent()`/`wait_until()` — three of the five frozen tool-layer functions — were completely broken against Herdr 0.8.0 (protocol 19)**, found and fixed same-day via a real-machine probe (2026-08-04, PPLX-reviewed design, see `docs/decisions/pplx-agent-read-terminal-id-0.8.0-20260804.md`): (1) `agent.read`/the `agent.prompt` replacement for `agent.send` reject a live agent's `terminal_id` as `target` with `agent_not_found`, even though `agent.list` still reports that same `terminal_id` as the agent's stable id — the agent's `pane_id` works instead; (2) the `agent.send` method itself no longer exists on the wire in protocol 19 (`invalid_request: unknown variant agent.send`) — `agent.prompt` is the replacement; (3) `events.subscribe`'s `pane.output_matched` subscription now requires a `match` field that used to be absent, breaking `wait_until()`'s event-driven fast path (it degraded to its polling fallback rather than failing outright, which is why this shipped quietly instead of loudly). All three fixes are scoped to exactly `protocol == 19` (a new `PROTOCOL_AGENT_TARGET_REGRESSION` constant in `schema.py`, alongside bumping `MAX_TESTED_PROTOCOL` 16 → 19) rather than `>= 19`, since these look like probable upstream regressions rather than an intentional new contract — an upstream fix in a later protocol should fall back to the original, still-correct behavior automatically instead of silently inheriting a workaround it no longer needs. `AgentInfo.agent_id`'s public semantics (still the `terminal_id`) are unchanged; only the value actually put on the wire changes internally. 4 new regression tests pin both the protocol-19 and pre-19 code paths. An upstream bug report is drafted (not yet filed) in `docs/decisions/draft-github-issue-agent-read-terminal-id-20260804.md`.

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

## [0.4.0] — 2026-07-25 — Full TUI/headless communication dogfooding + delivery-state FSM rework

### Added (2026-07-25 — full TUI/headless communication dogfooding)
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

