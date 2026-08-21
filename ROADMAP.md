# Roadmap

This roadmap is intentionally small and honest. herdr-bridge is a **thin,
frozen-surface** library — the goal is stability and reliability, not feature
growth. The five public functions are frozen as of `0.1.0`; everything here is
additive or infrastructural.

Roadmap items are tracked as GitHub issues; this file is the high-level view.

Last aligned: 2026-08-21 (v0.10.2). The five public signatures remain frozen.

## Shipped (was “near term 0.2”)

- **`herdr_bridge.testing`** — shipped in 0.2.1 (`FakeHerdrServer`).
- **Read revision cursor** — shipped in 0.2.2 (`AgentOutput.revision` / `since_revision`).
- **Submit semantics for `send_to_agent`** — covered in practice by `herdr-commander notify-pane` (0.4.0) and, against Herdr 0.8.0+, `agent.prompt` (0.8.0 / 0.10.1). The frozen `send_to_agent()` signature itself is unchanged.
- **Agent/pane lifecycle helpers** — still out of the frozen tool-layer surface; use the Herdr CLI / `herdr-commander start` rather than adding new public functions.

## Near term (additive — signatures stay frozen)

- Track Herdr protocol changes with patch releases as needed (0.10.1/0.10.2 is the protocol 20 example).
- Keep PyPI Trusted Publishing current with Core Metadata as Hatchling/Twine evolve.

## Ongoing (quality & security)

- Maintain green CI (lint, types, tests across supported Python versions) and
  the package-hygiene and supply-chain gates.
- Track Herdr protocol changes with patch releases as needed.
- Keep aligning with OpenSSF Best Practices / Scorecard criteria.

## Community

- **Grow beyond a single maintainer** to reduce bus-factor (see
  [`GOVERNANCE.md`](GOVERNANCE.md), [`MAINTAINERS.md`](MAINTAINERS.md)).
- Collect real-world usage in [`ADOPTERS.md`](ADOPTERS.md).

## Explicit non-goals

- **No policy engine.** Scheduling, prioritization, access control, and
  multi-caller policy belong in the layer you build *on top* of herdr-bridge,
  not in it (see ADR 0001). herdr-bridge stays a neutral base.
- **No breaking changes to the five signatures** without a major version and a
  migration guide.
- **No runtime dependencies** unless there is a compelling, audited reason.

---

# 路線圖

這份路線圖刻意寫得很精簡、很誠實。herdr-bridge 是一個**薄、介面已凍結**的函式庫——目標是穩定
與可靠，不是功能越堆越多。五個公開函式從 `0.1.0` 起就已凍結；這裡列的項目全部都是新增或基礎建設
性質，不會動到既有介面。

路線圖項目都會用 GitHub issue 追蹤；這份文件只是高層次的總覽。

最後對齊：2026-08-21（v0.10.2）。五個公開函式簽章維持凍結。

## 已出貨（原本寫在「近期 0.2」）

- **`herdr_bridge.testing`**——0.2.1 出貨（`FakeHerdrServer`）。
- **讀取用的 revision cursor**——0.2.2 出貨（`AgentOutput.revision` / `since_revision`）。
- **`send_to_agent` 的送出語意**——實務上由 `herdr-commander notify-pane`（0.4.0）以及 Herdr 0.8.0+ 的 `agent.prompt`（0.8.0 / 0.10.1）涵蓋。凍結的 `send_to_agent()` 簽章本身不變。
- **Agent / pane 生命週期輔助函式**——仍不放進凍結工具層；請用 Herdr CLI / `herdr-commander start`，不要為此新增公開函式。

## 近期（純新增——簽章維持凍結）

- 隨 Herdr 協定變動視需要發 patch（0.10.1/0.10.2 即 protocol 20 的例子）。
- 讓 PyPI Trusted Publishing 跟著 Hatchling/Twine 的 Core Metadata 演進保持可發佈。

## 持續進行中（品質與安全性）

- 維持 CI 綠燈（lint、型別檢查、跨支援版本的 Python 測試），以及套件衛生檢查與供應鏈安全的關卡。
- 隨 Herdr 協定的變動，視需要跟進發布 patch 版本。
- 持續對齊 OpenSSF Best Practices / Scorecard 的標準。

## 社群

- **擴大維護者陣容、不再只靠一個人**，降低 bus factor（單一失聯風險）（詳見
  [`GOVERNANCE.md`](GOVERNANCE.md)、[`MAINTAINERS.md`](MAINTAINERS.md)）。
- 在 [`ADOPTERS.md`](ADOPTERS.md) 收集實際使用案例。

## 明確不做的事

- **不做 policy engine（決策引擎）。** 排程、優先順序、存取控制、多呼叫端的決策邏輯，都屬於建在
  herdr-bridge *之上*那一層該做的事，不會放進 herdr-bridge 本身（詳見 ADR 0001）。herdr-bridge
  維持中立的底層角色。
- **五個公開函式的簽章不會有不相容變更**，除非搭配 major 版本升級與遷移指南。
- **不會加入 runtime 依賴套件**，除非有經過審視、站得住腳的理由。
