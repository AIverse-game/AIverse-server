# AIverse 服务端

AIverse 的权威游戏服务端。游戏状态只在这里计算，网页客户端（同级目录 `aiverse-client`）只接收并渲染。设计、规则与协作约定都在客户端仓库的文档中，先读那边的 `AGENTS.md` 与 `docs/architecture.md`。

当前功能：生成一张固定布局的正方形地图（边长 100）和 4 名居民，每人开局 10000 金币，客户端经 WebSocket 读取；通过操作原语改变世界，客户端可用的有“移动到坐标”（`move_to`）和“退出游戏”（`leave_game`）。一局 20 分钟（可配置），到点自动结束并给出按金币的最终排名（`lifecycle.py`）；居民可以中途出局，游戏照常进行。居民可以持有物品，服务端内部有存入、移出、计数、列出和清空的接口（`inventory.py`），尚未开放给客户端或 AI。居民之间需要对方同意的请求（如购买）作为事件排进世界中的队列，由收件人同意或拒绝、发件人撤回（`events.py`）；第一批事件是交易（`trade.py`）：买方或卖方任一方用原语 `OfferToBuy`、`OfferToSell` 发起报价，自己的金币或物品立即预留，对方同意后互换，拒绝或撤回后退回；金币是居民的独立余额。这些原语和答复原语 `RespondToEvent`、`WithdrawEvent` 都还没有进入协议。所有连接共享同一个世界，世界变化后推送给每个已握手的连接。

## 坐标系

- 地图边长为 `size` 个地图单位。
- 原点 (0, 0) 在左上角，x 向右增大，y 向下增大，与屏幕方向一致。
- 坐标是闭区间 [0, size] 内的实数，不是网格。构造越界或 id 重复的世界会抛出 `ValueError`。

## 运行

需要 [uv](https://docs.astral.sh/uv/)，它会自动准备 Python 3.12 和依赖。

```sh
uv sync
uv run aiverse-server                 # 监听 127.0.0.1:8000
uv run aiverse-server --port 9000     # 换端口，网页端需同时改代理端口，见下
uv run aiverse-server --duration 120  # 一局的秒数，默认 1200（20 分钟）
make verify                           # ruff、ruff format --check、mypy strict、pytest
```

一局游戏从服务端创建世界（即启动）时开始计时，到点后自动结束：撤销所有待处理报价、退回预留，世界冻结。要开新的一局，重启服务端。

网页端开发与预览服务器默认把 `/ws` 代理到 `127.0.0.1:8000`。服务端换端口时，客户端用同一端口启动：`AIVERSE_SERVER_PORT=9000 npm run dev`。

尚无身份认证、Origin 检查和消息大小限制，任何连接都能操作任何居民，不要监听公网地址。每个连接只保留最新一份待发的 `state`，对自己操作的回复最多积压 256 条，超过就以 1008 关闭。

## 协议

WebSocket 地址为 `/ws`，消息是 JSON 文本帧，键名用 camelCase。当前协议版本为 4。

| 方向 | 消息 | 说明 |
| --- | --- | --- |
| 客户端 → 服务端 | `{"type": "hello", "protocolVersion": 4}` | 连接后发送；多余字段忽略 |
| 客户端 → 服务端 | `{"type": "action", "action": {...}}` | 操作原语，`hello` 成功后才能发送，见下 |
| 服务端 → 客户端 | `{"type": "welcome", "protocolVersion": 4}` | 回应 `hello` |
| 服务端 → 客户端 | `{"type": "state", "world": {...}}` | 紧跟 `welcome`；世界变化后、游戏结束时也推送给每个已握手的连接。内容见下 |
| 服务端 → 客户端 | `{"type": "action_result", "ok": true}` | 回应发送者的 `action`。被拒绝时 `ok` 为 `false`，附 `code`（如 `unknown_resident`、`out_of_bounds`、`resident_left`、`game_over`；客户端应接受未知代码）和 `message` |
| 服务端 → 客户端 | `{"type": "error", "code": "...", "message": "..."}` | `bad_message` 或 `protocol_mismatch`；`message` 不超过 200 字，连接保持可用 |

客户端可用的原语：

```json
{ "type": "move_to", "residentId": "r1", "target": { "x": 30.0, "y": 40.5 } }
{ "type": "leave_game", "residentId": "r1" }
```

坐标必须是有限的数字（不接受布尔值、字符串、NaN 和无穷），否则回复 `bad_message`。居民不存在或目标在地图外时回复被拒绝的 `action_result`，世界不变。移动即时到达，速度与回合限制尚未设计。

`leave_game` 让居民出局：与其有关的待处理报价撤销并退回预留，物品清空，金币保留并参与排名；居民仍留在地图上，之后其操作一律以 `resident_left` 拒绝。游戏结束后，所有操作以 `game_over` 拒绝。交易与答复事件的原语还不在协议中。

`world` 的结构：

```json
{
  "map": { "size": 100.0 },
  "residents": [
    {
      "id": "r1", "name": "阿青", "color": "#e4572e", "position": { "x": 20.0, "y": 25.0 },
      "coins": 10000, "left": false, "rank": 1
    }
  ],
  "game": { "ended": false, "remainingSeconds": 1199.5 }
}
```

- `coins` 是金币总额，含待处理报价中预留的金币，对所有人公开。`rank` 按总额排名，1 为最多，金额相同名次并列（1、1、3）；游戏结束后即为最终排名。
- `remainingSeconds` 是发送时的剩余秒数，结束后为 0。状态只在变化时推送，客户端应以收到时刻为起点本地倒计时，到点后以服务端的 `ended` 为准。

协议在客户端 `packages/protocol` 中还有一份 TypeScript 定义。改动时两边同时修改并更新测试；不兼容的改动同时提升两边的 `PROTOCOL_VERSION`。

## 代码结构

| 文件 | 用途 |
| --- | --- |
| `src/aiverse_server/world.py` | 坐标、居民（含金币、是否出局）、物品栏、事件队列、地图与世界模型（含是否结束），越界、重复 id 与事件一致性校验 |
| `src/aiverse_server/layout.py` | 固定的初始地图与居民 |
| `src/aiverse_server/actions.py` | 操作原语：玩家与 AI 改变世界的唯一入口，校验并执行；含答复事件、发起购买与出售、退出游戏的原语 |
| `src/aiverse_server/inventory.py` | 居民物品的存入、移出、计数、列出与清空，金币的收付；服务端内部接口，不是原语 |
| `src/aiverse_server/events.py` | 事件队列：可扩展的事件种类（`EventPayload` 及其发送、同意、拒绝钩子），发送、答复、撤回与收件箱查询 |
| `src/aiverse_server/trade.py` | 交易事件：购买报价 `PurchaseOffer` 与出售报价 `SaleOffer`，条款校验、预留与互换 |
| `src/aiverse_server/lifecycle.py` | 对局生命周期：能否行动的检查、出局、结束、按金币总额排名，默认时长 |
| `src/aiverse_server/protocol.py` | 消息模型、客户端消息解析、消息到原语与世界到视图的转换 |
| `src/aiverse_server/app.py` | FastAPI 应用与 WebSocket `/ws`：共享世界、对局计时、握手、执行原语、广播状态 |
| `src/aiverse_server/__main__.py` | 启动入口 |
| `tests/` | 世界模型、操作原语、物品栏与金币、事件、交易、对局生命周期与 WebSocket 接口测试 |
