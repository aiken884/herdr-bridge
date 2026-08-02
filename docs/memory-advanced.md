# Herdr Bridge Memory — advanced / under the hood

This page is for advanced users, integrators, and contributors who need direct
access to the underlying backend, want to understand what's actually
happening, or are debugging a memory-related failure. Regular users don't
need any of this — see the "Automatic memory (Herdr Bridge Memory)" section
of [`docs/light-user-quickstart.md`](light-user-quickstart.md) instead.

## What powers it

Herdr Bridge Memory is powered by [RemaGraph](https://github.com/aiken884/remagraph)
(PyPI package [`remagraph`](https://pypi.org/project/remagraph/)), an
independent, separately maintained, Apache-2.0-licensed open-source project.
herdr-bridge embeds it as a hard dependency (see `pyproject.toml`) and wraps
it behind a single import boundary
(`src/herdr_bridge/orchestration/memory.py`) so the rest of herdr-bridge, and
its end users, don't need to know the backend's name to use the feature. This
is a branding/UX decision about a fully-disclosed public dependency, not a
secrecy measure — the dependency, its license, and its project are named
plainly in `pyproject.toml`, `NOTICE`, and `CONTRIBUTING.md`.

## Full environment variable reference

| Variable | Status | What it does |
|---|---|---|
| `HERDR_MEMORY_MODE` | Current | `auto` (default) / `on` / `off` — controls whether Herdr Bridge Memory is active |
| `HERDR_REMAGRAPH_MODE` | Deprecated alias | Same effect as `HERDR_MEMORY_MODE`; still works, but reading it emits a `DeprecationWarning` |
| `HERDR_MEMORY_PROJECT` | Current | The memory project id; takes priority over `--project` flag defaults and over `REMAGRAPH_PROJECT` |
| `REMAGRAPH_PROJECT` | RemaGraph-native | RemaGraph's own project-id variable. herdr-bridge sets this internally before every backend call. Setting it yourself directly (without `HERDR_MEMORY_PROJECT`) is an **intentional escape hatch** — see below |
| `REMAGRAPH_STATE_DIR` | RemaGraph-native, internal | Where the backend's per-project database lives. herdr-bridge computes and sets this automatically from the project id; there is no `HERDR_MEMORY_*` alias for it because normal use never needs to set it directly |

Priority order for resolving the effective project id (CLI flags like
`dispatch --project`, `notify-pane --project`, `doctor --project`):
**`--project` flag > `HERDR_MEMORY_PROJECT` > `REMAGRAPH_PROJECT` > `herdr-bridge` (default)**.

## Advanced escape hatch: bypassing the naming layer

If you already know RemaGraph and want to control it directly — for example
you're also running the standalone `remagraph` CLI against the same project,
or scripting something that needs the backend's native environment variable
names — you can set `REMAGRAPH_PROJECT` (and, if you really need to,
`REMAGRAPH_STATE_DIR`) directly. herdr-bridge treats this as a deliberate
choice by an advanced user, not an error condition: it will work exactly as
RemaGraph itself would interpret it. `HERDR_MEMORY_PROJECT`, when set, always
wins over a directly-set `REMAGRAPH_PROJECT`.

## Seeing the full error detail (`-v` / `--verbose`)

By default, `herdr-commander` prints a clean, non-backend-named message when
a Herdr Bridge Memory operation fails (a `HerdrMemoryError`, or another
`HerdrBridgeError` subclass) and exits non-zero. The original underlying
exception — which may name RemaGraph, show internal paths, or include a full
traceback — is preserved on the exception's cause chain but not printed by
default. Pass `-v` / `--verbose` *after* the subcommand (e.g.
`herdr-commander doctor -v`, `herdr-commander memory note ... -v`) to see the
full chain instead, which is the right first step when debugging a memory
backend failure.

## Manually logging a memory note

Regular users should use the CLI escape hatch:
```bash
herdr-commander memory note "<message>" --task-id <task-id> --agent-id <agent-id> [--project <project>]
```

This is the Herdr Bridge Memory equivalent of `store_memory()`'s own CLI
fallback. If you're working directly with the underlying backend (e.g. from a
downstream project that doesn't have `herdr-commander` on its `PATH`), the
equivalent raw call is:
```bash
remagraph store --project <project> --task-id <task-id> --agent-id <agent-id> --kind status_update --summary "<message>"
```
(Note: `remagraph auto -- "<command>"` is a *different* RemaGraph feature — it
executes `<command>` as a real shell command and logs the result, rather than
storing `<command>` itself as a note. Don't confuse the two.)

## License and attribution

RemaGraph is licensed under Apache-2.0, the same license as herdr-bridge
itself. See [`NOTICE`](../NOTICE) for the full attribution, and
[`pyproject.toml`](../pyproject.toml) for the exact version constraint in
use.

---

# Herdr Bridge Memory — 進階／底層實作說明

本頁面給需要直接存取底層後端、想了解實際運作機制、或正在排查記憶相關失敗的
進階使用者、整合者與貢獻者看。一般使用者不需要看這頁——請參考
[`docs/light-user-quickstart.md`](light-user-quickstart.md) 的「自動記憶功能
（Herdr Bridge Memory）」那一節即可。

## 底層是什麼

Herdr Bridge Memory 是由 [RemaGraph](https://github.com/aiken884/remagraph)
（PyPI 套件 [`remagraph`](https://pypi.org/project/remagraph/)）驅動的——一個
獨立維護、Apache-2.0 授權的開源專案。herdr-bridge 把它當作必要依賴內嵌
（見 `pyproject.toml`），並包在單一 import 邊界內
（`src/herdr_bridge/orchestration/memory.py`），讓其餘的 herdr-bridge 程式碼
與一般使用者都不需要知道底層後端的名字就能使用這個功能。這是針對一個完全
公開揭露的依賴所做的品牌／使用者體驗決定，不是保密措施——這個依賴、它的授權
與專案本身，都清楚具名寫在 `pyproject.toml`、`NOTICE`、`CONTRIBUTING.md` 裡。

## 完整環境變數對照表

| 變數 | 狀態 | 作用 |
|---|---|---|
| `HERDR_MEMORY_MODE` | 現行 | `auto`（預設）／`on`／`off`——控制 Herdr Bridge Memory 是否啟用 |
| `HERDR_REMAGRAPH_MODE` | 已棄用別名 | 效果同 `HERDR_MEMORY_MODE`；仍可使用，但讀取時會發出 `DeprecationWarning` |
| `HERDR_MEMORY_PROJECT` | 現行 | 記憶專案 id；優先權高於 `--project` flag 的預設值，也高於 `REMAGRAPH_PROJECT` |
| `REMAGRAPH_PROJECT` | RemaGraph 原生 | RemaGraph 自己的專案 id 變數。herdr-bridge 每次呼叫底層前都會內部自動設定它。直接自己設定它（不設 `HERDR_MEMORY_PROJECT`）是**刻意保留的逃生口**——見下方說明 |
| `REMAGRAPH_STATE_DIR` | RemaGraph 原生、內部使用 | 該後端每個專案的資料庫存放位置。herdr-bridge 會依專案 id 自動算出並設定，一般使用不需要、也沒有 `HERDR_MEMORY_*` 對應別名 |

CLI flag（如 `dispatch --project`、`notify-pane --project`、`doctor --project`）
解析有效專案 id 的優先順序：
**`--project` flag > `HERDR_MEMORY_PROJECT` > `REMAGRAPH_PROJECT` > `herdr-bridge`（預設值）**。

## 進階逃生口：繞過命名轉換層

如果你本來就熟悉 RemaGraph，想直接控制它——例如你同時也在對同一個專案跑
獨立的 `remagraph` CLI，或是在寫腳本需要用到底層原生的環境變數名稱——可以
直接設定 `REMAGRAPH_PROJECT`（真的有需要的話，也可以設 `REMAGRAPH_STATE_DIR`）。
herdr-bridge 會把這當成進階使用者的刻意選擇，不當成錯誤：行為會完全比照
RemaGraph 本身的解讀方式。只要有設定 `HERDR_MEMORY_PROJECT`，它一律優先於
直接設定的 `REMAGRAPH_PROJECT`。

## 查看完整錯誤細節（`-v` / `--verbose`）

預設情況下，`herdr-commander` 在 Herdr Bridge Memory 操作失敗時（`HerdrMemoryError`
或其他 `HerdrBridgeError` 子類別），只會印出乾淨、不具名底層後端的訊息並回傳
非 0 exit code。原始的底層例外——可能會提到 RemaGraph、內部路徑，或完整
traceback——會保留在例外的 cause chain 上，但預設不印出來。在子指令**之後**加上
`-v` / `--verbose`（例如 `herdr-commander doctor -v`、
`herdr-commander memory note ... -v`）即可看到完整鏈，這是排查記憶後端失敗時
該做的第一步。

## 手動記錄一筆記憶

一般使用者請用 CLI 逃生口：
```bash
herdr-commander memory note "<訊息>" --task-id <task-id> --agent-id <agent-id> [--project <project>]
```

這是 `store_memory()` 自己的 CLI fallback 對應的包裝版本。如果你是在直接跟底層
後端打交道（例如某個下游專案沒有把 `herdr-commander` 裝在 `PATH` 上），對應的
原始呼叫是：
```bash
remagraph store --project <project> --task-id <task-id> --agent-id <agent-id> --kind status_update --summary "<訊息>"
```
（注意：`remagraph auto -- "<指令>"` 是**另一個**不同的 RemaGraph 功能——它會把
`<指令>` 當成真正的 shell 指令執行並記錄結果，而不是把 `<指令>` 本身存成一筆
備註。不要搞混這兩者。）

## 授權與歸屬

RemaGraph 採用 Apache-2.0 授權，跟 herdr-bridge 本身相同。完整歸屬說明見
[`NOTICE`](../NOTICE)，目前使用的確切版本限制見 [`pyproject.toml`](../pyproject.toml)。
