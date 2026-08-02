# herdr-bridge

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/herdr-bridge.svg)](https://pypi.org/project/herdr-bridge/)
[![CI](https://github.com/aiken884/herdr-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/aiken884/herdr-bridge/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-support-F16061?logo=ko-fi&logoColor=white)](https://ko-fi.com/aikenlin)
[![PayPal](https://img.shields.io/badge/PayPal-donate-00457C?logo=paypal&logoColor=white)](https://paypal.me/aikenlin)

**Your AI command tower — for developers who reach for AI every once in a while.**

Every so often you need AI to take on a piece of development work, but you don't want to babysit which assistant does what, or figure out afterward whether the job actually got done right. herdr-bridge takes all of that off your hands.

Just say what you need in one sentence. It breaks the task down, hands it to whichever AI assistant fits best, tracks progress while it runs, and checks the result for you.

No juggling windows, no tracking progress by hand, no complicated setup to learn first.

## About

herdr-bridge is a semantic coordination layer built on top of [Herdr](https://herdr.dev), letting a single command tower reliably direct multiple brands of AI coding agent (Claude Code, Codex, Grok, OpenCode, Copilot, Gemini, and more) running side by side in your terminal. It's the missing link between "I have several AI assistants open" and "I told one of them what I need, and it got handled."

At its core, herdr-bridge is two things:
- **A tool layer** (`herdr_bridge`, the Python library): five frozen, typed functions — `list_agents`, `read_agent`, `send_to_agent`, `wait_until`, `acquire_control` — wrapping Herdr's socket API with a local eventually-consistent state cache and a full audit trail. No scheduling, no rule engine, no hidden policy — just a stable surface to automate against.
- **A light command tower** (`herdr-commander`, the CLI): a ready-to-use layer on top of the tool layer for occasional users — say a task in one sentence, and it picks the right agent, dispatches, tracks progress, and reports back in plain language.

Built-in memory (recall/store across tasks and agents), multi-layer delivery confirmation (so "sent" actually means "received"), and support for both headless (ACP) and interactive TUI agents are included out of the box — see the sections below for the full picture.

> **Current status**: ACP Router + real downstream agents + embedded Herdr Bridge Memory + CLI router are all complete (4 agents: code/research/echo/general-tui, with dynamic discovery + registration, 407 tests). `herdr-commander run` / `router` / `status` are ready to use.

<details>
<summary>Table of contents</summary>

- [For occasional users: three steps to get started](#for-occasional-users-three-steps-to-get-started)
- [Why occasional users need it even more](#why-occasional-users-need-it-even-more)
- [Install](#install)
- [Quickstart](#quickstart)
- [ACP command plane](#acp-command-plane-herdr_bridgeacp--provisional)
- [Limitations](#limitations)
- [Reserved fields](#reserved-fields)
- [Compatibility](#compatibility)
- [Quality assurance](#quality-assurance)
- [Support](#support)
- [License](#license)
- [Project docs](#project-docs)

</details>

## For occasional users: three steps to get started

```bash
pip install herdr-bridge   # requires Herdr first: https://herdr.dev
herdr-commander start      # check your environment (or bash scripts/commander-start.sh --sandbox)
herdr-commander run        # run your first task: a thumbnail function + unit tests
```

See [`docs/light-user-quickstart.md`](docs/light-user-quickstart.md) for details.

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/aiken884/herdr-bridge/badge)](https://scorecard.dev/viewer/?uri=github.com/aiken884/herdr-bridge)

## Why occasional users need it even more

When you're not living inside multiple AI windows every day, every time you want AI to handle a piece of development work you run into the same friction all over again:
- Which AI should I open?
- How do I explain the task clearly enough?
- How do I know it's actually done, not just finished running?

herdr-bridge absorbs that coordination cost. You say what you want, and the command tower handles the rest.

**This isn't built for people who spend all day fine-tuning a fleet of agents** — those folks are already having a great time on their own. We're building for the people who only need this once in a while.

Herdr already exposes the primitives this needs: a local Unix socket API to list panes and agents, read pane output, send keystrokes, and subscribe to status-change events. What it does not give you is a stable, typed, documented surface to build automation against — the socket protocol is server-owned and evolves with Herdr itself.

herdr-bridge is that missing layer. It wraps the socket API in five functions — `list_agents`, `read_agent`, `send_to_agent`, `wait_until`, `acquire_control` — with frozen call signatures, a local eventually-consistent cache of session state, and an audit trail of who called what. It is deliberately just the tool layer: no scheduling, no rule engine, no multi-tenant policy. Those belong in a governance layer built on top, which is why every call already carries `actor_id`, `priority`, and `mode` fields even though this library does not act on them yet (see "Reserved fields" below).

### Not the same thing as `pyherdr`

PyPI also hosts [`pyherdr`](https://pypi.org/project/pyherdr/), a pure-Python port/fork of the Herdr multiplexer itself. The two do different jobs: `pyherdr` reimplements the multiplexer; herdr-bridge is a client library for the official Rust Herdr's socket API — it assumes you run upstream Herdr and gives your automation a stable, audited call surface on top of it. If you want a Python multiplexer, use `pyherdr`; if you want to script agents running under official Herdr, that is what this library is for.

## Install

```bash
uv add herdr-bridge
# or
pip install herdr-bridge
```

Requires Python 3.11+ and a running local [Herdr](https://herdr.dev) installation (0.7.3+, socket protocol 16) reachable via the `herdr` CLI or the `HERDR_SOCKET_PATH` environment variable. herdr-bridge talks to Herdr over a Unix domain socket, so it runs on macOS and Linux; there is no Windows support.

## Quickstart

```python
from herdr_bridge import connect

actions = connect()
ACTOR = "rule:demo-script"  # "<category>:<name>" — see docs/api.md

# who's out there?
agents = actions.list_agents(ACTOR)
for agent in agents:
    print(agent.agent_id, agent.brand, agent.status)

target = agents[0].agent_id

# what has it printed so far?
output = actions.read_agent(ACTOR, target)
print(output.text[-500:])

# tell it to do something
actions.send_to_agent(ACTOR, target, "run the test suite")

# wait until its output looks done, or give up after 2 minutes
result = actions.wait_until(
    ACTOR, target,
    predicate=lambda out: "PASSED" in out.text or "FAILED" in out.text,
    timeout_sec=120,
)
print(result.success, result.reason)  # reason: predicate | timeout | agent_gone | error | blocked
```

`connect()` auto-detects the local Herdr socket, checks protocol compatibility, and starts the session cache. See [`examples/failed_forwarder.py`](examples/failed_forwarder.py) for a complete governance-rule-shaped example (wait for a test failure, forward the context to a reviewer agent), and [`docs/api.md`](docs/api.md) for the full reference.

## ACP command plane (`herdr_bridge.acp`) — provisional

The five functions above are the "watch and coordinate over Herdr panes" layer. `herdr_bridge.acp` is a second, separate module that drives opencode directly over the [Agent Client Protocol](https://agentclientprotocol.com) (via the [`acpx`](https://www.npmjs.com/package/acpx) CLI) instead of screen-scraping a pane: structured `session/update` events and an explicit `stopReason` in place of marker-grepping.

```python
from herdr_bridge.acp import connect, AcpPolicy

acp = connect()
ACTOR = "rule:demo-script"

acp.ensure_session(ACTOR, "opencode", workdir="/path/to/repo", session_name="s1",
                   policy=AcpPolicy(mode="approve-reads"))
result = acp.prompt(ACTOR, "s1", "fix the failing test")
print(result.reason, result.stop_reason)  # reason: stop | timeout | error | canceled
acp.close_session(ACTOR, "s1")
```

**This module is provisional/experimental** and explicitly **not** covered by the frozen five-function semver guarantee above — see [`BOUNDARIES.md`](BOUNDARIES.md). Upstream `acpx` is alpha, and this module currently depends on a locally-built opencode fork carrying a fix for a real upstream bug (child/subagent ACP sessions were never registered, hanging any prompt that needed to ask permission for a delegated subagent's own action — [anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902), pending upstream review). Only the `opencode` agent tier is wired up today. Full reference and known limitations: [`docs/api-acp.md`](docs/api-acp.md).

## Status semantics caveat

`AgentInfo.status` for Claude Code panes comes from Herdr's screen-content detection, not a structured signal from the agent process itself — **`idle` does not reliably mean "done."** M0 real-machine testing (N=8 trials, a single Claude Code version, a single injection method — a preliminary sample, not a reliability benchmark) found `working`/`idle` transitions detected correctly in all 8 trials, but a "waiting for your confirmation" prompt (e.g. a trust-folder dialog) was also reported as `idle` — a confirmed false-idle case.

Because of this, `wait_until` never trusts a status event by itself: on every `pane_agent_status_changed` event it re-reads the pane and re-evaluates your predicate against the actual text, and only a matching predicate (or a timeout, the agent disappearing, or the agent entering the `blocked` state) ends the wait. It returns a `WaitResult` rather than raising, with a stable five-value `reason` (`predicate` / `timeout` / `agent_gone` / `error` / `blocked`). The `blocked` reason (added in 0.1.2) exits early when Herdr detects the agent is waiting for external input — the caller doesn't burn `timeout_sec` staring at a stuck agent.

## Limitations

- **`acquire_control(mode="control")` is a single-process mutex**, not a Herdr-server-side lock. It prevents two callers inside the same bridge process from fighting over a pane; two independent bridge processes on the same machine are invisible to each other.
- **The audit log grows without bound.** It's a JSONL file (default `~/.local/state/herdr-bridge/audit.jsonl`, file mode `0600`, directory `0700`) recording call summaries — never full text payloads — and herdr-bridge does not rotate or cap it; point `logrotate` or similar at it if that matters for your deployment.
- **`agent_id` (Herdr's `terminal_id`) is only valid for one Herdr server run.** Confirmed on a real-machine restart test: `terminal_id` was reassigned across the restart while `pane_id` and the agent's own session identity (`AgentInfo.session_ref`) stayed stable. Don't persist `agent_id` across a Herdr server restart — re-resolve identity via `session_ref` instead.
- **`AgentInfo.status` is eventually consistent**, backed by a local cache that reconciles a full snapshot every 5 minutes as an upper bound on drift; it is not a live-push guarantee for every intermediate transition.
- **herdr had a confirmed upstream restore bug (observed on 0.7.4)**: after a Herdr session server restart, panes/agents restored from `session.snapshot` show up in listings, but reading them (`agent.read`/`pane.read`) fails with `agent_not_found`/`pane_not_found`. herdr-bridge cannot work around this at the client level — it raises `AgentNotFoundError` as designed, for your own degradation path to handle. Newly created panes after the restart are unaffected. **Update (2026-08-02)**: a third party independently filed and fixed this upstream ([herdrdev/herdr#2065](https://github.com/herdrdev/herdr/issues/2065), fix merged in [#2088](https://github.com/herdrdev/herdr/pull/2088) on 2026-07-30), but as of the latest tagged release (0.7.5, 2026-07-21) and the latest preview build (2026-07-29) — both predating the merge — the fix has not shipped yet. Keep treating this as a live limitation until a newer release confirms the fix.

## Reserved fields

Every call takes an `actor_id`; `send_to_agent` takes a `priority`; `acquire_control` takes a `mode`. herdr-bridge does not enforce, rank, or gate anything based on these today — it only records them (plus an `actor_id_status` audit grade for malformed values). They exist so a future governance layer — a rule engine, priority scheduler, or multi-caller policy — can be built without changing these frozen signatures. Full format, value ranges, and named anchors: [`docs/api.md`](docs/api.md).

## Compatibility

- Tested against Herdr 0.7.3 and 0.7.4, socket protocol 16 (85 methods).
- `connect()` rejects servers older than protocol 16 outright; servers reporting a newer protocol get a warning and continue (`protocol_compat="untested"`) rather than a hard failure.
- Herdr itself is pre-1.0 and owns its socket protocol; herdr-bridge may need patch releases to track upstream changes independent of anything on this library's own side.
- The five public function signatures (`list_agents`, `read_agent`, `send_to_agent`, `wait_until`, `acquire_control`) are **frozen as of 0.1.0**. The `0.x` version number reflects Herdr's own pre-1.0 maturity, not instability of this library's interface.
- **0.1.1 (additive, all v0.1.0 signatures unchanged)** — surfaced from real-world usage of this library in downstream projects: `AgentOutput.normalized_text` (joins PTY hard-wraps so a marker split across a wrapped line still matches), `get_audit_log_path()` (public read-only audit-log path so consumers stop reaching into internals), `resolved_socket_path` / `socket_source` on the object `connect()` returns (assert you connected to the intended session), and a one-shot `"degraded"` subscription state emitted after sustained reconnect failures. See [`CHANGELOG.md`](CHANGELOG.md).
- **0.1.2 (additive, all prior signatures unchanged)** — `get_agent_status()` (sixth public method, Herdr-native status query with no semantic interpretation), `wait_until` now exits early with `reason="blocked"` when the agent enters Herdr's blocked state (waiting for approval/input) and the predicate hasn't matched yet. See [`CHANGELOG.md`](CHANGELOG.md).
- **0.2.0 (additive, all v0.1.x signatures unchanged)** — the `herdr_bridge.acp` command plane described above: `connect()`/`AcpActions`'s nine methods, driving opencode over ACP via `acpx`. This is a separate, provisional/experimental surface (see [`BOUNDARIES.md`](BOUNDARIES.md)) — it does not affect the frozen five-function guarantee. See [`CHANGELOG.md`](CHANGELOG.md) and [`docs/api-acp.md`](docs/api-acp.md).
- **0.2.1 (additive, all prior signatures unchanged)** — `herdr_bridge.testing` public subpackage: `FakeHerdrServer` for downstream consumer contract testing without a real Herdr install. See [`CHANGELOG.md`](CHANGELOG.md) and [`docs/testing.md`](docs/testing.md).
- **0.2.2 (additive, all prior signatures unchanged)** — `AgentOutput.revision` monotonic counter + `since_revision` keyword-only filter (experimental); ACP `AcpxTransport` extended to claude tier; `AcpSdkTransport` as alternative ACP transport via official `agent-client-protocol` Python SDK (opt-in); CI/QA hardening (mutmut nightly gate, CodeQL/Scorecard stubs). See [`CHANGELOG.md`](CHANGELOG.md).
- **0.3.0 (additive, all prior signatures unchanged)** — `AcpRouter` (`herdr_bridge.acp.router`): the tower acting as both ACP server and client, with dynamic agent registry discovery, four real independent downstream ACP agents, `herdr-commander router {list,discover,route,register,unregister,start}` CLI, and embedded Herdr Bridge Memory coordination across the whole dispatch path. See [`CHANGELOG.md`](CHANGELOG.md).
- **0.4.0 (additive, all prior signatures unchanged)** — `herdr-commander notify-pane`: the reliable channel for interactive TUI panes (atomic keystroke injection + screen-diff delivery confirmation, per-TUI submit detection, busy/zombie/startup-race guards); `herdr-commander` can now be installed globally via `pipx install --editable <repo>` so any pane on the machine — not just this project's own venvs — can reach any other pane; delivery-state FSM dedicated storage. See [`CHANGELOG.md`](CHANGELOG.md).
- **0.5.0 (additive, all prior signatures unchanged)** — `agent-client-protocol` promoted from optional extra to a main dependency (Secondary/ACP layer available by default); `notify-pane` `--tui` gained `copilot` and `gemini` brand support; `herdr-commander doctor` one-shot diagnostic. See [`CHANGELOG.md`](CHANGELOG.md).

## Quality assurance

herdr-bridge treats quality gates as a first-class concern — every change, on every branch, for every contributor, runs the same automated checks. Below is the full set of gates, all visible in [`.github/workflows/`](.github/workflows/).

### Test (pytest)

Full test suite on every push and PR, across `ubuntu-latest` × `macos-latest` × Python 3.11–3.14. Unit tests run against an in-process `FakeHerdrServer` (no real Herdr installation needed); integration tests (marked `integration`) require a local Herdr and are deselected in CI. Convention: every fix starts with a failing regression test, every feature starts with a test-first spec. Run locally with `uv run pytest -q`.

### Coverage (pytest-cov)

`pytest --cov=src/herdr_bridge --cov-fail-under=80` — CI fails below 80% line coverage. This is a floor, not a ceiling; the actual coverage on core logic modules sits higher. The probe CLI entry point (`probe/__main__.py`) is the only file explicitly omitted (it's a CLI convenience wrapper, not library logic).

### Mutation testing (mutmut)

[mutmut](https://pypi.org/project/mutmut/) validates that the test suite actually catches bugs, not just executes lines. It mutates `schema.py` (the validation logic at the trust boundary) and confirms each mutation is killed by an existing test. Currently advisory (non-blocking in CI via `continue-on-error: true`); tightening to a hard gate as kill rate stabilizes.

### pip-audit

Weekly CVE scan of runtime and dev dependencies (`pip-audit --strict`, fail on HIGH or CRITICAL). Also runs on every push to `main` and every PR. Configuration: [`.github/workflows/pip-audit.yml`](.github/workflows/pip-audit.yml).

### gitleaks

Secret scanning on every push and every PR — full Git history, not just the diff. A [`.gitleaks.toml`](.gitleaks.toml) config file whitelists known false positives (test fixtures, example outputs). Configuration: [`.github/workflows/gitleaks.yml`](.github/workflows/gitleaks.yml).

### DCO (Developer Certificate of Origin)

All contributions require a `Signed-off-by` trailer (`git commit -s`) certifying the [Developer Certificate of Origin](https://developercertificate.org/) — you wrote the change yourself or otherwise have the right to submit it under this project's Apache-2.0 license. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor workflow.

## License

Apache-2.0 (see [`LICENSE`](LICENSE)). herdr-bridge is an independent client of the Herdr socket API: it does not contain, copy, or derive from Herdr source code (see [`NOTICE`](NOTICE)). As of Herdr's last tagged release, Herdr itself is a separate project licensed under AGPL-3.0-or-later — **note that Herdr's own upstream changelog has an "Unreleased" entry announcing a relicense to Apache-2.0, not yet shipped in a tagged version; re-check Herdr's current license before relying on the AGPL-specific guidance below.**

That independence claim covers this package's own code — it says nothing about your obligations. Concretely, under Herdr's *currently released* AGPL-3.0-or-later license:

(a) herdr-bridge itself is Apache-2.0 — you may use, modify, and redistribute it under those terms.
(b) At runtime it drives a local Herdr server, which is a separate program (AGPL-3.0-or-later as of the last tagged release; see the note above) you install and run yourself.
(c) If you offer a service over a network that runs Herdr as a component (CI bots, SaaS automation, hosted orchestration), AGPL §13's network-use clause may obligate you with respect to *Herdr* — regardless of herdr-bridge's own license. This obligation goes away once Herdr's announced Apache-2.0 relicense actually ships.
(d) herdr-bridge cannot waive or satisfy those obligations on your behalf; for commercial or networked deployments, evaluate Herdr's *current* license position yourself (and consider legal counsel for enterprise use).

## Project docs

- [`docs/api.md`](docs/api.md) — full per-function API reference and reserved-field semantics
- [`docs/api-acp.md`](docs/api-acp.md) — `herdr_bridge.acp` command-plane API reference (provisional tier)
- [`docs/testing.md`](docs/testing.md) — `FakeHerdrServer` usage guide for downstream contract testing without a real Herdr install
- [`docs/light-user-quickstart.md`](docs/light-user-quickstart.md) — quickstart guide for occasional users of `herdr-commander`

## Support

If herdr-bridge saves you from babysitting AI coding agents, you can support its development on [Ko-fi](https://ko-fi.com/aikenlin) (card or PayPal) or directly via [PayPal](https://paypal.me/aikenlin). Entirely optional — the library is and stays free.
