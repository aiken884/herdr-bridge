# Security Policy

## Supported Versions

herdr-bridge is pre-1.0. Security fixes land on the latest `0.x` release line.
The five public function signatures are frozen as of `0.1.0`; security patches
are additive and do not break them.

| Version | Supported |
|---------|-----------|
| latest `0.1.x` | ✅ |
| older | ❌ (upgrade to latest) |

## Reporting a Vulnerability

**Please do not open a public issue for security problems.**

Report privately via GitHub's **private vulnerability reporting**:
Security → *Report a vulnerability* on this repository. This opens a private
advisory visible only to the maintainers.

Please include:
- affected version(s),
- a description and impact assessment,
- reproduction steps or a proof of concept if you have one.

We aim to acknowledge within a few working days and to agree a disclosure
timeline with you before any public advisory.

## Scope

In scope — issues in herdr-bridge's own code, for example:
- unsafe handling of data read from the Herdr socket,
- command/argument injection in the paths herdr-bridge controls,
- unsafe file or permission handling by the audit logger,
- resource-exhaustion reachable through the public API.

Out of scope:
- **Herdr itself.** herdr-bridge is an independent client of the Herdr socket
  API. Vulnerabilities in the Herdr server belong to that project. herdr-bridge
  drives a local Herdr server you install and run yourself.
- Misuse of the library against sessions or panes you are not authorised to
  control — herdr-bridge is a thin, policy-neutral layer; access control is the
  responsibility of the code you build on top of it.

## Trust Model (what herdr-bridge assumes)

- The local Herdr Unix socket and the process on the other end are **trusted**;
  herdr-bridge parses the server's NDJSON responses but does not treat the
  local server as an adversary.
- `actor_id` / `priority` / `mode` are **recorded, not enforced** — they are
  not an authentication or authorization mechanism (see `README.md`, ADR 0001).
- Audit logs may contain agent output and socket paths; protect the audit
  directory accordingly (files are created private to the user).

---

# 資安政策

## 支援版本

herdr-bridge 目前還是 pre-1.0 專案。安全性修補只會發布在最新的 `0.x` 版本線上。
五個公開函式簽章已在 `0.1.0` 凍結;安全性修補只會用增補的方式進行,不會破壞這些簽章。

| 版本 | 是否支援 |
|------|----------|
| 最新 `0.1.x` | ✅ |
| 較舊版本 | ❌(請升級到最新版)|

## 回報漏洞

**請不要為資安問題開公開 issue。**

請透過 GitHub 的**私下漏洞回報**機制回報:到這個 repo 的 Security → *Report a
vulnerability*。這樣會開一個只有維護者看得到的私人 advisory。

回報時請附上:
- 受影響的版本,
- 問題描述與影響評估,
- 重現步驟或概念驗證(如果有的話)。

我們會在幾個工作天內回覆確認,並在公開任何 advisory 之前先跟你談好揭露時程。

## 涵蓋範圍

在範圍內——herdr-bridge 自己程式碼裡的問題,例如:
- 從 Herdr socket 讀進來的資料處理不當,
- herdr-bridge 掌控的路徑上出現指令/參數注入,
- 稽核記錄(audit logger)的檔案或權限處理不安全,
- 透過公開 API 可觸發的資源耗盡。

不在範圍內:
- **Herdr 本身。** herdr-bridge 是 Herdr socket API 的一個獨立客戶端。Herdr 伺服器本身的
  漏洞屬於那個專案的範疇。herdr-bridge 驅動的是你自己安裝、自己執行的本機 Herdr 伺服器。
- 濫用這個函式庫去操控你沒有授權的 session 或 pane——herdr-bridge 是一層薄薄的、不涉政策
  判斷的介面;存取控制的責任在建立於它之上的程式碼身上。

## 信任模型(herdr-bridge 假設什麼是可信的)

- 本機的 Herdr Unix socket,以及在另一端的那個行程,都被視為**可信任**;herdr-bridge 會
  解析伺服器回傳的 NDJSON 回應,但不會把本機伺服器當成潛在攻擊者來防範。
- `actor_id` / `priority` / `mode` 只是**被記錄下來,而不是被強制執行**——它們不是一種
  認證或授權機制(詳見 `README.md`、ADR 0001)。
- 稽核記錄可能包含 agent 的輸出內容與 socket 路徑;請妥善保護稽核記錄目錄(這些檔案在建立時
  就已限定只有本機使用者本人可讀)。
