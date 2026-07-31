# herdr-bridge git hooks

本目錄下的 hook 透過 `core.hooksPath` 啟用，**不是**手動複製到 `.git/hooks/`——
`.git/hooks/` 不受版控管理，每個 clone 都要重複安裝；`core.hooksPath` 指向這個
版控目錄，安裝一次即可跨 clone 生效。

## 安裝

```bash
bash .githooks/install.sh
```

等效於：

```bash
git config core.hooksPath .githooks
chmod +x .githooks/commit-msg .githooks/post-commit
```

## 卸載

```bash
git config --unset core.hooksPath
```

## Hook 清單

### `commit-msg`（DCO Signed-off-by 檢查）

依 DCO 1.1（Developer Certificate of Origin）強制 commit message 含
`Signed-off-by: Name <email>` trailer，缺則阻擋 commit（exit 1）。CI 不依賴
此 hook——DCO 合規由 GitHub 的 DCO app / 人工 review 確保，此 hook 純粹是
本機提早攔截的開發便利。

用 `git commit -s` 自動附加 sign-off。

### `post-commit`（自動寫回 RemaGraph 記憶）

章程 §3.5.9 第 4 點：「回收前必寫」不靠每個 agent 自己記得在收工時手動呼叫
`remagraph store`，而是把這個動作嵌進 commit 流程本身——每次 commit 完成後，
自動用該 commit 的 message／變更檔案清單呼叫一次 `remagraph store`，
`kind=status_update`。

**agent-agnostic**：純 shell + git 原生機制，任何底層 agent（Claude Code、
OpenCode、Grok、Codex、AGY……）只要執行 `git commit` 就會觸發，不需要 agent
本身認識 RemaGraph 或做任何額外整合。

**欄位推導規則**：

| 欄位 | 來源 |
|------|------|
| `--project` | 主 repo 目錄名稱（用 `git rev-parse --git-common-dir` 往上一層取得，**worktree 安全**——即使在 `git worktree` 子目錄下 commit，推導出的仍是主 repo 名稱，不是 worktree 自己的目錄名） |
| `--task-id` | `<project>-commit-<commit 短 hash>` |
| `--agent-id` | 環境變數 `AGENT_ID`（優先）或 `git config user.name`；經 slugify 轉為小寫英數字元／底線／連字號，長度補到至少 3 碼 |
| `--summary` | `Commit <短hash> (<完整hash>) on branch <branch> in <project>: <commit subject>`（刻意保留足夠長的 ASCII 前綴，確保滿足 RemaGraph summary ≥30 字的仲裁規則，即使 commit subject 很短也不會被拒絕） |
| `--learnings` | 本次 commit 變更的檔案清單（JSON 陣列）；若沒有變更檔案（極少見）則退回一筆固定佔位內容，避免因 RemaGraph「learnings 不可為空」規則被拒絕 |
| `--handoff-note` | commit 的完整訊息（含 body） |

**降級策略（不可違反）**：

- 沒裝 `remagraph`（`command -v remagraph` 失敗）→ 靜默略過，僅印一行提示到
  stderr，commit 正常完成，不阻擋、不報錯。
- `remagraph store` 執行失敗（任何原因：資料庫、仲裁規則被拒、dedup 等）→
  印一行帶錯誤訊息的提示到 stderr，commit 仍正常完成。post-commit 本來就
  無法阻擋 commit，但此 hook 額外保證不會讓使用者看到未捕捉的 stack trace。

相容性：bash 3.2+（macOS 內建 bash）、無外部依賴（純 git + coreutils）。

## 驗證安裝

```bash
git config --get core.hooksPath        # 應輸出 .githooks
ls -la .githooks/commit-msg .githooks/post-commit   # 應皆可執行

# 手動觸發一次 post-commit（不需要真的 commit）：
.githooks/post-commit
```
