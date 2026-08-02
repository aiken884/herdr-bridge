# ADR 0002: ACP command plane via acpx (additive `herdr_bridge.acp` module)

- **Date**: 2026-07-20
- **Status**: Accepted(design)——M0 spike 與 M1 工具層模組已完成實作,見文末〈實作狀態〉
- **Decided-by**: PPLX 兩輪共識(round-1 NEEDS_REVISION → v2 修訂 → round-2 CONSENSUS)+ CommandTower 設計;Aiken 授權設計階段,實作開工另行核准
- **Refs**: docs/acp-command-plane-design.md(定案版全文)、docs/reviews/PPLX-ACP-round1.md、docs/reviews/PPLX-ACP-round2.md

## Context

指揮塔↔艦隊成員的指揮通訊現況是「herdr pane 盲打」(send-text 注入 `opencode run … | tee; echo MARKER`,grep marker 偵測完成)。實證痛點:單輪無上下文、marker 誤判、螢幕抓取無結構、idle≠done。ACP(Agent Client Protocol,JSON-RPC over stdio,v1)提供結構化 session/prompt/事件流/stopReason,直接消滅全部痛點;opencode 原生支援 `opencode acp`;acpx CLI(npm,alpha 0.12.0)提供 named stateful sessions。

## Decision

1. **方案 B:additive 新模組 `herdr_bridge.acp`**(0.2.0 minor)。六個 frozen API 一字不動;新契約(AcpActions:ensure_session/prompt/start_prompt+wait_done/cancel)並存。否決「雙軌 backend 塞舊 API」(pane-centric vs session-centric 本體論不合)與「上層直調 acpx」(alpha 直穿政策層+audit 斷裂)。
2. **兩面架構**:ACP=指揮面;herdr=監看+佈局面。canonical join key = herdr pane_id + 派工 ledger;session_name 為 display alias。
3. **可視性 baseline = O3**:pane 前景逐條跑 `acpx <agent> -s <name> '<prompt>'`(100% 確定性;-s 保多輪;exit+stopReason 滅 marker 誤判)。O1/O2 為 M0 後優化 spike 候選。
4. **alpha 風險隔離**:AcpxAdapter 單點組裝 + npm exact pin 0.12.0 + package-lock 提交 + 隔離 prefix + npm audit;AcpTransport Protocol 接縫 + 一週切官方 Python SDK 演練條款(R11)。
5. **ACP v2 應對**:全程釘 v1;bridge client 端對 v2 移除項(modes/fs/terminal)零直接依賴;model 指定以 per-tier config wrapper 為主(不依賴 modes);M0-V9 版本協商為 kill criterion。
6. **遷移**:M0 spike(12+ 驗證項,四 kill criteria:model 指定/版本協商/可視性/並行污染)→ M1 工具層模組(provisional tier)→ M2 政策層雙軌(fleet.yaml per-tier dispatch_backend,≥2 週或 ≥20 件實戰)→ M3 權限映射。每階段可回退;M2 回退=設定翻轉。
7. **權限**:起步 --approve-all(=現況等權,audit 標記無 agent 間隔離)→ S1 白名單 → S2 deny-default;M2 補 worktree 隔離 opt-in→opt-out 觸發條件 ADR。

## Consequences

- 上層 dispatch 從 marker predicate 改 branch on PromptResult.reason/stop_reason;named session 讓 agent_id 跨重啟失效之痛消失。
- 審計面從螢幕層升到全量 JSON-RPC tool_call 層。
- 定位:herdr-bridge 受眾從「herdr 使用者」擴為「任何 ACP agent 的編排者」(生態標準)。
- 承擔兩個 alpha/半熟面(acpx、opencode #13644 model bug),緩衝=四 kill criteria + 全程雙軌 + 設定級回退。
- ⏸ 待 Aiken:M0 spike 開工、M2 正式切換、S2 政策。

- **Confidence**: 90%(架構共識高;剩餘不確定性集中在 M0 待驗證的 acpx 實際行為)

## 實作狀態(2026-07-20)

M0 spike 與 M1 工具層模組（`herdr_bridge.acp`）已完成實作，`docs/acpx-adapter-implementation-plan.md`、`docs/api-acp.md`、`BOUNDARIES.md`（provisional/experimental additive 一節）、ADR 0003（根因鏈與 M0 決定性實測證據）為權威記錄。摘要：

- **模組結構**（§4.1）全數落地：`adapter.py`（G1 修復 + config/argv/env 組裝 + N1-N4 技術債補強）、`transport.py`（`AcpTransport` Protocol + `AcpxTransport`）、`binding.py`（pane↔session ledger 純函式）、`actions.py`（`connect()` + `AcpActions` 九方法）、既有 `models.py`/`events.py`/`errors.py`。
- **範圍調整**：只接好 `agent="opencode"` 這個 tier（§4.5 規劃的具名 acpx agent 條目多 tier 支援待上層設定後擴充，非本輪範圍）；`cancel()` 是直接終止子行程而非協定層 `session/cancel`；`prompt()` 的 `policy` 參數保留簽名形狀但不生效（policy 只在 `ensure_session()` 決定一次）。
- **驗證**：TDD 全程，單元測試 + 對真實 acpx CLI（配 stdlib-only fake ACP agent 或本機 G1-patched opencode 二進位）的整合測試，覆蓋率 95%+。獨立對抗式驗證發現並修正一項 CRITICAL（`OPENCODE_CONFIG` 非 opencode 設定合併鏈最終權威，已補 `OPENCODE_DISABLE_PROJECT_CONFIG` + 清除 `OPENCODE_CONFIG_CONTENT`）。
- **M2/M3**（政策層雙軌切換、權限映射正式導入）維持未開工，待對應的批准關卡。
