# Light User Quickstart

**Who this is for**: weekend builders and occasional taskers — people who use AI coding agents now and then, not as a full-time job.

## Three Steps to Get Started

### 1. Install

```bash
# You need Herdr first: https://herdr.dev
pip install herdr-bridge
# Or for local development:
# cd herdr-bridge && uv sync
```

### 2. Prepare your workspace

```bash
# Option A: one-command sandbox (recommended for your first try)
bash scripts/commander-start.sh --sandbox

# Option B: you already have a Herdr session running
herdr-commander start
```

### 3. Run a task

```bash
herdr-commander run --dry-run                    # preview first (default: thumbnail task)
herdr-commander run                              # actually run the thumbnail task
herdr-commander run --use-acp-router             # route to real downstream agents via the ACP router (auto)
herdr-commander router list                      # see the 4 built-in agents + dynamic registry
```

When it's done, herdr-commander tells you in plain language:
- whether it succeeded
- what it produced
- what you should check next

Currently supported narrow scenarios:
- thumbnail-py (default): a Python thumbnail function + tests
- fastapi-health: a simple FastAPI /health endpoint + tests

(More scenarios are being added over time.)

---

## Commands you'll use

| Command | What it does |
|---|---|
| `herdr-commander start` | Check the environment, see if any assistants are available |
| `herdr-commander status` | List current assistants |
| `herdr-commander run` | Run the default task (thumbnail) |
| `herdr-commander run --task fastapi-health` | Run the FastAPI health-endpoint task |
| `herdr-commander run --dry-run` | Preview only, don't dispatch |

**You don't need to know about**: fleet.yaml, actor_id, the routing engine, or socket paths.

## Currently supported narrow scenarios

- **thumbnail-py** (default): Python thumbnail function + unit tests
- **fastapi-health**: simple FastAPI /health endpoint + tests

Switch between them with `--task`. More scenarios will keep being added so occasional users have more to choose from.

---

## What's the first task?

Build a Python thumbnail function with unit tests (see `docs/first-task-spec.md` for the full spec).

Just run `herdr-commander run`, and herdr-commander will:
1. automatically pick a suitable assistant
2. describe the task clearly and dispatch it
3. wait for it to finish
4. report the result back to you in plain language

---

## When something goes wrong

| Situation | What to do |
|---|---|
| Can't find herdr | Install it first: https://herdr.dev |
| No assistants available | Start at least one AI in Herdr (e.g. claude) |
| An assistant is waiting for confirmation | Switch to the Herdr window and press Enter / follow the on-screen prompt |
| Timeout | Run it again, or make the request more specific |

Add `-v` for technical details when troubleshooting (you normally won't need this).

## Automatic memory (Herdr Bridge Memory)

herdr-bridge has a built-in memory feature, **Herdr Bridge Memory**, for AI agent workflows.

**For regular users**:
- Herdr Bridge Memory is built in and enabled automatically — herdr-commander will:
  - recall prior memories for the same task before dispatching
  - inject a summary into the prompt
  - tell the agent how to record its own progress
- If you set `HERDR_MEMORY_MODE=off`, or the memory backend's CLI isn't on your `PATH`, everything else still works exactly the same — this feature just doesn't activate. (If the memory backend's Python package itself is missing or broken — a corrupted install, since it's a required dependency — `herdr-commander` won't start at all; reinstall herdr-bridge to fix this.)

You can control it with an environment variable:
```bash
HERDR_MEMORY_MODE=off   herdr-commander run   # fully disable memory features
HERDR_MEMORY_MODE=on    herdr-commander run   # force it on (useful for development)
```
(The older `HERDR_REMAGRAPH_MODE` name still works, but is deprecated — switch to `HERDR_MEMORY_MODE`.)

Example of recording progress manually, from the CLI:
```bash
herdr-commander memory note "just finished this step" --task-id herdr-20260722-xxx --agent-id your-agent-name
```

See [`docs/memory-advanced.md`](memory-advanced.md) if you want to know what's under the hood, or need direct access to the underlying backend.

## Stability improvements ("Option C") and a guide for external projects

herdr-bridge 0.3+ shipped a round of single-machine, cross-project stability improvements (retries + routing), internally nicknamed "Option C":

- **Retry strategy**: when the transport layer detects a "needs reconnect" condition, it rebuilds the session and **retries exactly once** (beyond that it surfaces the original error, so non-idempotent commands don't get executed twice). Connection failures get bounded retries with exponential backoff.
- **Stronger routing**: the ACP router first tries to match downstream agents by their registered capabilities, then falls back to keyword matching (code/research/general), with a clear fallback chain. External agents that register with capabilities get matched automatically to the most suitable task.
- **Cross-project acknowledgement**: a shared task_id plus Herdr Bridge Memory gives both sides visibility — whichever side stores first, the other can recall and see the full history.

**Integration guide for external projects** (for developers of other projects who want to plug in):

1. **Register your agent into the fleet** (a dynamic, persistent registry):
   ```bash
   herdr-commander router register my-research "uv run -m my_research" --cap research,analyze --desc "external research agent"
   herdr-commander router list
   ```

2. **Let herdr route tasks to it** (automatically, or by name):
   ```bash
   herdr-commander run --use-acp-router --router-target my-research "please analyze this with your capability..."
   ```
   Or call `from herdr_bridge.acp.router import AcpRouter` directly from Python.

3. **Cross-project coordination + memory acknowledgement** (the shared foundation underneath all of the above):
   Agree on the same task_id on both sides, then run:
   ```bash
   herdr-commander memory note "received via ACP and processing, progress..." --task-id herdr-bridge-20260722-xxx --agent-id my-agent
   ```
   See `examples/coordination/remagraph-cross-project.py` for a full working example (with 4 real downstream agents and isolated worktrees) — the example script itself talks directly to the underlying backend by its real name, since it's developer-facing source code, not the CLI-facing surface this doc otherwise describes.

**Important boundary**: herdr-bridge is strictly scoped to a single machine (an isolated worktree + local Herdr Bridge Memory). Cross-machine / networked use is out of scope (see `BOUNDARIES.md`).

Reference docs:
- `BOUNDARIES.md`, `docs/api.md`

With this, external projects can reliably join herdr's fleet of agents and participate in cross-project memory coordination.

---

## Full `herdr-commander` command reference

`herdr-commander` is the CLI entry point for `herdr_bridge.light.cli:main`, with 8 subcommands:

| Subcommand | What it does |
|---|---|
| `start` | Check and prepare the workspace |
| `status` | View the current status of assistants |
| `run` | Run a task (default: thumbnail-py; switch with `--task`) |
| `router` | ACP router control (server + client + registry) — manage dynamic registration/discovery/routing of downstream agents |
| `dispatch` | Dispatch to a specific downstream agent through the formal `AcpRouter.dispatch_with_memory_confirm`, with PONG/side-channel delivery confirmation |
| `notify-pane` | Notify an interactive TUI pane (layer 4: atomic keystroke injection + screen-diff delivery verification) — use this for non-ACP-compliant agents (already-running TUIs), not `dispatch` |
| `doctor` | One-shot health check for the communication channels: global install, Herdr Bridge Memory connectivity, `project.json` consistency, whether the maintenance loop has run away |
| `memory note` | Log a Herdr Bridge Memory note by hand (`herdr-commander memory note "<message>" --task-id <id> --agent-id <id>`) — see the "Automatic memory" section above |

### `dispatch` (layer 2, ACP, via AcpRouter)

```bash
herdr-commander dispatch --target <pane_id or registered agent name> "<prompt>"
herdr-commander dispatch --pane-id <pane_id> --project <memory-project> --timeout <seconds> "<prompt>"
```

| Flag | Description |
|---|---|
| `--target` | Target pane_id or registered agent name (must provide this or `--pane-id`; no automatic routing) |
| `--pane-id` | Explicit pane_id (usable when `--target` isn't given) |
| `--project` | Herdr Bridge Memory project id (defaults to `HERDR_MEMORY_PROJECT`, otherwise `herdr-bridge`) |
| `--name` | Fleet member display name (for logging) |
| `--timeout` | Overall CLI-level timeout in seconds |

**Current status**: `agent-client-protocol` has been promoted from an optional extra to a core dependency (see `dependencies` in `pyproject.toml`) — this layer is available by default and you no longer need to install the optional extra by hand. Broken installs, stale lockfiles, and similar edge cases still degrade explicitly (`ok=False`, `degraded=True`, `delivery_status="not_attempted"`) rather than silently reporting success.

### `notify-pane` (layer 4 — use this for interactive TUIs)

```bash
herdr-commander notify-pane --pane <pane_id> --tui <agy|claude|codex|copilot|gemini|grok|opencode> "<message>"
```

| Flag | Description |
|---|---|
| `--pane` | Target pane_id (required; no automatic routing) |
| `--project` | Herdr Bridge Memory project id (defaults to `HERDR_MEMORY_PROJECT`, otherwise `herdr-bridge`; only affects the audit log, not where the message actually goes) |
| `--retries` | Max retries for delivery verification (each attempt includes atomic injection + an Enter fallback if uncommitted), default 3 |
| `--settle-delay` | Seconds to wait after each injection/Enter fallback for the screen to finish rendering, default 0.35 |
| `--read-lines` | Number of lines sampled per `herdr pane read`, default 40 |
| `--tui` | Explicitly specify the target TUI (submission is detected via a per-TUI pattern); if omitted, all known patterns are tried in order |
| `--allow-busy` | Allow injecting into a pane whose `agent_status` is `working` (rejected by default — interrupt-style TUIs like grok can drop the previous job; only use this when you're sure the target is a queueing-style TUI like claude/agy) |
| `--ready-timeout` | Seconds to wait, before injecting, for the TUI to render a known prompt, default 5.0 (right after an agent starts, `agent_status` may report idle before the TUI is actually ready) |
| `--no-audit` | Skip writing to the Herdr Bridge Memory audit log (for testing) |

**This is currently the only reliable communication channel for interactive TUIs**: atomic keystroke injection (a real newline sent in one shot, avoiding the TUI event-loop race that happens when Enter is sent as a separate step) plus screen-diff delivery verification. If delivery still can't be confirmed within the retry budget, it fails loudly rather than silently claiming success.

**TUI brands currently supported by `--tui`, and their verification status**: `claude`, `agy`, `codex`, `grok`, `opencode`, `copilot`, and `gemini` — all seven have registered patterns and have been verified end-to-end. **Kimi CLI and CodeBuddy CLI are not yet supported** — no pattern has been registered for them, and there's no committed timeline. Adding support would follow the same process as the others: real end-to-end testing, screen verification, and registering a new pattern.

### `doctor` (one-shot diagnostics)

```bash
herdr-commander doctor
herdr-commander doctor --project <memory-project>
```

Checks four things, and returns a non-zero exit code if any of them fail:

1. Whether `herdr-commander`/`herdr` are both on PATH (whether the global install is actually working)
2. Whether the Herdr Bridge Memory backend can connect (actually runs a `search`)
3. Whether the `project_id` recorded in `project.json` under the state dir matches what's expected (a mismatch can mean a different project accidentally wrote into this one)
4. Whether the number of `maintenance_completed` events in the last hour's audit log looks abnormal (more than 30 suggests a runaway external cleanup loop)

This command exists because these four things once had to be checked manually, one at a time, to track down a real problem — now there's one command for "something feels off."

### `router`

```bash
herdr-commander router list                                              # list known agents
herdr-commander router discover --path <path>                            # scan and expand the registry
herdr-commander router route --prompt "<text>" [--target <agent>]        # route to an agent in the registry
herdr-commander router register <name> "<command>" --capabilities code,general
herdr-commander router unregister <name>
```

### Global install, so panes from any project can talk to each other

By default, `herdr-commander` only lives inside herdr-bridge's own worktree `.venv`, so panes from other projects can't call it and fall back to the raw `herdr pane send-text`/`send-keys` — the exact commands this doc keeps warning about (they send Enter as a separate step, which races the TUI event loop, and they don't verify delivery).

The fix: install it globally with `pipx` (the same pattern used for other CLIs, installed to `~/.local/bin`, which is already on every pane's PATH):

```bash
pipx install --editable /path/to/herdr-bridge
```

Once installed:

- **Any pane, in any project**, as long as it shows up in `herdr pane list`, can call `herdr-commander notify-pane --pane <target>` / `dispatch --target <target>` directly — regardless of which project the caller or the target pane belongs to. `--pane`/`--target` address panes managed globally by Herdr, not something scoped to the herdr-bridge project.
- Because the install is `--editable`, pointing at the herdr-bridge main worktree, improvements on the main branch take effect automatically — but if that directory ever gets moved or deleted, the global command breaks with it. That's the one coupling point to be aware of.
- This has been tested end-to-end: two panes from two different projects, running different TUIs, sent each other test messages via the globally installed `herdr-commander notify-pane`, and both confirmed delivery successfully.

---

## Overview of the five communication channels (this doc is the source of truth — if other docs or messages disagree on syntax, this one wins)

| # | Channel | Role | Delivery confirmation |
|---|---|---|---|
| 1 | Herdr Bridge Memory `store`/`search` | Primary — a general-purpose, persistent, cross-project mailbox | None in real time; relies on the other side recalling it |
| 2 | ACP `dispatch` | Secondary — PING/PONG correlation | Yes |
| 3 | side-channel (Unix socket) | Tertiary — used by fleet members to report completion | Yes |
| 4 | `herdr-commander notify-pane` | Layer 4 — the only reliable channel for interactive TUIs | Yes (screen-diff verified) |
| 5 | raw `herdr pane send-text` | Herdr's lowest-level primitive | **None** — returns as soon as it's sent, doesn't verify the agent actually processed it |

**Use 1–4 whenever you can.** Raw `herdr pane send-text` has no delivery confirmation and is prone to the TUI event-loop race (especially when text and Enter are sent as two separate steps) — treat it as a last resort only when 1–4 aren't available.

### Raw `herdr pane` command reference (syntax verified against real behavior, not copied from other docs)

The subcommands under `herdr pane` have **inconsistent syntax** — worth paying close attention to:

| Subcommand | Syntax | How the pane is specified |
|---|---|---|
| `send-text` | `herdr pane send-text <PANE_ID> <TEXT>` | **Positional argument** — there is no `--pane` flag |
| `read` | `herdr pane read [OPTIONS] <PANE_ID>` | **Positional argument**; plus `--source {visible,recent,recent-unwrapped,detection}`, `--lines <N>`, `--format {text,ansi}` |
| `list` | `herdr pane list [--workspace <ID>]` | No single pane needs to be specified |
| `process-info` | `herdr pane process-info [--pane <ID>] [--current]` | **Flag** (unlike `send-text`/`read` — easy to mix up) |

Examples:
```bash
herdr pane send-text wT:p1E $'message text, sent in one shot with a real newline\n'   # correct: positional + trailing real newline
herdr pane read wT:p1E --lines 20 --source recent               # correct: positional + optional flags
herdr pane process-info --pane wT:p1E                            # correct: this one actually is a flag
```

### Maintenance rule

Anyone referencing `herdr`/`herdr-commander` syntax in a broadcast message, a doc, or a conversation with another agent should check this doc's tables first rather than typing it from memory. If the command-line interface changes in the future, this doc needs to be updated in the same commit — don't let the docs drift from actual behavior.

---

# 輕度使用者快速開始

**目標對象**：週末建造者 / 偶爾出任務者——偶爾用一下 AI coding agent 的人，不是全職在用。

## 三步開始

### 1. 安裝

```bash
# 需要先有 Herdr：https://herdr.dev
pip install herdr-bridge
# 或本機開發：
# cd herdr-bridge && uv sync
```

### 2. 準備工作環境

```bash
# 方式 A：一鍵沙盒（推薦第一次試用）
bash scripts/commander-start.sh --sandbox

# 方式 B：你已有 Herdr session 在跑
herdr-commander start
```

### 3. 執行任務

```bash
herdr-commander run --dry-run                    # 先預覽（預設縮圖任務）
herdr-commander run                              # 實際執行縮圖任務
herdr-commander run --use-acp-router             # 使用 ACP Router 路由到真實下游 agents（auto）
herdr-commander router list                      # 查看 4 agents + 動態 registry
```

完成後會用白話告訴你：
- 是否完成
- 產生了哪些東西
- 建議你檢查什麼

目前支援的窄場景：
- thumbnail-py（預設）：Python 縮圖函式 + 測試
- fastapi-health：簡單 FastAPI /health 端點 + 測試

（更多窄場景會陸續加入）

---

## 你會用到的指令

| 指令 | 做什麼 |
|---|---|
| `herdr-commander start` | 檢查環境、看有沒有助手 |
| `herdr-commander status` | 列出目前助手 |
| `herdr-commander run` | 執行預設任務（縮圖） |
| `herdr-commander run --task fastapi-health` | 執行 FastAPI 健康端點任務 |
| `herdr-commander run --dry-run` | 只預覽不派工 |

**你不需要知道**：fleet.yaml、actor_id、規則引擎、socket 路徑。

## 目前支援的窄場景

- **thumbnail-py**（預設）：Python 縮圖函式 + 單元測試
- **fastapi-health**：簡單 FastAPI /health 端點 + 測試

使用 `--task` 切換。更多窄場景會持續加入，讓偶爾使用者有選擇。

---

## 第一個任務是什麼？

建立一個 Python 縮圖函式 + 單元測試（詳見 `docs/first-task-spec.md`）。

你只要執行 `herdr-commander run`，herdr-commander 會：
1. 自動選一個適合的助手
2. 把任務說清楚並派出去
3. 等它做完
4. 用你聽得懂的話回報結果

---

## 出問題時

| 情況 | 怎麼辦 |
|---|---|
| 找不到 herdr | 先裝 https://herdr.dev |
| 沒有助手 | 在 Herdr 裡啟動至少一個 AI（如 claude） |
| 助手在等確認 | 到 Herdr 視窗按 Enter / 依畫面操作 |
| 逾時 | 再跑一次，或把需求說得更具體 |

除錯時可加 `-v` 看技術細節（一般不需要）。

## 自動記憶功能（Herdr Bridge Memory）

herdr-bridge 內建了 **Herdr Bridge Memory**，一種給 AI agent 工作流程用的記憶機制。

**一般使用者**：
- Herdr Bridge Memory 內建且自動啟用，herdr-commander 會：
  - 派工前讀取同任務的之前記憶
  - 把摘要注入 prompt
  - 告訴 agent 要怎麼記錄進度
- 如果你設定 `HERDR_MEMORY_MODE=off`，或記憶後端的 CLI 不在 `PATH` 上，其餘功能完全不受影響，
  這個特性單純不會啟動而已。（如果記憶後端的 Python 套件本身就缺失或損毀——它是必要依賴——
  `herdr-commander` 會完全無法啟動；重新安裝 herdr-bridge 即可修復。）

你可以用環境變數控制：
```bash
HERDR_MEMORY_MODE=off   herdr-commander run   # 完全關閉記憶功能
HERDR_MEMORY_MODE=on    herdr-commander run   # 強制開啟（適合開發）
```
（舊名稱 `HERDR_REMAGRAPH_MODE` 仍可使用，但已棄用——請改用 `HERDR_MEMORY_MODE`。）

手動記錄範例（透過 CLI）：
```bash
herdr-commander memory note "我剛完成這步" --task-id herdr-20260722-xxx --agent-id 你的助手
```

想了解底層實作細節，或需要直接存取底層後端，請見 [`docs/memory-advanced.md`](memory-advanced.md)。

## 穩定性改善（Option C）與外部專案使用指南

herdr-bridge 0.3+ 已完成一輪單機跨專案穩定性改善（重試 + routing），內部代號「Option C」：

- **重試策略**：transport 層偵測 "needs reconnect" 時自動重建 session 並**只重試一次**（超過回傳原始錯誤，避免非冪等指令重複執行）。connect 失敗有有限重試 + 指數退避。
- **路由強化**：AcpRouter 優先用 registry capabilities 精準匹配，其次關鍵字（code/research/general），有明確 fallback 鏈。外部 agent 註冊時帶 capabilities，router 自動選最適合。
- **跨專案 ack**：shared task_id + Herdr Bridge Memory 雙向可見。任一方 store 後另一方 recall 即可追蹤完整歷史。

**給外部專案的整合指南**（其他專案開發者適用）：

1. **註冊你的 agent 加入艦隊**（動態 registry，支援持久化）：
   ```bash
   herdr-commander router register my-research "uv run -m my_research" --cap research,analyze --desc "外部研究 agent"
   herdr-commander router list
   ```

2. **讓 herdr 路由任務給它**（自動或指定）：
   ```bash
   herdr-commander run --use-acp-router --router-target my-research "請用你的能力分析..."
   ```
   或在 Python 使用 `from herdr_bridge.acp.router import AcpRouter` 直接呼叫。

3. **跨專案協調 + 記憶 ack**（前面兩點共同的基礎）：
   雙方約定相同 task_id，執行：
   ```bash
   herdr-commander memory note "已透過 ACP 收到並處理，進度..." --task-id herdr-bridge-20260722-xxx --agent-id my-agent
   ```
   完整範例見 `examples/coordination/remagraph-cross-project.py`（含 4 真實下游 + 隔離 worktree）——這支範例腳本本身是給開發者看的原始碼，直接使用底層後端的真實名稱，跟本文其餘部分描述的 CLI 介面不同層級。

**重要邊界**：herdr-bridge 嚴格限定單機（isolated worktree + 本地 Herdr Bridge Memory）。跨機器/網路不在範圍內（見 `BOUNDARIES.md`）。

參考文件：
- `BOUNDARIES.md`、`docs/api.md`

這樣外部專案就能可靠地被納入 herdr 的艦隊，並參與跨專案記憶協調。

---

## `herdr-commander` 完整指令參考

`herdr-commander` 是 `herdr_bridge.light.cli:main` 的 CLI 入口，共 8 個子指令：

| 子指令 | 做什麼 |
|---|---|
| `start` | 檢查並準備工作環境 |
| `status` | 查看目前助手狀態 |
| `run` | 執行任務（預設 thumbnail-py，可用 `--task` 切換） |
| `router` | ACP Router 控制（Server + Client + registry），管理下游 agent 的動態註冊/發現/路由 |
| `dispatch` | 派工到指定下游 agent，走正式 `AcpRouter.dispatch_with_memory_confirm`，有 PONG/side-channel 送達確認 |
| `notify-pane` | 通知互動式 TUI pane（第四層：原子鍵盤注入 + 畫面 diff 送達驗證），非 ACP-compliant agent（已啟動的 TUI）用這個，不要用 `dispatch` |
| `doctor` | 一鍵診斷通訊管道健康度：全域安裝、Herdr Bridge Memory 連線、`project.json` 對應、maintenance 迴圈是否失控 |
| `memory note` | 手動記錄一筆 Herdr Bridge Memory 備註（`herdr-commander memory note "<訊息>" --task-id <id> --agent-id <id>`）——見上方「自動記憶功能」一節 |

### `dispatch`（第二層 ACP，走 AcpRouter）

```bash
herdr-commander dispatch --target <pane_id 或已註冊 agent 名稱> "<prompt>"
herdr-commander dispatch --pane-id <pane_id> --project <memory-project> --timeout <秒> "<prompt>"
```

| 旗標 | 說明 |
|---|---|
| `--target` | 目標 pane_id 或已註冊 agent 名稱（與 `--pane-id` 至少擇一，禁止自動路由） |
| `--pane-id` | 明確指定 pane_id（未提供 `--target` 時可用此指定） |
| `--project` | Herdr Bridge Memory project id（預設讀 `HERDR_MEMORY_PROJECT`，否則 `herdr-bridge`） |
| `--name` | fleet member 顯示名稱（記錄用） |
| `--timeout` | CLI 層整體逾時秒數 |

**現況**：`agent-client-protocol` 已從 optional extra 升為主依賴（見 `pyproject.toml` 的 `dependencies`），這一層預設可用，不必再手動裝 optional extra。損壞安裝／舊 lockfile 等異常情況仍會明確降級（`ok=False`、`degraded=True`、`delivery_status="not_attempted"`），不會靜默假成功。

### `notify-pane`（第四層，互動式 TUI 用這個）

```bash
herdr-commander notify-pane --pane <pane_id> --tui <agy|claude|codex|copilot|gemini|grok|opencode> "<訊息>"
```

| 旗標 | 說明 |
|---|---|
| `--pane` | 目標 pane_id（必填，禁止自動路由） |
| `--project` | Herdr Bridge Memory project id（預設讀 `HERDR_MEMORY_PROJECT`，否則 `herdr-bridge`；僅影響稽核記錄，不影響實際 pane 定位） |
| `--retries` | 送達驗證重試上限（每次含原子注入 + 未提交時的 Enter fallback），預設 3 |
| `--settle-delay` | 每次注入/補 Enter 後等待畫面 render 完成的秒數，預設 0.35 |
| `--read-lines` | 每次 `herdr pane read` 取樣的行數，預設 40 |
| `--tui` | 明確指定目標 TUI（提交判定用 per-TUI pattern）；不指定時自動依序嘗試所有已知 pattern |
| `--allow-busy` | 允許對 `agent_status=working` 的 pane 注入（預設拒絕；grok 等中斷型 TUI 會弄丟前一則工作，確定目標是排隊型 TUI 如 claude/agy 時才加這個旗標） |
| `--ready-timeout` | 注入前等待 TUI 渲染出已知提示符的逾時秒數，預設 5.0（agent 剛啟動時 `agent_status` 立刻回報 idle 但 TUI 可能還沒 ready） |
| `--no-audit` | 跳過 Herdr Bridge Memory 稽核記錄（測試用） |

**這是目前唯一對互動式 TUI 可靠的通訊管道**：原子鍵盤注入（真正換行一次送出，避開分成兩步送 Enter 時的 TUI event loop race condition）+ 畫面 diff 驗證送達，重試上限內都無法確認送達會明確報錯，不會靜默假成功。

**目前 `--tui` 支援的品牌與驗證狀態**：`claude`／`agy`／`codex`／`grok`／`opencode`／`copilot`／`gemini` 七種都已登記 pattern 並實測驗證。**Kimi CLI、CodeBuddy CLI 目前未支援**——尚未登記 pattern，也沒有明確時程；之後若要支援，會走跟其他品牌一樣的流程：實機端到端測試、畫面驗證、補上 pattern 登記。

### `doctor`（一鍵診斷）

```bash
herdr-commander doctor
herdr-commander doctor --project <memory-project>
```

檢查四件事，任何一項有問題就回傳非 0 exit code：

1. `herdr-commander`／`herdr` 是否都在 PATH 上（全域安裝是否生效）
2. Herdr Bridge Memory 後端能不能連上（實際跑一次 `search`）
3. state_dir 裡的 `project.json` 記的 `project_id` 是否跟預期一致（不一致代表可能被別的 project 誤連寫入）
4. 過去一小時稽核 log 裡 `maintenance_completed` 次數是否異常（超過 30 次視為疑似有外部程序在跑失控清理迴圈）

這個指令的由來：這四項曾經全靠手動一項項排查才抓到問題，收斂成這個指令後，之後遇到「怎麼感覺怪怪的」就先跑這個。

### `router`

```bash
herdr-commander router list                                              # 列出已知 agents
herdr-commander router discover --path <path>                            # 掃描擴充 registry
herdr-commander router route --prompt "<text>" [--target <agent>]        # 路由到 registry 裡的 agent
herdr-commander router register <name> "<command>" --capabilities code,general
herdr-commander router unregister <name>
```

### 全域安裝，讓任何專案的 pane 都能互相通訊

`herdr-commander` 預設只在 herdr-bridge 自己 worktree 的 `.venv` 裡，其他專案的 pane 呼叫不到，只能退回裸的 `herdr pane send-text`／`send-keys`——這正是本文件其他章節反覆在提醒的那個原始指令（分兩步送 Enter 而踩 TUI event loop race、送出不驗證送達）。

解法：用 `pipx` 把它裝成全域指令（跟其他 CLI 同一模式，裝到 `~/.local/bin`，已在所有 pane 的 PATH 上）：

```bash
pipx install --editable /path/to/herdr-bridge
```

裝好之後：

- **任何 pane、任何專案**，只要出現在 `herdr pane list` 裡，都能直接呼叫 `herdr-commander notify-pane --pane <目標>` / `dispatch --target <目標>`，跟呼叫者或目標當下所屬的專案／工作目錄無關——`--pane`/`--target` 定位的是 Herdr 全域管理的 pane，不是 herdr-bridge 專案內部概念。
- 因為是 `--editable` 指向 herdr-bridge 的 main worktree，main 分支之後的改進會自動生效；但如果這個目錄被搬走或刪除，全域指令會跟著壞掉——這是唯一的耦合點。
- 已實測驗證：兩個不同專案、跑不同 TUI 的 pane，用全域安裝的 `herdr-commander notify-pane` 互相送達測試訊息，雙方都送達確認成功。

---

## 五條通訊管道總覽（本文件為 SOT，其他文件/訊息若語法衝突以此為準）

| # | 管道 | 定位 | 送達確認 |
|---|---|---|---|
| 1 | Herdr Bridge Memory `store`/`search` | Primary，跨專案通用信箱，持久記錄 | 無即時確認，靠對方 recall |
| 2 | ACP `dispatch` | Secondary，PING/PONG correlation | 有 |
| 3 | side-channel（Unix socket） | Tertiary，艦隊成員回報完工用 | 有 |
| 4 | `herdr-commander notify-pane` | 第四層，互動式 TUI 唯一可靠管道 | 有（畫面 diff 驗證） |
| 5 | 裸 `herdr pane send-text` | Herdr 平台最底層原始指令 | **無**，送出即回傳，不驗證是否真的被 agent 處理 |

**能用 1-4 就不要用 5**：裸 `herdr pane send-text` 沒有送達確認、容易踩 TUI event loop race（分兩步送文字+Enter 時尤其明顯），只在 1-4 都不可用時的最後手段用。

### 裸 `herdr pane` 指令參考（實測校驗，非文件抄寫）

`herdr pane` 底下的子指令**語法不一致**，寫的時候特別注意：

| 子指令 | 語法 | pane 怎麼給 |
|---|---|---|
| `send-text` | `herdr pane send-text <PANE_ID> <TEXT>` | **位置參數**，沒有 `--pane` 旗標 |
| `read` | `herdr pane read [OPTIONS] <PANE_ID>` | **位置參數**；`--source {visible,recent,recent-unwrapped,detection}`、`--lines <N>`、`--format {text,ansi}` |
| `list` | `herdr pane list [--workspace <ID>]` | 不需要指定單一 pane |
| `process-info` | `herdr pane process-info [--pane <ID>] [--current]` | **旗標**（跟 `send-text`/`read` 不同，容易搞混） |

範例：
```bash
herdr pane send-text wT:p1E $'訊息文字，用真正換行一次送出\n'   # 正確：位置參數 + 真換行結尾
herdr pane read wT:p1E --lines 20 --source recent               # 正確：位置參數 + 選填旗標
herdr pane process-info --pane wT:p1E                            # 正確：這個反而是旗標
```

### 維護規則

任何人要在廣播訊息、文件、或跟其他 agent 的溝通裡引用 `herdr`/`herdr-commander` 指令語法時，先查這份文件的表格，不要憑記憶手打。這份文件如果之後指令介面有變動，要在同一個 commit 裡一併更新，避免文件跟實際行為分岔。
