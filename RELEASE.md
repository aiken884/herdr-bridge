# Release Process

herdr-bridge follows [Semantic Versioning](https://semver.org/). Because the
five public function signatures are frozen as of `0.1.0`, the version number
communicates a specific contract — see [`BOUNDARIES.md`](BOUNDARIES.md).

## Versioning rules

- **MAJOR** — a backward-incompatible change to the frozen public surface. These
  are avoided; when unavoidable, they ship with a migration guide.
- **MINOR** — additive, backward-compatible: new symbols, new optional
  parameters with defaults, new behavior behind opt-in. (e.g. `0.1.1` added
  `normalized_text`, `get_audit_log_path()`, socket provenance, the `degraded`
  state — all additive.)
- **PATCH** — bug/security fixes and internal changes with no surface change.

The `0.x` prefix reflects Herdr's own pre-1.0 maturity, not instability of this
library's interface.

## Cutting a release

1. Ensure `main` is green: CI (lint, types, tests, package-hygiene) passes.
2. Update `CHANGELOG.md` — move the unreleased entries under a new
   `## [X.Y.Z] - DATE` heading (Keep a Changelog format).
3. Bump the version in `pyproject.toml` and `src/herdr_bridge/__init__.py`.
4. Commit, then tag: `git tag vX.Y.Z && git push --tags`.
5. Create a GitHub Release for the tag with notes from the CHANGELOG. This
   triggers `publish.yml`, which builds and publishes to PyPI via OIDC Trusted
   Publishing (no long-lived token) with provenance attestations.
6. Verify: `pip index versions herdr-bridge`; smoke-test a clean install.

## Security releases

Security fixes are prioritized and released as a PATCH as soon as a fix is
verified. See [`SECURITY.md`](SECURITY.md) for the private reporting process and
the coordinated-disclosure timeline.

## Cadence

There is no fixed calendar cadence for a project this size; releases are cut
when there is meaningful, verified change. A rough intent is at least a release
or an explicit roadmap update each quarter so users can see the project is
maintained.

---

# 發版流程

herdr-bridge 遵循 [Semantic Versioning](https://semver.org/)（語意化版本）。由於五個公開函式的簽章從
`0.1.0` 起就已凍結，版本號代表的是一份具體的約定——詳見 [`BOUNDARIES.md`](BOUNDARIES.md)。

## 版號規則

- **MAJOR（主版號）**——對凍結中的公開介面做出不相容變更。這種變更能避免就避免；真的無法避免時，
  一定會附上遷移指南。
- **MINOR（次版號）**——新增且向下相容：新增符號、新增有預設值的選用參數、以 opt-in 方式加入新行為。
  （例如 `0.1.1` 新增的 `normalized_text`、`get_audit_log_path()`、socket 來源追蹤、`degraded` 狀態，
  全部都是純新增。）
- **PATCH（修訂號）**——臭蟲/安全性修復，以及不影響對外介面的內部調整。

`0.x` 這個前綴反映的是 Herdr 本身還在 1.0 之前的成熟度，不代表這個函式庫的介面不穩定。

## 怎麼切一個版本

1. 確認 `main` 是綠燈：CI（lint、型別檢查、測試、套件衛生檢查）全部通過。
2. 更新 `CHANGELOG.md`——把尚未發布的項目搬到新的 `## [X.Y.Z] - DATE` 標題底下（依 Keep a Changelog
   格式）。
3. 把版本號同步改到 `pyproject.toml` 與 `src/herdr_bridge/__init__.py`。
4. 進行 commit，再打 tag：`git tag vX.Y.Z && git push --tags`。
5. 針對這個 tag 建立一個 GitHub Release，內容引用 CHANGELOG 的說明。這會觸發 `publish.yml`，
   透過 OIDC Trusted Publishing（不需要長效 token）建置並發佈到 PyPI，同時附上 provenance
   attestation（來源證明）。
6. 驗證：跑 `pip index versions herdr-bridge` 確認版本已上架，並做一次乾淨安裝的煙霧測試。

## 安全性修復的發版

安全性問題一律優先處理，修復確認無誤後立刻以 PATCH 版本發布。私下回報流程與協調揭露的時程，
詳見 [`SECURITY.md`](SECURITY.md)。

## 發版節奏

以這個專案的規模來說，沒有固定的日曆式發版節奏；只要有實質且經過驗證的變更，就會切一個版本。
大致的目標是每季至少有一次發版，或至少更新一次明確的路線圖，讓使用者看得出這個專案還在被維護。
