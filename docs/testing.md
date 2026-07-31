# FakeHerdrServer — a test double for contract tests

`herdr_bridge.testing` provides `FakeHerdrServer`, so downstream consumers can run contract tests against herdr-bridge and verify their integration behavior without needing a real Herdr server around.

## Quick Start

```python
from herdr_bridge.testing import FakeHerdrServer
from herdr_bridge.client import SocketClient

with FakeHerdrServer() as srv:
    # Register a custom handler to simulate a specific socket method
    srv.set_handler("session.snapshot", lambda _params: {
        "panes": [{"pane_id": "w1:p1", "terminal_id": "t1", "agent": "claude"}],
    })

    client = SocketClient(srv.socket_path)
    result = client.request("session.snapshot", {})
    assert result["panes"][0]["pane_id"] == "w1:p1"
```

## Public API

### `FakeHerdrServer(handlers: dict[str, Handler] | None = None)`

Creates a local AF_UNIX socket server with a `ping` handler registered by default.

| Attribute/Method | Description |
|---|---|
| `socket_path: str` | The path the server listens on |
| `requests: list[dict]` | A log of every request received so far |
| `set_handler(method, handler)` | Register or override the handler for a given method |
| `push_event(event, data)` | Push an event out to every subscribed connection |
| `push_raw(line)` | Push an arbitrary raw line (including malformed JSON), for protocol-resilience testing |
| `drop_subscribers()` | Simulate a server-side disconnect — immediately drops every subscribed connection |

Supports the context-manager protocol (`with FakeHerdrServer() as srv:`); on exit it cleans up its socket and temp directory automatically.

### `FakeApiError(code: str, message: str)`

Handlers raise `FakeApiError(...)` to express an error envelope, which is how you test a client's error-handling path:

```python
from herdr_bridge.testing import FakeApiError

def nope(_params):
    raise FakeApiError("pane_not_found", "no such pane")

srv.set_handler("pane.read", nope)
# client.request("pane.read", {}) → HerdrApiError
```

### `Handler = Callable[[dict[str, Any]], dict[str, Any]]`

A type alias for handlers, for use in type annotations.

## Contract Testing Pattern

Downstream consumers can swap `FakeHerdrServer` in for a real Herdr server in CI, to verify:

1. **Connection management** — how `SocketClient` behaves when the server restarts or disconnects
2. **Error handling** — the `FakeApiError` → `HerdrApiError` mapping
3. **Caching behavior** — `SessionCache`'s event-driven updates
4. **Bridge Actions** — the five-method coordination semantics (send → read → wait → status → control)
5. **Audit logging** — correctness of the audit log's fields

## Notes

- macOS caps `sun_path` at 104 bytes, so `FakeHerdrServer` always builds its socket path with the short `/tmp/fh-*` prefix
- `push_raw()` doesn't append a trailing newline automatically — you need to include `"\n"` yourself (unlike `push_event()`)
- This module has no dependency on any real herdr binary — it's a pure-Python AF_UNIX socket implementation

---

# FakeHerdrServer — 契約測試替身

`herdr_bridge.testing` 提供 `FakeHerdrServer`，讓下游 consumer 在無真實 herdr server 環境下執行契約測試，驗證與 herdr-bridge 的整合行為。

## 快速開始

```python
from herdr_bridge.testing import FakeHerdrServer
from herdr_bridge.client import SocketClient

with FakeHerdrServer() as srv:
    # 自訂 handler 模擬特定 socket 方法
    srv.set_handler("session.snapshot", lambda _params: {
        "panes": [{"pane_id": "w1:p1", "terminal_id": "t1", "agent": "claude"}],
    })

    client = SocketClient(srv.socket_path)
    result = client.request("session.snapshot", {})
    assert result["panes"][0]["pane_id"] == "w1:p1"
```

## 公開 API

### `FakeHerdrServer(handlers: dict[str, Handler] | None = None)`

建立一個本機 AF_UNIX socket server，預設已註冊 `ping` handler。

| 屬性／方法 | 說明 |
|---|---|
| `socket_path: str` | server 監聽的 socket 路徑 |
| `requests: list[dict]` | 所有收到的 request 記錄 |
| `set_handler(method, handler)` | 註冊／覆蓋指定 method 的 handler |
| `push_event(event, data)` | 向所有已訂閱連線推送事件 |
| `push_raw(line)` | 推送任意原始行（含畸形 JSON），供協定韌性測試 |
| `drop_subscribers()` | 模擬 server 端斷線（立即中斷所有訂閱連線） |

支援 context manager（`with FakeHerdrServer() as srv:`），離開時自動清理 socket 與暫存目錄。

### `FakeApiError(code: str, message: str)`

Handler 以 `raise FakeApiError(...)` 表達 error envelope，用於測試 client 的錯誤處理路徑：

```python
from herdr_bridge.testing import FakeApiError

def nope(_params):
    raise FakeApiError("pane_not_found", "no such pane")

srv.set_handler("pane.read", nope)
# client.request("pane.read", {}) → HerdrApiError
```

### `Handler = Callable[[dict[str, Any]], dict[str, Any]]`

Handler 型別別名，供型別標註使用。

## 契約測試模式

下游 consumer 可在 CI 中以 `FakeHerdrServer` 取代真實 herdr server，驗證：

1. **連線管理**：`SocketClient` 對 server 重啟／斷線的反應
2. **錯誤處理**：`FakeApiError` → `HerdrApiError` 的映射
3. **快取行為**：`SessionCache` 的事件驅動更新
4. **Bridge Actions**：五函式協調語意（send → read → wait → status → control）
5. **稽核記錄**：audit log 欄位正確性

## 注意事項

- macOS `sun_path` 上限 104 bytes，`FakeHerdrServer` 固定使用 `/tmp/fh-*` 短前綴建立 socket
- `push_raw()` 不自動附加換行；需自行包含 `"\n"`（與 `push_event()` 不同）
- 本模組不依賴任何真實 herdr 二進位檔——純 Python AF_UNIX socket 實作
