**[English](#herdr-bridge) | [繁體中文](#herdr-bridge-1)**

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
- **herdr 0.7.4 has a confirmed upstream restore bug**: after a Herdr session server restart, panes/agents restored from `session.snapshot` show up in listings, but reading them (`agent.read`/`pane.read`) fails with `agent_not_found`/`pane_not_found`. herdr-bridge cannot work around this at the client level — it raises `AgentNotFoundError` as designed, for your own degradation path to handle. Newly created panes after the restart are unaffected.

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

Weekly CVE scan of dev dependencies (`pip-audit --strict`, fail on HIGH or CRITICAL). Also runs on every push to `main` and every PR. Runtime dependencies are intentionally empty (`dependencies = []`), so the audit scope is dev tooling only. Configuration: [`.github/workflows/pip-audit.yml`](.github/workflows/pip-audit.yml).

### gitleaks

Secret scanning on every push and every PR — full Git history, not just the diff. A [`.gitleaks.toml`](.gitleaks.toml) config file whitelists known false positives (test fixtures, example outputs). Configuration: [`.github/workflows/gitleaks.yml`](.github/workflows/gitleaks.yml).

### DCO (Developer Certificate of Origin)

All contributions require a `Signed-off-by` trailer (`git commit -s`) certifying the [Developer Certificate of Origin](https://developercertificate.org/) — you wrote the change yourself or otherwise have the right to submit it under this project's Apache-2.0 license. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full contributor workflow.

## Support

If herdr-bridge saves you from babysitting AI coding agents, you can support its development on [Ko-fi](https://ko-fi.com/aikenlin) (card or PayPal) or directly via [PayPal](https://paypal.me/aikenlin). Entirely optional — the library is and stays free.

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

---

# herdr-bridge

**你的 AI 指揮塔，為偶爾才動用 AI 開發的你而生。**

不是每天都得靠 AI 幫忙,但每次真的需要 AI 處理一件開發工作時,總得重新煩惱一次:要顧哪個助手做什麼、事後又要怎麼確認它真的做對了。herdr-bridge 就是幫你把這些瑣事整個接手。

你只要用一句話說出你要什麼,它會幫你把任務拆解、派給最適合的 AI 助手、盯著執行進度,連結果驗收都幫你顧到。

不必開一堆視窗盯著、不必自己追進度、也不必先啃一份落落長的設定文件。

## 關於這個專案

herdr-bridge 是建立在 [Herdr](https://herdr.dev) 之上的語意協調層,讓單一指揮塔能可靠地指揮同時在你終端機裡跑的多個品牌 AI coding agent(Claude Code、Codex、Grok、OpenCode、Copilot、Gemini 等)。它補上了「我開了好幾個 AI 助手」跟「我跟其中一個講了需求、它就處理好了」之間缺的那一塊。

herdr-bridge 核心是兩個部分:
- **工具層**(`herdr_bridge`,Python 函式庫):五個凍結、有型別的函式——`list_agents`、`read_agent`、`send_to_agent`、`wait_until`、`acquire_control`——包裝 Herdr 的 socket API,搭配本機最終一致的狀態快取與完整稽核記錄。沒有排程、沒有規則引擎、沒有隱藏的政策邏輯——就是一個穩定的自動化介面。
- **輕量指揮塔**(`herdr-commander`,CLI):建立在工具層之上、給偶爾使用者的即用層——一句話說出任務,它會挑出最適合的 agent、派工、追蹤進度,並用白話回報結果。

內建記憶功能(跨任務、跨 agent 的記憶存取)、多層送達確認(讓「已送出」真的等於「已收到」),以及對 headless(ACP)與互動式 TUI agent 的支援,都是開箱即用——完整細節見下方各節。

> **目前狀態**:ACP Router + 真實下游 agents + 內嵌 Herdr Bridge Memory + CLI router 皆已完成(4 個 agent:code/research/echo/general-tui,支援動態發現與註冊,407 項測試)。`herdr-commander run`/`router`/`status` 皆可直接使用。

<details>
<summary>目錄</summary>

- [給偶爾使用者:三步驟開始](#給偶爾使用者三步驟開始)
- [為什麼偶爾使用者更需要它](#為什麼偶爾使用者更需要它)
- [安裝](#安裝)
- [快速上手](#快速上手)
- [ACP 指令層](#acp-指令層herdr_bridgeacp--實驗性質)
- [限制](#限制)
- [保留欄位](#保留欄位)
- [相容性](#相容性)
- [品質把關](#品質把關)
- [支持這個專案](#支持這個專案)
- [授權](#授權)
- [專案文件](#專案文件)

</details>

## 給偶爾使用者:三步驟開始

```bash
pip install herdr-bridge   # 需先安裝 Herdr:https://herdr.dev
herdr-commander start      # 檢查環境(或執行 bash scripts/commander-start.sh --sandbox)
herdr-commander run        # 執行第一個任務:縮圖函式 + 單元測試
```

詳見 [`docs/light-user-quickstart.md`](docs/light-user-quickstart.md)。

## 為什麼偶爾使用者更需要它

當你不是每天都泡在多個 AI 視窗裡,每次想請 AI 幫忙做開發,都得重新面對同樣的麻煩:
- 該開哪幾個 AI?
- 任務要怎麼講才夠清楚?
- 做完了要怎麼知道真的沒問題?

herdr-bridge 把這些協調成本整個收走。你只要說出你要什麼,剩下的交給指揮塔處理。

**這不是給每天精心調教一整隊 agent 的重度玩家用的**——那群人早就玩得很開心了。我們服務的是偶爾才需要動用 AI 的人。

Herdr 本身已經提供了必要的底層能力:一個本機 Unix socket API,可以列出 pane 與 agent、讀取 pane 輸出、送出按鍵、訂閱狀態變化事件。它沒有提供的,是一個穩定、有型別、有文件記錄的介面讓你在上面建立自動化——這個 socket 協定由伺服器端擁有,並隨著 Herdr 本身持續演進。

herdr-bridge 補的就是這一層。它把 socket API 包裝成五個函式——`list_agents`、`read_agent`、`send_to_agent`、`wait_until`、`acquire_control`——具備凍結的呼叫簽章、本機最終一致的 session 狀態快取,以及「誰呼叫了什麼」的稽核紀錄。它刻意只做工具層:沒有排程、沒有規則引擎、沒有多租戶政策。這些屬於建立在它之上的治理層該做的事,這也是為什麼每一次呼叫都已經帶著 `actor_id`、`priority`、`mode` 這幾個欄位——即使這個函式庫目前還不會依據它們採取任何行動(見下方「保留欄位」)。

### 與 `pyherdr` 是不同的東西

PyPI 上另外還有 [`pyherdr`](https://pypi.org/project/pyherdr/),一個把 Herdr 多工器本身用純 Python 重新實作(port/fork)的專案。兩者做的是完全不同的事:`pyherdr` 重新實作了多工器本身;herdr-bridge 則是官方 Rust 版 Herdr socket API 的客戶端函式庫——它假設你執行的是上游 Herdr,並在其上提供一個穩定、有稽核紀錄的呼叫介面給你的自動化程式使用。如果你想要一個 Python 版的多工器,請用 `pyherdr`;如果你想在官方 Herdr 之上寫腳本操控 agent,這個函式庫就是為此而生。

## 安裝

```bash
uv add herdr-bridge
# 或
pip install herdr-bridge
```

需要 Python 3.11 以上,並且本機要有執行中的 [Herdr](https://herdr.dev)(0.7.3 以上、socket 協定版本 16),可透過 `herdr` CLI 或 `HERDR_SOCKET_PATH` 環境變數連線。herdr-bridge 透過 Unix domain socket 與 Herdr 溝通,因此只支援 macOS 與 Linux,不支援 Windows。

## 快速上手

```python
from herdr_bridge import connect

actions = connect()
ACTOR = "rule:demo-script"  # "<category>:<name>" —— 詳見 docs/api.md

# 現在有哪些 agent 在線上?
agents = actions.list_agents(ACTOR)
for agent in agents:
    print(agent.agent_id, agent.brand, agent.status)

target = agents[0].agent_id

# 它目前印出了什麼?
output = actions.read_agent(ACTOR, target)
print(output.text[-500:])

# 叫它做點事
actions.send_to_agent(ACTOR, target, "run the test suite")

# 等到輸出看起來完成了,或是 2 分鐘後放棄
result = actions.wait_until(
    ACTOR, target,
    predicate=lambda out: "PASSED" in out.text or "FAILED" in out.text,
    timeout_sec=120,
)
print(result.success, result.reason)  # reason: predicate | timeout | agent_gone | error | blocked
```

`connect()` 會自動偵測本機的 Herdr socket、檢查協定相容性,並啟動 session 快取。完整的治理規則風格範例(等待測試失敗、把上下文轉給審查用的 agent)見 [`examples/failed_forwarder.py`](examples/failed_forwarder.py);完整 API 參考見 [`docs/api.md`](docs/api.md)。

## ACP 指令層(`herdr_bridge.acp`)—— 實驗性質

上面五個函式屬於「透過 Herdr pane 觀察並協調」這一層。`herdr_bridge.acp` 是另一個獨立模組,直接透過 [Agent Client Protocol](https://agentclientprotocol.com)(經由 [`acpx`](https://www.npmjs.com/package/acpx) CLI)驅動 opencode,而不是解析 pane 畫面文字(screen-scraping):用結構化的 `session/update` 事件與明確的 `stopReason`,取代在輸出裡抓關鍵字比對。

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

**這個模組屬於實驗性質、尚未定案**,明確**不**受上方凍結五函式的 semver 保證涵蓋——詳見 [`BOUNDARIES.md`](BOUNDARIES.md)。上游 `acpx` 本身還在 alpha 階段,這個模組目前依賴一個本機自行建置的 opencode fork,修補了一個真實存在的上游 bug(child/subagent 的 ACP session 從未被註冊,導致任何需要為被委派的 subagent 自身動作要求授權的 prompt 都會卡住——[anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902),等待上游審查中)。目前只接通了 `opencode` 這一個 agent tier。完整參考與已知限制見 [`docs/api-acp.md`](docs/api-acp.md)。

## 狀態語意的但書

Claude Code pane 的 `AgentInfo.status` 來自 Herdr 對畫面內容的偵測,而不是 agent 程序本身送出的結構化訊號——**`idle` 不代表真的「做完了」**。M0 實機測試(N=8 次、單一 Claude Code 版本、單一注入方式——只是初步樣本,不是可靠度基準)發現 8 次測試中 `working`/`idle` 的轉換都偵測正確,但一個「等你確認」的提示(例如信任資料夾的對話框)同樣被回報為 `idle`——這是已確認的 false-idle 案例。

正因如此,`wait_until` 從不單憑一次狀態事件就下判斷:每次收到 `pane_agent_status_changed` 事件,它都會重新讀取 pane 內容、重新用你的 predicate 評估實際文字,只有 predicate 真的符合(或逾時、agent 消失、agent 進入 `blocked` 狀態)才會結束等待。它會回傳 `WaitResult` 而不是丟出例外,並附上固定五種值的 `reason`(`predicate` / `timeout` / `agent_gone` / `error` / `blocked`)。`blocked` 這個 reason(0.1.2 新增)會在 Herdr 偵測到 agent 正在等待外部輸入時提早結束等待——呼叫端不用把 `timeout_sec` 整段耗在一個卡住的 agent 上。

## 限制

- **`acquire_control(mode="control")` 是單一行程內的互斥鎖**,不是 Herdr 伺服器端的鎖。它防的是同一個 bridge 行程裡兩個呼叫端搶同一個 pane;但機器上兩個各自獨立的 bridge 行程彼此完全看不見對方。
- **稽核紀錄會無限成長。** 它是一個 JSONL 檔案(預設路徑 `~/.local/state/herdr-bridge/audit.jsonl`,檔案權限 `0600`,目錄權限 `0700`),記錄呼叫摘要——絕不記錄完整文字內容——herdr-bridge 本身不會輪替或限制它的大小;如果這對你的部署有意義,請自行搭配 `logrotate` 之類的工具。
- **`agent_id`(Herdr 的 `terminal_id`)只在單一次 Herdr 伺服器執行期間有效。** 已在實機重啟測試中確認:重啟後 `terminal_id` 會被重新分配,但 `pane_id` 與 agent 自身的 session 識別(`AgentInfo.session_ref`)維持不變。不要跨 Herdr 伺服器重啟保存 `agent_id`——請改用 `session_ref` 重新解析身分。
- **`AgentInfo.status` 是最終一致的**,背後是一個本機快取,每 5 分鐘會做一次完整快照校正,作為漂移量的上限;它不保證每一次中間狀態轉換都會即時推送。
- **herdr 0.7.4 有一個已確認的上游還原 bug**:Herdr session 伺服器重啟後,從 `session.snapshot` 還原的 pane/agent 會出現在列表裡,但讀取它們(`agent.read`/`pane.read`)會失敗,回傳 `agent_not_found`/`pane_not_found`。herdr-bridge 在客戶端這一層無法繞過這個問題——它會照設計丟出 `AgentNotFoundError`,交由你自己的降級路徑處理。重啟後新建立的 pane 不受影響。

## 保留欄位

每一次呼叫都帶著 `actor_id`;`send_to_agent` 還帶 `priority`;`acquire_control` 帶 `mode`。herdr-bridge 目前不會依據這些欄位做任何強制、排序或門檻判斷——它只會記錄下來(外加一個 `actor_id_status` 稽核等級,用來標記格式錯誤的值)。它們存在的原因,是讓未來可能建立在此之上的治理層——規則引擎、優先權排程器、或多呼叫端政策——能夠直接使用,而不必更動這些已經凍結的函式簽章。完整格式、數值範圍與具名錨點見 [`docs/api.md`](docs/api.md)。

## 相容性

- 已在 Herdr 0.7.3 與 0.7.4、socket 協定版本 16(85 個方法)上測試過。
- `connect()` 會直接拒絕比協定 16 更舊的伺服器;若伺服器回報更新的協定版本,只會顯示警告並繼續運作(`protocol_compat="untested"`),不會直接失敗中斷。
- Herdr 本身是 pre-1.0、且擁有自己的 socket 協定;herdr-bridge 可能需要獨立於自身版本之外的 patch release,來追蹤上游的變動。
- 五個公開函式簽章(`list_agents`、`read_agent`、`send_to_agent`、`wait_until`、`acquire_control`)**自 0.1.0 起已凍結**。`0.x` 這個版號反映的是 Herdr 自身 pre-1.0 的成熟度,不是這個函式庫介面本身不穩定。
- **0.1.1(新增,v0.1.0 所有簽章不變)**——源自下游專案實際使用過程中發現的需求:`AgentOutput.normalized_text`(把 PTY 的強制換行接回去,讓被換行截斷的 marker 仍然能比對成功)、`get_audit_log_path()`(公開的唯讀稽核紀錄路徑,使用端不用再自己伸手進內部實作)、`connect()` 回傳物件上的 `resolved_socket_path` / `socket_source`(讓你能斷言確實連到了預期的 session),以及在連續重連失敗後會送出一次的 `"degraded"` 訂閱狀態。詳見 [`CHANGELOG.md`](CHANGELOG.md)。
- **0.1.2(新增,先前所有簽章不變)**——`get_agent_status()`(第六個公開方法,Herdr 原生狀態查詢,不做語意解讀)、`wait_until` 現在會在 agent 進入 Herdr 的 blocked 狀態(等待核准/輸入)且 predicate 尚未符合時,提早以 `reason="blocked"` 結束等待。詳見 [`CHANGELOG.md`](CHANGELOG.md)。
- **0.2.0(新增,v0.1.x 所有簽章不變)**——上述的 `herdr_bridge.acp` 指令層:`connect()`/`AcpActions` 的九個方法,透過 `acpx` 以 ACP 協定驅動 opencode。這是一個獨立、實驗性質的介面(見 [`BOUNDARIES.md`](BOUNDARIES.md))——不影響已凍結的五函式保證。詳見 [`CHANGELOG.md`](CHANGELOG.md) 與 [`docs/api-acp.md`](docs/api-acp.md)。
- **0.2.1(新增,先前所有簽章不變)**——`herdr_bridge.testing` 公開子套件:提供 `FakeHerdrServer`,讓下游使用端能在沒有真實 Herdr 安裝的情況下做合約測試。詳見 [`CHANGELOG.md`](CHANGELOG.md) 與 [`docs/testing.md`](docs/testing.md)。
- **0.2.2(新增,先前所有簽章不變)**——`AgentOutput.revision` 單調遞增計數器 + 僅限關鍵字參數的 `since_revision` 過濾(實驗性質);ACP 的 `AcpxTransport` 擴充支援 claude tier;新增 `AcpSdkTransport` 作為透過官方 `agent-client-protocol` Python SDK 的替代 ACP 傳輸方式(選用);CI/QA 強化(mutmut 每夜跑一次的把關、CodeQL/Scorecard 雛形)。詳見 [`CHANGELOG.md`](CHANGELOG.md)。
- **0.3.0(新增,先前所有簽章不變)**——`AcpRouter`(`herdr_bridge.acp.router`):讓指揮塔同時扮演 ACP 伺服器與客戶端,支援動態 agent 註冊表發現、四個真實獨立的下游 ACP agent、`herdr-commander router {list,discover,route,register,unregister,start}` CLI,以及貫穿整條派工路徑的內嵌 Herdr Bridge Memory 協調。詳見 [`CHANGELOG.md`](CHANGELOG.md)。
- **0.4.0(新增,先前所有簽章不變)**——`herdr-commander notify-pane`:給互動式 TUI pane 用的可靠通道(原子鍵盤注入 + 畫面差異送達確認、依 TUI 種類判斷送出時機、忙碌/殭屍/啟動競態防護);`herdr-commander` 現在可以透過 `pipx install --editable <repo>` 全域安裝,讓機器上任何 pane 都能連到任何其他 pane,不再侷限於這個專案自己的 venv;送達狀態的 FSM 有了獨立的儲存機制。詳見 [`CHANGELOG.md`](CHANGELOG.md)。
- **0.5.0(新增,先前所有簽章不變)**——`agent-client-protocol` 從選用 extra 升級為主要依賴(Secondary/ACP 層預設可用);`notify-pane` 的 `--tui` 新增 `copilot` 與 `gemini` 品牌支援;新增 `herdr-commander doctor` 一次性診斷指令。詳見 [`CHANGELOG.md`](CHANGELOG.md)。

## 品質把關

herdr-bridge 把品質關卡當成第一等公事——每一次變更、在每一個分支上、對每一位貢獻者,都跑同一套自動化檢查。以下是完整的關卡清單,全部可以在 [`.github/workflows/`](.github/workflows/) 看到。

### 測試(pytest)

每次 push 與 PR 都會跑完整測試套件,涵蓋 `ubuntu-latest` × `macos-latest` × Python 3.11–3.14。單元測試針對 in-process 的 `FakeHerdrServer` 執行(不需要真的安裝 Herdr);整合測試(標記為 `integration`)需要本機真實 Herdr,CI 裡會排除不跑。慣例是:每個修復都先寫一個會失敗的回歸測試,每個新功能都先寫測試規格。本機執行:`uv run pytest -q`。

### 覆蓋率(pytest-cov)

`pytest --cov=src/herdr_bridge --cov-fail-under=80`——CI 在行覆蓋率低於 80% 時會失敗。這是下限不是上限;核心邏輯模組實際的覆蓋率更高。唯一明確排除在外的是 probe CLI 進入點(`probe/__main__.py`)——它是 CLI 使用上的便利包裝,不是函式庫邏輯本身。

### 突變測試(mutmut)

[mutmut](https://pypi.org/project/mutmut/) 驗證的是測試套件真的能抓到 bug,而不只是把程式碼跑過一遍。它對 `schema.py`(信任邊界上的驗證邏輯)做突變,並確認每一個突變都會被既有測試抓到、判定為失敗。目前是建議性質(CI 裡透過 `continue-on-error: true` 設為不阻擋),隨著抓到率穩定下來,會逐步收緊成硬性關卡。

### pip-audit

每週對開發依賴做一次 CVE 掃描(`pip-audit --strict`,發現 HIGH 或 CRITICAL 就失敗)。每次 push 到 `main` 與每個 PR 也都會跑。執行期依賴刻意保持空白(`dependencies = []`),所以掃描範圍只涵蓋開發工具鏈。設定檔:[`.github/workflows/pip-audit.yml`](.github/workflows/pip-audit.yml)。

### gitleaks

每次 push 與每個 PR 都做機密掃描——掃的是完整 Git 歷史,不只是這次的 diff。[`.gitleaks.toml`](.gitleaks.toml) 設定檔把已知的誤判(測試 fixture、範例輸出)列入白名單。設定檔:[`.github/workflows/gitleaks.yml`](.github/workflows/gitleaks.yml)。

### DCO(Developer Certificate of Origin)

所有貢獻都必須帶有 `Signed-off-by` 標記(`git commit -s`),證明你確實擁有依照本專案 Apache-2.0 授權提交這項變更的權利,詳見 [Developer Certificate of Origin](https://developercertificate.org/)。完整貢獻流程見 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 支持這個專案

如果 herdr-bridge 幫你省下了盯著 AI coding agent 的時間,歡迎透過 [Ko-fi](https://ko-fi.com/aikenlin)(信用卡或 PayPal)或直接透過 [PayPal](https://paypal.me/aikenlin) 贊助它的開發。完全自由選擇——這個函式庫本身永遠免費。

## 授權

Apache-2.0(詳見 [`LICENSE`](LICENSE))。herdr-bridge 是 Herdr socket API 的獨立客戶端:不包含、不複製、也不衍生自 Herdr 的原始碼(詳見 [`NOTICE`](NOTICE))。以 Herdr 最新一次正式發行版為準,Herdr 本身是另一個獨立專案,採用 AGPL-3.0-or-later 授權——**但請注意,Herdr 自己的上游 changelog 裡有一筆「Unreleased」條目,宣布要改採 Apache-2.0 授權,目前尚未隨正式版本釋出;引用下方 AGPL 相關說明前,請自行覆查 Herdr 當下的實際授權狀態。**

這項獨立性的說明,涵蓋的是這個套件自身的程式碼——不代表你自己就不需要承擔任何義務。以下具體說明以 Herdr **目前已發行**的 AGPL-3.0-or-later 授權為準:

(a) herdr-bridge 本身是 Apache-2.0——你可以依照該授權使用、修改、再散布它。
(b) 它在執行期會驅動一個本機 Herdr 伺服器,那是一個你自己安裝、自己執行的獨立程式(以最新正式發行版而言為 AGPL-3.0-or-later,見上方提醒)。
(c) 如果你透過網路提供一項把 Herdr 當成元件的服務(CI bot、SaaS 自動化、託管式協調服務),AGPL 第 13 條的網路使用條款可能會就 *Herdr* 這部分對你產生義務——這與 herdr-bridge 自身的授權無關。等 Herdr 宣布的 Apache-2.0 重新授權正式生效後,這項義務就不再適用。
(d) herdr-bridge 無法代替你免除或滿足那些義務;商業或網路部署情境,請自行評估 Herdr **當下**的授權立場(企業使用建議諮詢法律顧問)。

## 專案文件

- [`docs/api.md`](docs/api.md) —— 完整的逐函式 API 參考與保留欄位語意
- [`docs/api-acp.md`](docs/api-acp.md) —— `herdr_bridge.acp` 指令層 API 參考(實驗性質)
- [`docs/testing.md`](docs/testing.md) —— `FakeHerdrServer` 使用指南,供下游做合約測試時不需要真實 Herdr 安裝
- [`docs/light-user-quickstart.md`](docs/light-user-quickstart.md) —— 給 `herdr-commander` 偶爾使用者的快速上手指南
