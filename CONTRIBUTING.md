# Contributing to herdr-bridge

Thanks for considering a contribution. This document covers the developer workflow, testing conventions, versioning policy, and the scope boundary this repo enforces.

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency management.

```bash
git clone <this repo>
cd herdr-bridge
uv sync --dev
```

`uv sync --dev` creates `.venv/` and installs the dev dependency group (`pytest`, `ruff`, `mypy`, `jsonschema`). Runtime dependencies are intentionally empty (`dependencies = []` in `pyproject.toml`) — `jsonschema` is a dev-only dependency used solely by one golden test as an independent cross-check on the hand-written schema validator; it is never imported at runtime.

## Running the checks

```bash
uv run pytest -q                        # full test suite
uv run pytest -q -m "not integration"   # unit tests only (see "Contributor environment tiers" below)
uv run ruff check .
uv run mypy src
```

All four must be clean before a PR is reviewed. CI runs the same commands (`.github/workflows/ci.yml`) across an `ubuntu-latest` x `macos-latest` matrix and Python 3.11-3.14, always with `-m "not integration"`.

## Contributor environment tiers

Not every contribution needs a real Herdr installation:

- **Unit-only contributions** (the large majority: bug fixes, new tests, refactors that don't touch socket wire behavior) — **any OS** works. `uv sync --dev && uv run pytest -m "not integration"` runs entirely against `tests/fake_server.py`'s `FakeHerdrServer`, an in-process fake; no real `herdr` binary is needed or invoked.
- **Integration-level changes** (anything touching `client.py`'s socket handling, schema fetching, or behavior that depends on real Herdr semantics) — need **macOS, locally, with the `herdr` CLI installed**. These tests are marked `integration` and are deselected in CI (see "Why no devcontainer" below for the same underlying reasoning). If you're changing this layer, run the integration tests yourself before opening a PR and say so in the PR description.

### Cross-platform pitfalls

Read this before touching `client.py` or `tests/fake_server.py`:

- **`AF_UNIX` socket path length**: macOS's `sockaddr_un.sun_path` is capped at **104 bytes** (Linux allows more, but don't rely on that difference). `pytest`'s default `tmp_path` and macOS's default `tempfile` locations are frequently too long once a fixture nests a few directories deep. Always build test socket paths under a short prefix (`/tmp` plus a short unique suffix), and guard the assumption with an assertion rather than letting it fail as a mysterious `OSError` at connect time.
- **`SO_PEERCRED` is Linux-only.** macOS has no direct equivalent in use here (`LOCAL_PEERPID` exists on macOS but isn't wired into this codebase); there is currently no peer-identity verification at the socket layer. If you add any, gate it explicitly per platform rather than assuming portability.
- **`SIGPIPE`**: CPython sets `SIGPIPE` to `SIG_IGN` at interpreter startup, so a broken pipe surfaces as an ordinary `BrokenPipeError` (an `OSError` subclass) instead of killing the process. `client.py` relies on this and handles it through the existing `except OSError` paths — there's no need for `SO_NOSIGPIPE`/`MSG_NOSIGNAL` handling.

### Why no devcontainer

We deliberately do not ship a `.devcontainer/`. Herdr's socket API is a local Unix-domain-socket primitive; testing it through Docker Desktop for macOS means testing vpnkit's socket forwarding, not Herdr's actual behavior — for exactly the parts of this library that matter most, a container would validate the wrong thing. Combined with this being a zero-runtime-dependency, pure-stdlib library (so there's little environment drift for a container to guard against in the first place), the tradeoff isn't worth it today. If this changes in the future, a unit-only devcontainer (explicitly documented as not covering the `integration` test tier) is the likely shape it would take.

## TDD convention

This codebase was built test-first throughout its implementation. New behavior should follow the same pattern: write the failing test against `FakeHerdrServer` first, confirm it fails for the reason you expect, then implement until it passes. Bug fixes should add a regression test that fails before the fix and passes after.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, ...). This keeps the history scannable and the changelog easy to derive.

### Sign-off (DCO)

Every commit must include a `Signed-off-by` trailer (`git commit -s`) certifying compliance with the [Developer Certificate of Origin](https://developercertificate.org/) (DCO). CI enforces this — the [`.github/workflows/dco.yml`](.github/workflows/dco.yml) workflow checks all commits in every pull request and blocks merging if any commit is missing the sign-off.

### Local git hooks

Run `bash .githooks/install.sh` once after cloning to enable the local `commit-msg` (DCO) and `post-commit` (RemaGraph memory write-back) hooks — see [`.githooks/README.md`](.githooks/README.md) for what each hook does and how to uninstall.

## Versioning policy (semver, pre-1.0)

herdr-bridge follows [Semantic Versioning](https://semver.org/). While the version stays `0.x` — tracking Herdr's own pre-1.0 status, not this library's interface stability — the practical rule contributors should apply is:

- **Any change to a public function signature** — `list_agents`, `read_agent`, `send_to_agent`, `wait_until`, `acquire_control`, `connect`, or any field on `AgentInfo` / `AgentOutput` / `SendResult` / `WaitResult` — **is treated as a major-version-equivalent change** (bumps the second digit while pre-1.0, e.g. `0.1.0` -> `0.2.0`) and needs a clear justification in the PR description. These signatures were frozen as of `0.1.0` specifically so a future governance layer can depend on them; don't add, remove, reorder, or retype a parameter without discussing it first.
- Purely additive functionality (a new optional parameter with a default, a new function) is a minor bump.
- Bug fixes that change no signature are a patch bump.

## Governance-layer boundary

This repo is the **tool layer only** — `SocketClient` / `SessionCache` / `BridgeActions`. It does not and will not contain rule engines, scheduling policy, multi-tenant access control, or anything that interprets `actor_id` / `priority` / `mode` beyond recording them. That logic belongs in a separate governance-layer project that consumes this package through the public `BridgeActions` interface only — see [`docs/api.md`](docs/api.md) for the full contract.

Concretely: **PRs that add governance or policy logic to this repo will be redirected, not merged.** If you're building a rule engine, a priority scheduler, or an audit-log consumer, it belongs downstream of this package, not inside it.

## Memory facade boundary

Only `src/herdr_bridge/orchestration/memory.py` may `import remagraph` directly. Every other module that needs memory functionality — recall, storage, dispatch-text augmentation — must go through that module's public functions (`store_memory`, `recall_memories`, `prepare_dispatch_text`, etc.) rather than importing RemaGraph directly. This doesn't restrict code comments or internal identifiers (`is_remagraph_enabled`, docstrings, etc. elsewhere are fine) — the rule that matters is about *runtime output*: outside `-v`/`--verbose` output, no user-facing code path (CLI stdout/stderr, `--help` text, a prompt sent to a downstream agent, a stored memory record's summary/handoff_note) should print, log, or raise anything that names "RemaGraph" — the public feature name is "Herdr Bridge Memory". This keeps the branding abstraction consistent for future contributors, not just for this rename.

## Release checklist

Before tagging a release:

1. `uv run pytest -q && uv run ruff check . && uv run mypy src` — all clean.
2. `uv build` — confirm both an sdist and a wheel are produced under `dist/`.
3. Confirm `py.typed` made it into the wheel (the PEP 561 marker):
   ```bash
   uv run python -c "import zipfile, glob; print('py.typed' in zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist())"
   ```
4. **Fresh-venv install smoke test** — in a throwaway virtual environment (not this repo's `.venv`), install the exact released version and run the README quickstart against a running herdr:
   ```bash
   uv venv /tmp/herdr-bridge-smoke && source /tmp/herdr-bridge-smoke/bin/activate
   pip install herdr-bridge==X.Y.Z
   python -c "from herdr_bridge import connect; connect()"
   ```
   This is the check that catches "works in the dev venv but the wheel is missing a file" bugs before they reach users.
5. Update `CHANGELOG.md` with the actual release date (replacing the `TBD` placeholder) and open a new `[Unreleased]` section for what comes next.

Publishing itself is automated by [`.github/workflows/publish.yml`](.github/workflows/publish.yml), triggered on a GitHub Release, using PyPI's Trusted Publisher (OIDC) — no API tokens involved. The one step that can't be automated is the one-time PyPI project setup that points the Trusted Publisher at this repo; that only needs to happen once, done by the maintainer directly on pypi.org.

---

# 為 herdr-bridge 做貢獻

謝謝你考慮參與這個專案。這份文件涵蓋開發流程、測試慣例、版本策略,以及這個 repo 強制執行的
範圍邊界。

## 開發環境設定

這個專案用 [uv](https://docs.astral.sh/uv/) 來管理環境與相依套件。

```bash
git clone <this repo>
cd herdr-bridge
uv sync --dev
```

`uv sync --dev` 會建立 `.venv/`,並安裝開發用的相依套件群組(`pytest`、`ruff`、`mypy`、
`jsonschema`)。執行期相依套件刻意留空(`pyproject.toml` 裡 `dependencies = []`)——
`jsonschema` 只是開發期用的相依套件,唯一用途是讓某一個 golden test 拿它當作獨立的交叉
驗證,去檢查手寫的 schema validator 對不對;它不會在執行期被 import。

## 執行檢查

```bash
uv run pytest -q                        # 完整測試套件
uv run pytest -q -m "not integration"   # 只跑單元測試(見下方「貢獻者環境分級」)
uv run ruff check .
uv run mypy src
```

PR 送審前這四項都要乾淨過關。CI(`.github/workflows/ci.yml`)在 `ubuntu-latest` 跟
`macos-latest` 的矩陣、Python 3.11 到 3.14 上跑一模一樣的指令,而且一律加上
`-m "not integration"`。

## 貢獻者環境分級

不是每個貢獻都需要真的裝一份 Herdr:

- **純單元測試等級的貢獻**(佔絕大多數:bug 修復、補測試、不動到 socket 通訊行為的重構)
  ——**任何作業系統**都能做。`uv sync --dev && uv run pytest -m "not integration"` 完全
  跑在 `tests/fake_server.py` 的 `FakeHerdrServer` 這個 in-process 假伺服器上,不需要也
  不會呼叫真正的 `herdr` binary。
- **整合層級的變更**(任何動到 `client.py` 的 socket 處理、schema 抓取,或是行為依賴真實
  Herdr 語意的部分)——需要**在 macOS 上、本機環境、裝好 `herdr` CLI**。這些測試標記為
  `integration`,CI 會刻意跳過(跟下方「為什麼不提供 devcontainer」背後同一套理由)。如果
  你在改這一層,開 PR 前請自己先跑過整合測試,並在 PR 描述裡註明你跑過了。

### 跨平台注意事項

動 `client.py` 或 `tests/fake_server.py` 之前先讀這段:

- **`AF_UNIX` socket 路徑長度限制**:macOS 的 `sockaddr_un.sun_path` 上限是 **104
  bytes**(Linux 容許的長度比較寬鬆,但不要依賴這個差異)。`pytest` 預設的 `tmp_path`
  跟 macOS 預設的 `tempfile` 路徑,一旦 fixture 巢狀個幾層目錄就常常爆長。測試用的
  socket 路徑一律建在短前綴底下(`/tmp` 加一小段獨特後綴就好),並且用 assertion 明確
  擋住這個前提,不要讓它在 connect 時炸成一個莫名其妙的 `OSError`。
- **`SO_PEERCRED` 只有 Linux 有。** macOS 沒有直接對應的東西可用(`LOCAL_PEERPID` 在
  macOS 上是存在的,但這個程式碼庫沒有接上它);目前在 socket 這一層完全沒有做
  peer-identity 驗證。如果你要加,請明確依平台區分,不要假設它能跨平台通用。
- **`SIGPIPE`**:CPython 在直譯器啟動時就把 `SIGPIPE` 設成 `SIG_IGN`,所以斷線的 pipe
  只會變成一個普通的 `BrokenPipeError`(是 `OSError` 的子類別),不會把行程直接砍掉。
  `client.py` 就是靠這個特性,透過既有的 `except OSError` 路徑處理;不需要另外處理
  `SO_NOSIGPIPE`/`MSG_NOSIGNAL`。

### 為什麼不提供 devcontainer

我們刻意不附 `.devcontainer/`。Herdr 的 socket API 是本機 Unix-domain-socket 這種原生
機制;透過 Docker Desktop for macOS 去測試它,測到的其實是 vpnkit 的 socket 轉發行為,
不是 Herdr 真正的行為——偏偏這正是這個函式庫最需要被正確驗證的部分,容器反而會驗到錯的
東西。再加上這是一個零執行期相依、純 stdlib 的函式庫(環境漂移本來就少,容器要防的東西本來
就不多),這筆帳算下來現階段不划算。如果未來情況變了,
比較可能出現的形式是一個「只涵蓋單元測試」的 devcontainer(並且要明確寫清楚它不涵蓋
`integration` 這個測試等級)。

## TDD 慣例

這個程式碼庫從一開始就是用測試先行的方式建構的。新增行為時請比照辦理:先針對
`FakeHerdrServer` 寫一個會失敗的測試,確認它是照你預期的理由失敗,再動手實作到它通過。
修 bug 時要補一個回歸測試,在修復前會失敗、修復後會通過。

## Commit 訊息

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式(`feat:`、
`fix:`、`docs:`、`test:`、`refactor:`、`chore:` ……)。這樣歷史紀錄好掃視,changelog
也比較好整理。

### 簽署(DCO)

每個 commit 都必須帶 `Signed-off-by` trailer(用 `git commit -s`),證明你遵守
[Developer Certificate of Origin](https://developercertificate.org/)(DCO)。CI 會
強制檢查這件事——[`.github/workflows/dco.yml`](.github/workflows/dco.yml) 這個
workflow 會檢查每個 PR 裡所有 commit,少簽一個就擋下合併。

### 本機 git hooks

clone 完之後跑一次 `bash .githooks/install.sh`,啟用本機的 `commit-msg`(DCO)與
`post-commit`(寫回 RemaGraph 記憶)這兩個 hook——各自在做什麼、怎麼移除,見
[`.githooks/README.md`](.githooks/README.md)。

## 版本策略(semver,pre-1.0)

herdr-bridge 遵循 [Semantic Versioning](https://semver.org/)。版本號停在 `0.x`
期間——這反映的是 Herdr 自己還是 pre-1.0 的狀態,不是這個函式庫介面不穩定——貢獻者實務上
要遵守的規則是:

- **任何動到公開函式簽章的變更**——`list_agents`、`read_agent`、`send_to_agent`、
  `wait_until`、`acquire_control`、`connect`,或是 `AgentInfo` / `AgentOutput` /
  `SendResult` / `WaitResult` 上的任何欄位——**一律視同 major 版本等級的變更**
  (pre-1.0 期間是把第二位數往上跳,例如 `0.1.0` -> `0.2.0`),並且要在 PR 描述裡講清楚
  為什麼要改。這些簽章之所以在 `0.1.0` 就凍結,是刻意讓未來的治理層可以放心依賴它們;
  不要在沒先討論過的情況下新增、移除、調換順序或改型別。
- 純粹增量的功能(新增一個有預設值的可選參數、新增一個函式)算 minor 版本。
- 不動簽章的 bug 修復算 patch 版本。

## 治理層邊界

這個 repo **只做工具層**——`SocketClient` / `SessionCache` / `BridgeActions`。它不會、
也不打算包含規則引擎、排程政策、多租戶存取控制,或任何解讀 `actor_id` / `priority` /
`mode` 語意的邏輯(超出單純記錄的範圍)。那類邏輯屬於一個獨立的治理層專案,透過公開的
`BridgeActions` 介面來使用這個套件——完整介面約定見 [`docs/api.md`](docs/api.md)。

具體來說:**在這個 repo 加治理或政策邏輯的 PR,會被引導到別的地方去,不會被合併。** 如果
你在做的是規則引擎、優先權排程器,或是稽核記錄的消費端,那應該放在這個套件的下游,而不是
這個 repo 裡面。

## 記憶 facade 邊界

只有 `src/herdr_bridge/orchestration/memory.py` 可以直接 `import remagraph`。其他任何需要記憶
功能(recall、儲存、dispatch text 增強)的模組,一律要透過這個模組公開的函式(`store_memory`、
`recall_memories`、`prepare_dispatch_text` 等),不能直接 import RemaGraph。這條規則不限制程式
碼註解或內部識別字(其他地方的 `is_remagraph_enabled`、docstring 等都沒問題)——真正要守的是
**執行期輸出**:在 `-v`/`--verbose` 以外、使用者看得到的路徑上(CLI stdout/stderr、`--help`
文字、送給下游 agent 的 prompt、儲存的記憶記錄的 summary/handoff_note),都不應該印出、記錄或
丟出任何點名「RemaGraph」的訊息——對外公開的功能名稱是「Herdr Bridge Memory」。這是為了讓這層
品牌抽象在未來的開發中維持一致,不只是為了這次改名而已。

## 發布檢查清單

要打 release tag 之前:

1. `uv run pytest -q && uv run ruff check . && uv run mypy src`——全部要乾淨過關。
2. `uv build`——確認 `dist/` 底下同時產出 sdist 跟 wheel。
3. 確認 `py.typed`(PEP 561 標記)有打包進 wheel 裡:
   ```bash
   uv run python -c "import zipfile, glob; print('py.typed' in zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist())"
   ```
4. **全新虛擬環境安裝的煙霧測試**——在一個用完即丟的虛擬環境裡(不是這個 repo 的
   `.venv`),安裝剛發布的那個確切版本,對著一個正在跑的 herdr 執行 README 裡的快速上手
   範例:
   ```bash
   uv venv /tmp/herdr-bridge-smoke && source /tmp/herdr-bridge-smoke/bin/activate
   pip install herdr-bridge==X.Y.Z
   python -c "from herdr_bridge import connect; connect()"
   ```
   這一步抓的是「在開發用的 venv 裡能跑,但 wheel 少打包了某個檔案」這種問題,要在使用者
   遇到之前先攔下來。
5. 更新 `CHANGELOG.md`,把 `TBD` 佔位字串換成實際發布日期,並開一個新的
   `[Unreleased]` 區段給接下來的變更用。

發布本身由 [`.github/workflows/publish.yml`](.github/workflows/publish.yml) 自動化,
由 GitHub Release 觸發,透過 PyPI 的 Trusted Publisher(OIDC)機制,完全不需要 API
token。唯一沒辦法自動化的是一次性的 PyPI 專案設定,把 Trusted Publisher 指向這個
repo;這件事只需要做一次,由維護者本人直接在 pypi.org 上完成。
