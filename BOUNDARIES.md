# API Boundaries and Stability Contract

This document draws the line between herdr-bridge's **public, stable surface** and its **internal implementation** as an open-source library — so that anyone building on top of it knows what they can rely on and what may still change. This is the project's compatibility contract.

## Design Scope Constraint (Hard Boundary)

**Herdr Bridge is strictly scoped to single-machine use.**

- Every capability — the ACP command plane, `AcpRouter`, embedded Herdr Bridge Memory, cross-project coordination, batch dispatch, and so on — is designed, implemented, and tested under the assumption that everything runs on **one machine**.
- Multi-project coordination *within* a single machine is supported: isolated worktrees plus a local Herdr Bridge Memory database let one command tower control agents across different projects on the same host.
- **Explicitly excluded, permanently, from the roadmap**: anything cross-machine, cross-host, remote ACP, network transport, distributed memory backends, or coordination across multiple Herdr instances.

## Stable Public Surface (semver guaranteed)

The following has been **frozen** since `0.1.0`; breaking changes will only ever land on a major version bump:

- `connect(socket_path=None, *, audit_path=None, herdr_bin="herdr") -> BridgeActions`
- `BridgeActions`'s five methods, signatures frozen:
  - `list_agents(actor_id)`
  - `read_agent(actor_id, agent_id, mode="recent-unwrapped")`
  - `send_to_agent(actor_id, agent_id, text, priority=0)`
  - `wait_until(actor_id, agent_id, predicate, timeout_sec=60, poll_interval_sec=2)`
  - `acquire_control(actor_id, pane_id, mode="control")`
- Return data models (frozen dataclasses): `AgentInfo`, `AgentOutput`, `SendResult`, `WaitResult`, `ControlHandle`
- Exception hierarchy: `HerdrBridgeError` and its subclasses
- The semantics of the per-call `actor_id`/`priority`/`mode` fields: **recorded only, never enforced** (see ADR 0001, README)

### Additive Evolution (minor version, backward compatible)

New symbols only get added, never changed. Additions so far: `AgentOutput.normalized_text`; `BridgeActions.resolved_socket_path`/`socket_source`; `get_audit_log_path()`; the `"degraded"` value for `subscribe()`'s `on_state` (0.1.1); `BridgeActions.get_agent_status()` (a sixth method — the original five stay frozen); the `WaitReason` value `"blocked"` (lets `wait_until` exit early for a blocked agent) (0.1.2); `AgentOutput.revision`, `read_agent(since_revision=...)`, `wait_until(since_revision=...)`, `_RevisionAdapter` (WP4 experimental; 0.2.2).

## Internal Implementation (no stability guarantee — may change on any patch or minor release)

The following are **not** part of the public contract; consumers shouldn't depend on their shape, or even on their continued existence:

- Transport/cache internals such as `SocketClient`, `SessionCache`, `Subscription`, `_ControlRegistry` — used internally by the `BridgeActions` instance you get back from `connect()`. Don't construct these directly and don't reach into their private attributes.
- `schema.py`'s `SchemaStore`/`validate_request`/`SchemaError`: an **opt-in, development/exploration-time request-validation tool** that sits **outside** `connect()`'s execution path (live compatibility checking is `check_server_compat`'s job). It's a helper for probing, testing, and for consumers who want to self-validate — not a runtime safety guard. Don't treat it as a live guard.
- The `probe/` CLI: a development diagnostic tool, not part of the library's API.
- Anything whose name starts with an underscore.
- `_RevisionAdapter` — WP4 experimental (0.2.2); an internal helper that normalizes the revision value from a herdr response into `int | None`. Consumers shouldn't depend on it directly — its interface may shift as the herdr protocol evolves.

## `herdr_bridge.acp`: a provisional/experimental additive tier (outside the semver-frozen surface)

`herdr_bridge.acp` (the ACP command plane) is a **second surface that exists alongside the five methods above** (an observation plane vs. a command plane), and it is **not covered by the "Stable Public Surface" semver guarantee above**. Why:

- The upstream `acpx` (openclaw/acpx) is alpha (0.12.0), and its own docs say its interface is still expected to change.
- `herdr_bridge.acp` currently depends on an opencode fork fix that hasn't been merged upstream yet (G1: child/subagent session ACP permission hang, [anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902)); the locally built patched binary (`.vendor/opencode-patched/`, gitignored) is not a version-controlled artifact.
- Four agent tiers are supported: `agent="opencode"`, `agent="claude"`, `agent="copilot"`, `agent="grok-build"` (WP6, WP9). `claude` runs through acpx's named subcommand (`acpx claude`) plus a `--agent` escape hatch, as a hybrid route — global options like `--cwd`/`--ttl`/policy flags go *before* the `claude` subcommand. `copilot`/`grok` run through acpx's native named subcommands (`acpx copilot`/`acpx grok-build`); permission compliance is solid at the acpx layer, so they don't need the extra local config workaround that opencode requires.
- **Grok token risk**: the Grok CLI auto-loads unrelated skills installed by other local tools (e.g. SEO analysis, Cloudflare migration), which can blow up a single conversation's available-command list by close to 20,000 characters. Before dispatching real work, the skill scope needs to be narrowed down (via `--plugin-dir` or an equivalent mechanism) — otherwise every dispatch burns tokens for nothing. This is a known limitation.

**Breaking changes in this tier can land on a patch or minor version — no major bump required.**

### Current Public Surface (intended to be stable, but not semver-guaranteed)

- `acp.connect(*, acpx_bin="acpx", config_path=None, audit_path=None, transcript_dir=None, strict_version=False) -> AcpActions`
- `AcpActions`'s nine methods: `list_acp_agents`, `ensure_session`, `close_session`, `get_history`, `prompt`, `exec_prompt`, `start_prompt`, `wait_done`, `cancel`, `close`
- Return data models (frozen dataclasses): `AcpAgentSpec`, `AcpSessionInfo`, `AcpEvent`, `PromptResult`, `AcpPolicy`
- Exception hierarchy: `AcpError` and its subclasses (`AcpAdapterError`, `AcpTransportError`, `AcpSessionError`, `AcpTimeoutError`, `AcpVersionError`) — deliberately kept separate from `HerdrBridgeError`, not sharing an exception tree

### Internal Implementation (not a consumer interface — do not depend on it directly)

- The individual functions in `herdr_bridge.acp.adapter` (`build_opencode_permission_config`/`write_session_config`/`build_acpx_argv_and_env`/`build_acpx_policy_flags`/`resolve_patched_opencode_binary`/`detect_target_triple`) — assembly building blocks for `AcpActions`/`AcpxTransport`, not an interface meant to be called directly by consumers.
- `herdr_bridge.acp.transport.AcpxTransport`/the `AcpTransport` Protocol/`PromptHandle` — an SDK-evolution seam (D-10) that may eventually be replaced wholesale by an implementation talking directly to the official ACP SDK; consumers only ever touch these indirectly, through `AcpActions`.
- The ledger functions in `herdr_bridge.acp.binding` — a dispatch-reconciliation tool meant for a governance/policy layer above this one, not something this module calls on its own (deliberately policy-neutral, left for that higher layer to decide when to use).
- The NDJSON parsing details in `herdr_bridge.acp.events`.

## Extension Path

herdr-bridge is the **foundation**: the right way to extend it is to **build your own layer on top of the five methods** — a rules engine, a scheduler, a policy layer, whatever you need — rather than modifying or subclassing this layer's internals. This layer is deliberately policy-neutral: all policy, prioritization, and identity decisions belong in the layer you build above it.

## Dependency Boundary

- Zero third-party runtime dependencies.
- At runtime this drives a separate Herdr server (AGPL-3.0 as of Herdr's last tagged release — Herdr's upstream has an unreleased changelog entry announcing a move to Apache-2.0, not yet shipped); herdr-bridge itself is Apache-2.0 and is an independent client of it. Whatever AGPL obligations apply to your network-service scenario, with respect to Herdr's *currently released* license, are yours to evaluate (see README, NOTICE).

---

# API 邊界與穩定性契約

本文件界定 herdr-bridge 作為開源函式庫的**公開穩定面**與**內部實作**之間的界線——讓在其上建構的消費者知道哪些可依賴、哪些會變。這是開源專案的相容性合約。

## 設計範圍限制（硬性邊界）

**Herdr Bridge 嚴格限定為單機使用（single-machine only）。**

- 所有功能（包含 ACP 指揮面、AcpRouter、Herdr Bridge Memory 內嵌、跨專案協調、batch dispatch 等）皆以「同一台機器」為前提設計、實作與測試。
- 支援「同一機器內多專案協調」：使用隔離 worktree + 本地 Herdr Bridge Memory 資料庫，單一指揮塔可控制同一機器上不同專案的 agents。
- **明確排除且永不列入開發計畫**：跨機器、跨主機、遠端 ACP、網路通訊、分散式記憶後端、多 herdr 實例之間的協調等任何形式。

## 穩定公開面（semver 保證）

以下自 `0.1.0` 起**凍結**；破壞性變更只會隨 major 版號發生：

- `connect(socket_path=None, *, audit_path=None, herdr_bin="herdr") -> BridgeActions`
- `BridgeActions` 的五個方法，簽名凍結：
  - `list_agents(actor_id)`
  - `read_agent(actor_id, agent_id, mode="recent-unwrapped")`
  - `send_to_agent(actor_id, agent_id, text, priority=0)`
  - `wait_until(actor_id, agent_id, predicate, timeout_sec=60, poll_interval_sec=2)`
  - `acquire_control(actor_id, pane_id, mode="control")`
- 回傳資料模型（frozen dataclasses）：`AgentInfo`、`AgentOutput`、`SendResult`、`WaitResult`、`ControlHandle`
- 例外階層：`HerdrBridgeError` 及其子類
- 每次呼叫的 `actor_id`／`priority`／`mode` 欄位語意：**只記錄、不強制**（見 ADR 0001、README）

### Additive 演進（minor 版號，向下相容）

新符號只增不改。已加入者：`AgentOutput.normalized_text`、`BridgeActions.resolved_socket_path`／`socket_source`、`get_audit_log_path()`、`subscribe()` 的 `on_state` 值 `"degraded"`（0.1.1）；`BridgeActions.get_agent_status()`（第六函式，五函式凍結不動）、`WaitReason` 值 `"blocked"`（wait_until 對 blocked agent 提前退出）（0.1.2）；`AgentOutput.revision`、`read_agent(since_revision=...)`、`wait_until(since_revision=...)`、`_RevisionAdapter`（WP4 experimental；0.2.2）。

## 內部實作（不保證穩定，可隨 patch/minor 變動）

以下**不是**公開契約，消費者不應依賴其形狀或存在：

- `SocketClient`、`SessionCache`、`Subscription`、`_ControlRegistry` 等傳輸/快取內部類別——透過 `connect()` 取得的 `BridgeActions` 使用，不要直接建構或依賴其私有屬性。
- `schema.py` 的 `SchemaStore`／`validate_request`／`SchemaError`：**opt-in 的開發/探測期請求驗證工具**，**不在** `connect()` 的執行路徑上（live 相容性檢查由 `check_server_compat` 負責）。它是給 probe/測試與想自行驗證的消費者用的輔助，不是執行期安全屏障——不要當作 live guard 依賴。
- `probe/` CLI：開發診斷工具，非函式庫 API。
- 任何底線開頭的屬性/函式。
- `_RevisionAdapter`——WP4 experimental（0.2.2）；將 herdr 回應中的 revision 值正規化為 `int | None` 的內部輔助函式。消費端不宜直接依賴；介面可能隨 herdr 協定演進而調整。

## `herdr_bridge.acp`：provisional/experimental additive tier（不入 semver 凍結面）

`herdr_bridge.acp`（ACP 指揮面）是**與上方五函式並存的第二面**（監看面 vs 指揮面），**不受上方「穩定公開面」的 semver 保證約束**。理由：

- 上游 `acpx`（openclaw/acpx）是 alpha（0.12.0，介面自己明言會變）。
- `herdr_bridge.acp` 目前依賴一個尚未合併上游的 opencode fork 修復（G1：child/subagent session ACP permission hang，[anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902)），本機建置的 patched 二進位（`.vendor/opencode-patched/`，gitignored）不是版控產物。
- 支援 `agent="opencode"`、`agent="claude"`、`agent="copilot"`、`agent="grok-build"` 四個 tier（WP6、WP9）。claude 走 acpx 具名子指令（`acpx claude`）+ `--agent` escape hatch 的混合路線——`--cwd`/`--ttl`/policy flag 等全域選項放在 `claude` 子指令之前。copilot/grok 走 acpx 原生具名子指令（`acpx copilot`／`acpx grok-build`），acpx 層權限遵從度高，不需 opencode 那套額外的本地 config workaround。
- **Grok token 風險**：Grok CLI 會自動載入本機其他工具裝的無關 skill（例如 SEO 分析、Cloudflare 遷移等），單次對話的可用指令清單暴增近 2 萬字元。正式派工前需限縮 skill 範圍（透過 `--plugin-dir` 或等效機制），否則每次派工白白燒 token。此為已知限制。

**這一層的破壞性變更可能隨 patch/minor 版號發生，不需要 major bump。**

### 目前的公開面（意圖穩定，非 semver 保證）

- `acp.connect(*, acpx_bin="acpx", config_path=None, audit_path=None, transcript_dir=None, strict_version=False) -> AcpActions`
- `AcpActions` 九方法：`list_acp_agents`、`ensure_session`、`close_session`、`get_history`、`prompt`、`exec_prompt`、`start_prompt`、`wait_done`、`cancel`、`close`
- 回傳資料模型（frozen dataclasses）：`AcpAgentSpec`、`AcpSessionInfo`、`AcpEvent`、`PromptResult`、`AcpPolicy`
- 例外階層：`AcpError` 及其子類（`AcpAdapterError`、`AcpTransportError`、`AcpSessionError`、`AcpTimeoutError`、`AcpVersionError`）——刻意獨立於 `HerdrBridgeError`，不共用同一棵例外樹

### 內部實作（不是消費介面，不要直接依賴）

- `herdr_bridge.acp.adapter` 的個別函式（`build_opencode_permission_config`／`write_session_config`／`build_acpx_argv_and_env`／`build_acpx_policy_flags`／`resolve_patched_opencode_binary`／`detect_target_triple`）——`AcpActions`/`AcpxTransport` 的組裝積木，不是給消費者直接呼叫的介面。
- `herdr_bridge.acp.transport.AcpxTransport`／`AcpTransport` Protocol／`PromptHandle`——SDK 演進接縫（D-10），未來可能整個替換為官方 ACP SDK 直連的實作，消費者只透過 `AcpActions` 間接使用。
- `herdr_bridge.acp.binding` 的 ledger 函式——治理層的派工對帳工具，不是這個模組自己會呼叫的路徑（policy-neutral，故意留給上層決定何時用）。
- `herdr_bridge.acp.events` 的 NDJSON 解析細節。

## 擴充方式

herdr-bridge 是**地基**：擴充的正道是**在五函式之上建構你自己的上層**（規則引擎、排程器、政策層等），而非改動或繼承本層內部。本層刻意 policy-neutral——所有策略、優先權、身份裁決屬於你的上層。

## 相依邊界

- Runtime 零第三方依賴。
- 執行期驅動一個獨立的 Herdr server(以最新正式發行版而言為 AGPL-3.0——Herdr 上游 changelog 有一筆尚未隨正式版本釋出的「改採 Apache-2.0」條目);herdr-bridge 本身 Apache-2.0，是其獨立 client。網路服務場景的 AGPL 義務,就 Herdr **目前已發行**的授權而言由你自行評估(見 README、NOTICE)。
