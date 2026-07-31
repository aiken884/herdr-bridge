# Governance

herdr-bridge is an open-source project under the Apache-2.0 license. This
document describes how decisions are made. It is deliberately lightweight to
match the project's size, and is expected to grow as the community grows.

## Roles

- **Users** — anyone using herdr-bridge. Feedback via issues and discussions is
  the primary input to the roadmap.
- **Contributors** — anyone who submits a pull request, issue, or review.
- **Maintainers** — listed in [`MAINTAINERS.md`](MAINTAINERS.md). They review and
  merge changes, cut releases, and steward the project's direction.

## Decision Making

- **Lazy consensus.** Most decisions happen in issues and pull requests. A
  change with maintainer approval and no sustained objection is accepted.
- **Non-trivial decisions** (breaking changes, new public surface, dependency
  additions, governance changes) are raised as an issue for discussion before
  implementation, so users can weigh in.
- **The frozen public API** (the five functions, `connect()`, and the documented
  data models) may only change in a backward-incompatible way with a major
  version bump and a clear migration note — see [`BOUNDARIES.md`](BOUNDARIES.md).
- If consensus cannot be reached, the maintainers decide. As the project grows
  to multiple maintainers, contentious decisions are resolved by a simple
  majority of maintainers.

## Becoming a Maintainer

A contributor may be invited to become a maintainer after a sustained track
record of quality contributions and reviews (roughly: several months of
meaningful activity) and agreement of the existing maintainers. New maintainers
are added via a pull request to `MAINTAINERS.md`.

## Vendor Neutrality

herdr-bridge is a thin, policy-neutral client of the Herdr socket API. It does
not privilege any particular AI agent, model vendor, or downstream consumer —
it records `actor_id`/`priority`/`mode` but enforces no policy (see ADR 0001).
Governance changes will preserve this neutrality: the project will not be
steered to lock users into any single vendor or downstream product.

## Changes to Governance

This document is changed by pull request under the same lazy-consensus process,
with explicit maintainer approval.

---

# 專案治理

herdr-bridge 是一個採用 Apache-2.0 授權的開源專案。這份文件說明決策是怎麼做出來的。內容刻意寫得
輕量，符合目前專案的規模，並預期會隨社群成長而擴充。

## 角色

- **使用者**——任何在用 herdr-bridge 的人。透過 issue 與討論回饋的意見，是路線圖最主要的輸入來源。
- **貢獻者**——任何送出 pull request、開 issue 或參與 review 的人。
- **維護者**——名單列在 [`MAINTAINERS.md`](MAINTAINERS.md)。負責審核與合併變更、切版本，並掌舵
  專案的發展方向。

## 決策方式

- **消極共識（lazy consensus）。** 大部分決策都是在 issue 與 pull request 裡發生的：只要有維護者
  核准、且沒有持續性的反對意見，變更就會被接受。
- **非瑣碎的決策**（破壞相容性的變更、新增公開介面、新增依賴套件、治理規則的變更）會先開一個 issue
  拿出來討論，等實作之前讓使用者有機會表達意見。
- **凍結中的公開 API**（五個函式、`connect()`，以及文件記載的資料模型）只能透過 major 版本升級並附上
  明確的遷移說明,才能做不相容的變更——詳見 [`BOUNDARIES.md`](BOUNDARIES.md)。
- 如果無法達成共識，由維護者做最終決定。等專案成長到有多位維護者時，有爭議的決策會以維護者的
  簡單多數決處理。

## 如何成為維護者

一位貢獻者如果長期有品質穩定的貢獻與 review 紀錄（大致上是連續好幾個月的有意義活動），
並取得現有維護者的同意，就可能受邀成為維護者。新維護者的加入是透過對 `MAINTAINERS.md` 送出
pull request 來完成。

## 廠商中立

herdr-bridge 是 Herdr socket API 的一個薄、不涉政策判斷的客戶端。它不會偏袒任何特定的 AI agent、
模型廠商或下游使用者——它只記錄 `actor_id`／`priority`／`mode`，但不強制執行任何政策（詳見
ADR 0001）。未來任何治理規則的調整，都會維持這份中立性：這個專案不會被導向去把使用者綁死在
單一廠商或單一下游產品上。

## 治理文件的變更

這份文件的修改一樣走同一套消極共識流程，透過 pull request 進行，並需要維護者明確核准。
