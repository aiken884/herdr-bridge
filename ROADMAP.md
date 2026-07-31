# Roadmap

This roadmap is intentionally small and honest. herdr-bridge is a **thin,
frozen-surface** library — the goal is stability and reliability, not feature
growth. The five public functions are frozen as of `0.1.0`; everything here is
additive or infrastructural.

Roadmap items are tracked as GitHub issues; this file is the high-level view.

## Near term (0.2, additive — signatures stay frozen)

- **`herdr_bridge.testing`** — promote the in-repo `FakeHerdrServer` to a
  supported public test double, so consumers can unit-test their orchestration
  against a fake Herdr without a live server.
- **Read revision cursor** — an optional, backward-compatible parameter so a
  consumer can ask "what changed since I last read?" instead of re-scanning
  output.
- **Submit semantics for `send_to_agent`** — a helper for multi-line / explicit
  submit, so interactive agent CLIs don't each re-implement the "send text then
  Enter" dance.
- **Agent/pane lifecycle helpers** — thin conveniences for opening a pane or
  starting an agent, so consumers don't have to shell out to the Herdr CLI.

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

## 近期（0.2，純新增——簽章維持凍結）

- **`herdr_bridge.testing`**——把目前放在 repo 裡的 `FakeHerdrServer` 升級成正式支援的公開測試替身，
  讓使用者可以針對一個假的 Herdr 做單元測試，不需要真的啟動一台 Herdr 伺服器。
- **讀取用的 revision cursor**——一個選用、向下相容的參數，讓使用者可以直接問「上次讀取之後有什麼
  變化」，不用每次都重新掃一遍輸出內容。
- **`send_to_agent` 的送出語意**——提供一個 helper 處理多行輸入／明確送出的情境，讓每一種互動式
  agent CLI 不用各自重新實作一次「先送文字、再送 Enter」這套動作。
- **Agent / pane 生命週期輔助函式**——開啟一個 pane 或啟動一個 agent 的輕量便利函式，讓使用者不用
  自己 shell out 去呼叫 Herdr CLI。

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
