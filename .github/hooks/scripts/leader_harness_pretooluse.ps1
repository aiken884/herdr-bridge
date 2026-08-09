# GitHub Copilot CLI preToolUse hook (Windows) — 最小可用版本，委派給 Python 腳本執行，
# 避免 PowerShell 欄位留空殼導致 Windows 環境下 hook 靜默不觸發。
$input | python3 "$PSScriptRoot/leader_harness_pretooluse.py"
exit $LASTEXITCODE
