# ADR 0003 — opencode 系 tier 的 FS 信任邊界：強制 workdir 隔離取代 ACP 權限協商

- **Date**: 2026-07-20
- **Status**: Accepted
- **Decided-by**: M0 ACP spike 實測（docs/m0-acp-spike-evidence.md §5.1、§10.2）+ PPLX 對抗式審查（docs/reviews/PPLX-M0-ACP-spike-review.md P0-A）
- **Refs**: docs/acp-command-plane-design.md §4.6/§5 R5/M3、docs/adr/0002-acp-command-plane.md

## Context

ADR 0002 §4.6/M3 規劃的權限演進路徑（S0 approve-all → S1 approve_reads 白名單 → S2 deny-by-default）假設 acpx 的 `--approve-all`/`--approve-reads`/`--deny-all` policy flag 對所有艦隊 tier 均有實際約束力。

M0 spike 實測推翻此假設，且僅限 opencode 系 tier：

1. 對 `oc-dsflash`（opencode 1.18.3 ACP server）跑 `--deny-all --non-interactive-permissions deny` 執行檔案寫入：**寫入照常成功**。逐行解析 NDJSON 確認全程零筆 `session/request_permission`、零筆 `fs/write_text_file`——opencode 的 ACP server 對檔案寫入完全不經過 ACP 協定層的權限協商，直接在自身進程內執行。
2. 對照組：同一測試對 `claude`（claude-agent-acp 0.37.0）跑 `--deny-all`，**正確攔截**——`session/request_permission` 確實被呼叫、寫入未發生、`acpx` 以非零 exit code 回報。claude adapter 的實作符合 ACP 規範。
3. 追加測試：改用 opencode 自身的 config schema（`permission.edit: "deny"`，經 `OPENCODE_CONFIG` 注入）——**同樣未擋下**。opencode 在 `opencode acp` 模式下，其自身原生 permission 設定似乎也未套用於此路徑。

這不是「policy flag 設定錯誤」，是 opencode ACP server 目前的實作架構性地不走 ACP 權限協商這條路徑（根因未深入 opencode 原始碼查證，列為待上游查證項，比照設計文件既有的 opencode issue #13644 model bug 處理方式）。

PPLX 對抗式審查明確指出：這個發現不能以「文件化風險、帶著已知缺口放行」處置，因為它是 M1（`herdr_bridge.acp`）安全邊界設計的地基假設之一，必須在 M1 開始前有明確架構決策，否則 M1 會在錯誤的安全假設上設計 API surface。

### 更新（2026-07-20，Phase 0 實測）——校正根因描述，Decision 本身不變

上方 Context 第 3 點與本節第一段「架構性地不走 ACP 權限協商這條路徑」的描述，經**第一次深入 opencode 原始碼查證**（`docs/acp-permission-wiring-design.md`）與**真實子進程實測**（同文件 §3.5）後需要校正：

1. **opencode 的 ACP 權限協商機制本身是健全的。** `packages/opencode/src/acp/permission.ts` 的 `Handler` 確實會監聽核心 `permission.asked` 事件並呼叫 `connection.requestPermission(...)`（即 `session/request_permission`），無 client 能力時 fail-closed reject；權限引擎（`permission/index.ts`）遇 `deny` 直接丟 `DeniedError` 不執行。
2. **用現行 opencode dev HEAD 的真實 `opencode acp` 子進程實測確認**：透過 opencode 官方支援的兩種 config 傳遞機制——`OPENCODE_CONFIG`（env 指向的檔案）與真實 project `opencode.json`——設定 `permission.edit:"deny"`，ACP 主 session 下的檔案寫入**都正確被擋下**。
3. 因此，M0 spike 觀察到的「注入 `permission.edit:"deny"` 也未擋下」現象，**責任邊界很可能落在 acpx 端**（acpx 生成 opencode 子進程時，沒有把設定正確傳遞進去），**而非 opencode ACP server 架構性地繞過權限協商**。這點目前尚待 acpx/herdr-bridge 整合團隊查核確認（Phase 0 報告已列查核請求）。
4. **同一輪 Phase 0 也發現並在 opencode dev 上實測重現、修復了一個真實、獨立的缺陷**（稱 G1）：child/subagent session（由 `task` 工具在 server 端生成）從未被註冊進 ACP session store，導致其 `permission.asked` 事件在 `ask` 情境下讓底層 `Permission.ask` 的 `Deferred` 永遠不 resolve，使整個 `session/prompt` 呼叫死結。此缺陷已用 RED→GREEN 回歸測試驗證修復（fork 端，尚未送上游 PR）。

### 補充查證（2026-07-20，獨立於 Phase 0）——上游 issue tracker 佐證

與 Phase 0（`docs/acp-permission-wiring-design.md`）並行、獨立進行的查證：用 `gh issue view` 直接查詢 `anomalyco/opencode` repo 即時狀態（非網頁快照），找到與「deny 設了但沒生效」現象相關的既有 issue：

| Issue | 狀態（查證當下） | 內容 | 與本案關係 |
|---|---|---|---|
| [#8832](https://github.com/anomalyco/opencode/issues/8832) opencode not respecting permissions | **OPEN**（16 comments） | 設 `bash.git:"deny"`，opencode 照跑 git 指令 | 現象與 M0 spike 相似，**非 ACP 情境**（一般 CLI/TUI 用法） |
| [#16331](https://github.com/anomalyco/opencode/issues/16331) Permissions ignored | **OPEN**（40 comments） | `read` deny 規則對部分檔案不生效（`.env` 正確擋、其他檔案未擋） | 同上，非 ACP 情境 |
| [#12133](https://github.com/anomalyco/opencode/issues/12133) ACP 模式 child session 權限請求未轉發、卡死 | **CLOSED**（stateReason: COMPLETED，但見下方 PR 查證） | ACP 下 Task subagent 的 permission 請求未註冊進 session store、silently dropped 導致 hang | **與 Phase 0 §3.5 G1 一字不差吻合**——見下方 PR #13222 查證：關閉此 issue 的 PR **實際上沒有修這個根因** |
| [#6396](https://github.com/anomalyco/opencode/issues/6396) SDK 呼叫時 deny 被忽略 | **CLOSED**（closed as 使用者未傳 `directory` 參數，非真 bug） | 不適用——Phase 0 exp-A/exp-B 已確認 config 有被正確載入 |

**#12133 關鍵查證（`gh pr view 13222`，非猜測，直接讀 PR 內容）**：關閉 #12133 的是 PR #13222「fix: resolve ACP hanging indefinitely in thinking state on Windows」——其 Root cause / Fix / Changes 三段完整內容是：Bun 的 `$` shell 讓 git 子行程繼承 ACP 的 stdin pipe，在 Windows 上造成 git 指令死結（`Snapshot.track()` 卡住）；修法是新增 `util/git.ts`，在 ACP 模式下用 `Bun.spawn({stdin:"ignore"})` 執行 git。**這個 PR 完全沒有動到 `acp/permission.ts`、`acp/session.ts`，跟「child session 未註冊進 ACP session store」這個根因毫無關係**——PR 描述欄位裡列了一串「Fixes #7632/#7587/#3730/#12133/#7587/#13024」，很可能是作者（或流程）把表面症狀相似（都叫「ACP 卡住/hang」）的多個 issue 一次性掛上同一個 PR，其中 #12133 被誤帶入清單。issue 討論串本身也印證這不是完整修復：留言者 jonchun（PR 作者）在 2026-02-04 明確寫「I think my PR for ACP didn't consider sub-subagents」——**承認自己的修法範圍有限**，但連「一層 subagent」這個最基本情境（我們的 G1 測試場景）都仍會複現死結，說明這個 PR 對 #12133 描述的根因是**完全沒修，不是部分修**。

### 決定性對照組實驗（2026-07-20）——排除版本差異與 acpx 變因

M0 spike 團隊（另一 pane）三次測試皆透過 acpx 生成 opencode 子進程，且用的是 npm 發布版 `opencode-ai@1.18.3`；本設計早先的 Phase 0 實測則是從 opencode dev git checkout 用 `bun run` 直接跑，兩者版本不同、且都經過各自的呼叫路徑，尚未有一個「同版本、排除 acpx」的乾淨對照組。

補做此對照組：直接從 npm registry 下載並安裝**真正發布的** `opencode-ai@1.18.3`（`npm view opencode-ai@1.18.3 dist.tarball` 取得的平台專屬預編譯二進位，非原始碼、非 dev checkout），手寫一個最小 stdio JSON-RPC driver（不透過 acpx）直接跟 `opencode acp` 子進程對話，配置 `OPENCODE_CONFIG` 指向 `{"permission":{"edit":"deny"}}`，用一個自建的假 LLM HTTP server 逼模型呼叫 `write` 工具建立檔案。

**結果**：`session/prompt` 正常回應 `stopReason:"end_turn"`，**檔案未落地**——deny 正確生效。

這同時排除兩個變因：
- **版本差異**：測的是跟 M0 spike 完全相同的 npm 發布版 1.18.3，不是 dev HEAD。
- **acpx 本身**：完全不經 acpx，自建 client 直接對話。

M0 spike 團隊三次測試（皆經 acpx）都失敗、本次對照組（不經 acpx，同版本）成功——**唯一變數是 acpx 這一層，H4（acpx 端 config 傳遞問題）證據強度大幅提升，接近確定**。

### 根因確認（2026-07-20）——直接讀 acpx 原始碼，非推測

授權跨專案查證後，直接安裝並讀取本機的 `acpx@0.12.0` 實際原始碼（`/opt/homebrew/lib/node_modules/acpx/dist/*.js`，非 minified 但保留可讀的函式邊界），追完「`--deny-all` flag → 子進程環境」這條完整鏈路：

1. **`--approve-all`/`--deny-all`/`nonInteractivePermissions` 純粹是 acpx 自己的 ACP client 端行為，不影響子進程環境。** `createConnection()` 註冊了一個完全正確的 `requestPermission: async (params) => this.handlePermissionRequest(params)` handler——acpx 對 ACP 協定本身的實作沒有問題，這些 flag 只決定「acpx 收到 agent 發來的 `session/request_permission` 時怎麼自動回答」。
2. **acpx 從未把這些 flag 轉換成任何要傳給子進程的環境變數。** 追到最底層的 `buildAgentEnvironment(authCredentials, sessionEnv)`：
   ```js
   function buildAgentEnvironment(authCredentials, sessionEnv) {
     const env = { ...process.env };          // 直接繼承 acpx 自己進程的環境
     // ...auth credential 注入，與權限無關...
     if (sessionEnv) for (...) assignSessionEnv(env, key, value);  // 僅呼叫端明確提供才會加
     return env;
   }
   ```
   `sessionEnv` 只來自 `this.options.sessionOptions?.env`——一個完全獨立、需要呼叫端手動提供的欄位。**沒有任何程式碼路徑會自動把 `permissionMode`/`nonInteractivePermissions` 轉成 env 變數塞進去。**
3. **因此完整因果鏈是**：opencode 本地權限引擎預設 `"*":"allow"`，只有它自己載入的本地 config（`OPENCODE_CONFIG`/project `opencode.json`）說「ask」時才會發 `session/request_permission`；acpx 沒有任何機制主動幫 opencode 設定這份本地 config；所以在「未額外手動注入本地 config」的情況下，opencode 永遠不會問，acpx 的 `--deny-all` 回答邏輯永遠沒有機會被觸發。**這不是 opencode 的 bug，也不是 acpx 的 bug，是兩邊權限模型之間一個沒人填的架構縫隙**：acpx 假設「agent 會自己主動問」，但沒有內建機制幫「本地決定要不要問」這種 agent（如 opencode）設定本地 config。

**M0 spike 測試 2、3 手動注入仍未生效的原因，範圍已收斂到兩個具體、可直接查驗的項目**（不再是模糊的「傳遞問題」）：
1. wrapper script 設定 `OPENCODE_CONFIG` 時，是否真的 `export` 到啟動 acpx 那個 shell 的環境——`buildAgentEnvironment` 是 `{...process.env}`，只會繼承 acpx 進程當下實際看到的環境變數，若 wrapper 在子 shell 或未 export 的情況下設定，acpx 自己都看不到，更不會往下傳。
2. project `opencode.json` 實際放置的路徑，是否與 acpx 真正拿去 spawn 子進程用的 `cwd`（`buildAgentSpawnOptions(cwd, ...)` 的 `cwd` 來自 `this.options.cwd`，即 acpx 對該 session 認定的工作目錄）完全一致——若有落差，opencode 的 project-config 探索會找不到那個檔案。

**對 M1 `AcpxAdapter` 設計的直接意涵**：acpx 有 `sessionOptions.env` 這個公開的注入接縫，這正是 M1 該用的機制。但實測發現這個接縫**只在直接呼叫 acpx 內部函式庫時存在**——目前 acpx CLI 本身完全沒有任何指令列選項能設定它（`sessionOptionsFromGlobalFlags()` 硬寫死只組 `{model, allowedTools, maxTurns, systemPrompt}`，不含 `env`）。所以實務上更直接的做法是：**herdr-bridge 自己 spawn acpx 子進程時，直接在那個子進程的環境變數設 `OPENCODE_CONFIG`**——因為 `buildAgentEnvironment()` 對 opencode 只是單純 `{...process.env}` 繼承，這是作業系統層級的基本行為，不需要 acpx 提供任何額外支援。

### 決定性真實環境測試（2026-07-20）——兩個新發現，修正並補強前述結論

授權後直接用**真正的全域 `acpx`（0.12.0）+ 真正的全域 `opencode`（1.18.3）+ 真實 API 憑證（OpenCode Go / OpenRouter）+ 真實模型**跑了兩次端到端測試（非模擬 LLM），結果修正了前述部分推論：

**測試 A**：`OPENCODE_CONFIG` 設 `{"permission":{"edit":"deny"}}`，直接對 acpx spawn 出的 opencode 子進程環境變數設定（不透過任何 acpx 特殊機制），提示詞要求建立檔案。

- 模型第一次嘗試呼叫 opencode 原生 `write` 工具 → **報錯「工具不存在」**（`Model tried to call unavailable tool 'write'. Available tools: ...`，清單裡沒有 `write`/`edit`，只有各種 MCP 提供的工具，例如 `filesystem_write_file`）。
- 模型隨即改呼叫 `filesystem_write_file`（filesystem MCP server 提供）→ **檔案正常建立，完全未被擋下**。

**根因（讀原始碼確認，非猜測）**：`packages/opencode/src/session/tools.ts:390-408` 顯示 MCP server 提供的工具**確實會經過 `ctx.ask(...)` 檢查**，但檢查用的 `permission` key 是**工具自己的名字**（例如 `"filesystem_write_file"`），**不是 `"edit"`**：
```js
for (const [key, entry] of Object.entries(yield* mcp.tools())) {
  // ...
  item.execute = (args, opts) => run.promise(Effect.gen(function* () {
    // ...
    yield* ctx.ask({ permission: key, metadata: {}, patterns: ["*"], always: ["*"] })
    return yield* Effect.promise(() => execute(args, opts))
  }))
}
```
由於 agent 出廠預設帶 `"*":"allow"`（`agent/agent.ts:119-120`），而使用者只設了 `permission.edit:"deny"`（沒有針對 `filesystem_write_file` 這個特定 key、也沒設更廣的 `"*"` 規則），evaluate() 對這個 MCP 工具名稱找不到專屬規則，直接落到 `"*":"allow"` 通過。**這與 acpx 傳遞問題完全獨立、是另一個真實存在的落差**：`permission.edit:"deny"` 只保護原生 `write`/`edit`/`apply_patch` 工具，任何 MCP server 提供的工具都要另外用其專屬名稱覆蓋、或直接用 `"*"` 這種涵蓋全部的規則，否則會被出廠的萬用 allow 蓋過。

**測試 B**：改用 `{"permission":"deny"}`（涵蓋 `"*"` 的最廣規則），提示詞改為「用任何可用工具建立檔案」。

- 模型這次選擇把任務**委派給 subagent**（呼叫 `task` 工具）——不是我刻意誘導的，是模型自己的選擇。
- 整個 acpx 呼叫**卡死超過 90 秒逾時**，程序被強制終止（無殘留進程）。

**這是 G1 在完全真實、未修改的生產環境（真實 acpx + 真實 opencode 1.18.3 + 真實模型）下的自然重現**——不是我刻意設計的合成測試，是模型在被要求「更嚴格權限」時自然選擇委派子任務、然後撞上 child session 死結。這比先前用假 LLM 腳本重現的證據更有力：**G1 不是理論邊界案例，是一旦採用 deny-by-default（`"*"` 規則）就會在真實使用中自然浮現的實際故障模式。**

**對 M1 設計的最終修正**：
1. `AcpxAdapter` 寫入的 `OPENCODE_CONFIG` 必須用**涵蓋 `"*"` 的規則**（例如 `{"permission":{"*":"deny", "read":"allow", ...白名單}}`），不能只設 `edit`，否則 MCP 工具會透過出廠萬用 allow 繞過去——這是獨立於 acpx 傳遞問題之外，另一個必須在 M1 設計時就處理的落差。
2. 正因為第 1 點要求用更廣的 deny 規則，G1（child session 死結）在 M1 實際上線後**很可能不是低機率邊緣案例，而是常態會撞到的問題**（模型在權限收緊時傾向委派子任務）。這直接提高了「G1 上游 PR」的優先順序——不再只是「順便送一送」，而是 M1 採用嚴格權限政策後必須有解法（送 PR 讓上游採納、或 M1 自己在 policy 層面暫時停用/限制 opencode 系 tier 的 Task 委派能力，直到上游修復合併為止）。

**這份補充查證與 Phase 0 的關係（誠實揭露，不誇大佐證力道）**：#8832/#16331 描述的現象與 M0 spike 表面相似，但 Phase 0 的原始碼查證已確認 `acp/permission/tool` 相關路徑在 1.18.3→HEAD 之間零 commit 改動；#8832/#16331 描述的是**一般 CLI/TUI 情境**（非 `opencode acp` 子進程／ACP 協定路徑），無法確認是否為同一段程式碼、同一根因——**不構成對 H4（acpx 傳遞問題）任一方向的直接佐證或反證**，只能佐證「opencode 的 permission 系統在多個情境下有已知、目前仍開放的可靠性缺口」這個更弱的一般性觀察。**[#12133](https://github.com/anomalyco/opencode/issues/12133) 則不同——經 PR 內容核實後，這是對 G1 的直接、獨立、上游社群已回報的佐證，且確認「已關閉」標籤具有誤導性（根因從未真正被修）。** Phase 1（若啟動）送出的修復 PR 應明確引用 #12133 並指出先前的關閉不涵蓋此根因，避免上游 maintainer 誤以為重工。

**Decision 本身（下方第 1–4 點，強制 workdir/worktree 隔離）不因此校正而撤銷或放寬**，原因：
- worktree 隔離是縱深防禦，即使 opencode 主 session 的 ACP 權限協商本身運作正常，也不代表「信任 opencode 進程」是安全的（見下方「已知邊界」第 1 點：worktree 隔離本來就不假設協商是唯一防線）。
- G1（child session 缺口）在你方查核/opencode upstream 正式合併修復之前，仍是真實存在的風險面；即使主 session 沒問題，child session 曾經（在你查核清楚 acpx 傳遞問題之前也可能仍然）繞過協商。
- `policy_enforced` 欄位語意（下方第 4 點）建議維持現狀，直到 acpx 端傳遞問題查核有結論、且 G1 修復確認上游採納後，再重新評估是否需要調整。

## Decision

1. **opencode 系 tier（`oc-dspro`/`oc-dsflash`/`oc-kimi` 及未來新增的 opencode-backed tier）一律視為 untrusted FS actor。** `herdr_bridge.acp` 不得在 API 語意、文件或 audit 記錄中暗示 acpx 的 permission policy（`--approve-all`/`--approve-reads`/`--deny-all`/`--permission-policy`）對 opencode tier 有實際強制力——這些 flag 對 opencode tier 目前確認為空轉。
2. **workdir/git worktree 隔離，從 M1 起對 opencode 系 tier 為強制預設，不是 M2 opt-in、也不是「達到某個觸發條件才升 opt-out」——觸發條件即本次 M0 實證本身。** `ensure_session(actor_id, agent, workdir, ...)` 對 opencode 系 agent 要求 `workdir` 為顯式、非共用、非主要工作樹的路徑；`AcpxAdapter` 對 opencode 系 agent 的 session 建立，若 `workdir` 指向 repo 主要工作樹或與其他既有 session 共用，應拒絕/告警（具體實作機制留給 M1）。
3. `claude` tier 不受此限制——ACP 權限協商對 claude adapter 確認有效，可依 ADR 0002 §4.6 既定的 S0→S1→S2 路徑正常演進。
4. `AcpPolicy` 模型（設計文件 §4.2）對 opencode tier 的欄位仍保留（供未來 opencode upstream 修正後啟用、或未來透過 AcpTransport 接縫換官方 SDK 時重新評估），但 audit 記錄需明確標記 `policy_enforced: false`（opencode 系）vs `policy_enforced: true`（claude 系），避免上層誤判「已設 policy = 已受控」。**`policy_enforced` 目前僅為 audit 記錄欄位，M1 階段沒有 runtime enforcement layer 讀取它做裁決——這是刻意的單點依賴（見下方「已知邊界」第 3 點），M2 若要加 runtime enforcement 需另行設計消費者。**

此決策同時解決 ADR 0002 標頭「共識附帶記錄」中懸而未決的一項：「M2 驗收項需補 ADR：worktree 隔離從 opt-in 升 opt-out 的觸發條件」——觸發條件確認成立，時點提前至 M1（非 M2）。

### 已知邊界（PPLX 對抗式審查 round2 要求明確聲明，非事後補述）

1. **worktree 隔離是 workdir 層級，不是 process 層級或 filesystem namespace 層級。** opencode 進程本身若執行 `git checkout`/`git reset`/`git stash`/`git gc`、修改 `.git/config`、`.git/hooks/`，或透過其 shell 執行能力 `cd ..`/使用絕對路徑，**完全不受此機制約束**——worktree 共用同一個 git object database，這些操作可能影響到 worktree 邊界之外。**worktree 隔離只防「acpx/herdr_bridge 自己發出的 session 定址」誤指到共用路徑，不防「opencode 進程主動繞出去」。** 後者屬於 process-level/OS-level 隔離（container、namespace、或至少唯讀掛載非目標路徑）的範疇，**明確列為 M2 強化項，不在 M1 範圍內**，未來維護者不應誤讀 M1 的 workdir 檢查為完整隔離。
2. **`ensure_session()` reject 邏輯最低實作規格**（M1 必須落地，非 ADR 文字了事）：
   - 路徑比對一律先 `os.path.realpath()`／`Path.resolve()`，禁止純字串前綴比對（防 symlink/hardlink 繞過）。
   - 用 `git worktree list --porcelain` 取得目前已知 worktree 路徑集合，`workdir` 若與 repo 主要工作樹路徑相同、或與任何既有 session 已佔用的 workdir 路徑相同（resolve 後比對），拒絕並回傳明確錯誤，記入 audit。
   - 檢查時機：至少覆蓋 `ensure_session()` 呼叫當下；**session 建立後 opencode 透過工具呼叫改變自身 cwd 或建立新檔案於 workdir 之外，M1 不做執行期攔截**（這是第 1 點「workdir 層級非 process 層級」邊界的直接後果，非遺漏），此限制需同步寫入 `herdr_bridge.acp` 的 api.md。
3. **`policy_enforced` 欄位目前只進 audit，沒有 runtime enforcement layer 消費它做裁決。** 即：M1 的實際隔離有效性完全依賴 `ensure_session()` 那一個檢查點；若呼叫端繞過 `ensure_session()` 直接建構 session（理論上不應該，但沒有程式層面的強制），這個單點依賴沒有第二道防線。此風險明確記錄，不視為 M1 阻斷項，但 M1 的 api.md／BOUNDARIES 需誠實揭露此限制，避免未來被當作「已有 runtime enforcement」誤用。

## Consequences

- 正面：M1 設計不會建立在錯誤的安全假設上；opencode tier 的真實授權範圍（workdir 邊界）從一開始就明確、可稽核。
- 代價：opencode tier 無法使用 S1/S2 的細粒度權限白名單（approve_reads 等）作為實際約束——這條路徑對 opencode tier **不可行**，除非 opencode upstream 修正其 ACP server 實作。governance 層若需要對 opencode tier 做更細粒度限制（例如僅允許讀特定路徑），只能透過 workdir 邊界本身的縮小（例如更細粒度的 worktree 切分），不能指望 acpx policy flag。
- audit 記錄新增 `policy_enforced` 欄位（bool），如實反映實際約束力，不是「已設定」= 「已生效」。
- 待辦（非阻塞 M1，M1 開發中平行處理）：向 opencode upstream 查證此行為是否為已知限制或 bug（比照 #13644 的處理慣例）；若後續版本修正，重新評估是否可放寬 workdir 強制隔離為 opt-in。

- **Confidence**: 90%（實測證據紮實：兩種獨立機制皆確認無效、有 claude 對照組排除「acpx 全面失靈」的可能；根因未深入 opencode 原始碼，留 10% 給「特定版本/設定組合下例外可行」的可能性）——**此信心水準的根因假設已於 2026-07-20 更新**（見上方「更新」小節）：opencode 原始碼查證 + 實測顯示協商機制本身健全，「10% 例外可行」的方向已部分證實（透過官方支援的兩種 config 傳遞機制皆可行），惟 acpx 端實際傳遞路徑仍待查核，故 Decision 維持不變，Confidence 數字暫不調整。

## acpx queue-owner 連線中斷自動重試（2026-07-21）

真實派工實測發現：`AcpPolicy(mode="deny-all")` 時，若模型委派 subagent（呼叫 `task` 工具），acpx 的 `run_prompt()`／`wait_done()` 可能回報 `reason="error"`，錯誤訊息包含 `"needs reconnect"`。acpx 原始碼 `probeQueueOwnerHealth()` 顯示這代表背景 queue-owner 行程對 agent 子行程的 IPC socket 探測失敗（`socketReachable: false`）。

### 實作防線

`AcpxTransport.run_prompt()` 與 `wait_done()` 現有一層通用偵測+重試邏輯：

- **偵測條件**：`result.reason == "error"` 且 `"needs reconnect" in (result.error or "")`——以錯誤訊息字串比對觸發，不限定特定 policy 或 subagent 委派情境（根因尚未 100% 確定）。
- **重試方式**：呼叫 `ensure_session()` 重新建立 acpx queue-owner 連線後，再執行一次完整的 prompt 呼叫。
- **重試次數**：硬性上限 **一次**，避免 opencode 持續崩潰時無限重建循環。
- **日誌**：使用 `logger.warning()`（非 debug）記錄「Detected 'needs reconnect' for session X — retrying once after re-establishing session」。
- **重試仍失敗**：回傳原始 error 訊息，不包裝或吞掉診斷資訊。

### 已知限制

此重試是止血方案——它緩解「queue-owner 連線中斷」的表面症狀，但**不會修復根因**（為什麼 deny-all + subagent 委派情境下 queue-owner 的 IPC socket 會斷）。根因調查仍在進行，見 `docs/tech-debt-cleanup-plan.md` 問題 1。

## 實作完成狀態（2026-07-20，Phase D；更新 2026-07-21 WP6）

`docs/acpx-adapter-implementation-plan.md` 規劃的 `AcpxAdapter`（`src/herdr_bridge/acp/adapter.py`）已完成 Phase A-E 全部階段：opencode fork G1 修復（commit `7c12cd101`、上游 PR [anomalyco/opencode#37902](https://github.com/anomalyco/opencode/pull/37902)）、`adapter.py` 三支純函式 TDD 實作（單元測試 18 個、100% 覆蓋率）、對真實 acpx + patched opencode 二進位 + 真實模型的整合測試（3 個，涵蓋 deny-all 阻擋原生/wildcard 寫入、G1 子任務死結回歸、approve-reads 讀寫分流）、以及獨立對抗式驗證（發現並修正一項 CRITICAL：`OPENCODE_CONFIG` 並非 opencode 設定合併鏈最終權威，專案層 `opencode.json` 與 `OPENCODE_CONFIG_CONTENT` 皆可在其後覆蓋，已補 `OPENCODE_DISABLE_PROJECT_CONFIG=true` + 主動清除 `OPENCODE_CONFIG_CONTENT` 修正）。

### WP6：B2 Claude → AcpxTransport（2026-07-21）

- `acpx` 0.12.0 已原生支援 `claude` 具名子指令：`acpx --cwd ... --ttl ... --non-interactive-permissions deny claude sessions ensure`（全域 flag 放在 `claude` 之前）。
- `_default_agent_resolver()` 已擴充接受 `"claude"`：先查 `CLAUDE_BIN` env var，再 fallback `shutil.which("claude")`。
- `build_acpx_argv_and_env()` 依 `agent` 參數分流：opencode 走 `--agent` escape hatch + `OPENCODE_CONFIG` env；claude 走 `--cwd` global flag（`claude` 子指令由 transport 層附加），不設任何 opencode 專屬 env var。
- `AcpxTransport.ensure_session()` 對 claude 跳過 `write_session_config()`（`config_path=None`）。
- `_BUILTIN_AGENTS` 已更新為 `("opencode", "claude")`，`list_acp_agents()` 同時回報兩者。
- `policy_enforced`：claude 維持 `True`（`agent != "opencode"` 分支，已既有）。
- 單元測試：`TestResolveClaudeBinary`（4 個）、`TestBuildAcpxArgvAndEnvClaude`（5 個）、`TestDefaultAgentResolverClaude`（4 個）。
- 整合測試：`TestAcpxTransportClaudeIntegration`（2 個，`@pytest.mark.integration`，CI deselect）。
- `AcpTransport` Protocol 已存在且凍結——WP7（SDK transport）可據此開工。

此 ADR 上方「Decision」與「已知邊界」段落**維持不變**——本次實作解決的是 opencode 本地 config 如何被可靠寫入/生效的問題，不改變「opencode 系 tier 一律視為 untrusted FS actor、workdir/worktree 隔離為強制預設」這個核心決策；`AcpxAdapter` 是在此決策之上補的第二道防線（config 層面的權限收斂），不是取代 workdir 隔離。詳細實作/驗證細節見 `docs/acpx-adapter-implementation-plan.md`。
