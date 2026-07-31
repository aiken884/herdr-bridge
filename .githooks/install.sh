#!/usr/bin/env bash
# 啟用 .githooks/ 底下所有 git hook：
#   - commit-msg   強制 Signed-off-by（DCO 1.1）
#   - post-commit  自動把 commit 摘要寫回 RemaGraph（章程 §3.5.9 第 4 點）
#
# 用 `git config core.hooksPath` 取代手動複製到 .git/hooks/：
# .git/hooks/ 不受版控管理，每次 clone 都要重複手動安裝；core.hooksPath
# 指向版控目錄，clone 一次、跑一次這支腳本即可。
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

chmod +x .githooks/commit-msg .githooks/post-commit
git config core.hooksPath .githooks

echo "已設定 core.hooksPath=.githooks，下列 hook 現已啟用："
echo "  - commit-msg   (DCO Signed-off-by 檢查)"
echo "  - post-commit  (自動寫回 RemaGraph 記憶)"
