# ADR 0001 — 為什麼中立的薄工具層裡有一個「有狀態」的控制權 lease

狀態：Accepted（2026-07-19）
脈絡來源：CodeGraph 結構稽核 + 外部架構審核指出——`acquire_control` / `_ControlRegistry` / `ControlHandle` 是 herdr-bridge 這個「無狀態 pass-through」層裡**唯一有狀態、且會 allow/deny 的元件**，與「thin、agent-agnostic、policy-neutral」的設計語意存在張力。本 ADR 記錄這是**刻意選擇**而非漂移。

## 決策

保留 `acquire_control` 的 in-process 互斥 lease，維持其有狀態、會拒絕重複取得的行為；但明確界定其語意邊界，並以此 ADR 消除「中立層裡的例外」的誤讀。

## 理由

1. **它管的是「本進程內的協調」，不是「策略」。** lease 回答的是「這個 pane 現在有沒有被本進程內的某個呼叫者宣告獨佔」——這是**同一進程多呼叫者**之間的協調原語（類似 `threading.Lock` 的語意），任何多呼叫者的消費者都需要它。它**不**做任何跨進程、跨使用者、基於身份或優先權的政策裁決。

2. **有狀態是這個原語的本質。** 互斥必然要記住「誰持有」。把它做成無狀態就不再是互斥。它記的是最小必要狀態（pane_id → handle），不是業務狀態。

3. **它仍然 policy-neutral。** lease **不**根據 `actor_id`／`priority`／`mode` 做任何 allow/deny——這些欄位一律只記進 audit、不影響裁決（見 README「Frozen for a future governance layer」）。lease 的拒絕條件只有一個且與身份無關：「此 pane 已被本進程內另一個未釋放的 handle 持有」。被拒絕的搶奪嘗試本身會記 audit（`acquire_control_denied`），把「要不要搶、誰有權搶」的**策略**留給上層。

4. **它是 herdr server 層鎖的替代品的最小形。** herdr 的 socket 協定不提供 pane 級的獨佔鎖；消費者若要「我正在操作這個 pane、別人先別插手」的保證，工具層是唯一能提供 in-process 協調點的地方。不提供它，等於把這個必然需求推給每個消費者各自重造。

## 邊界（明確界定，供開源消費者理解）

- lease 是 **in-process、advisory**：它不阻止另一個**進程**或直接的 socket 呼叫去動同一個 pane；它只在本進程的 `BridgeActions` 呼叫者之間協調。
- lease **不持久化**、不跨重啟、不跨機器。
- lease **不**是安全邊界，也不是權限系統。身份/優先權/政策一律屬於上層。

## 後果

- 正面：消費者拿到一個開箱即用的 pane 獨佔協調原語，語意明確且中立。
- 代價：本層有一處合理的有狀態例外——本 ADR 即為其正當性存證，未來審查見此不需再列為「架構漂移」。
- 若日後需要跨進程/持久化的獨佔，屬**上層或 herdr server**的責任，不應把政策塞進本 lease。
