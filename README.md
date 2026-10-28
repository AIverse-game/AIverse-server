# AIverse 服务端

AIverse 的权威游戏服务端。游戏状态只在这里计算，网页客户端（同级目录 `aiverse-client`）只接收并渲染。设计、规则与协作约定都在客户端仓库的文档中，先读那边的 `AGENTS.md` 与 `docs/architecture.md`。

当前功能：生成一张固定布局的正方形地图（边长 100）和 4 名居民，每人开局 10000 金币，客户端经 WebSocket 读取；通过操作原语改变世界，客户端可用的有“走到坐标”（`move_to`，按固定速度走过去，`walking.py`）、“退出游戏”（`leave_game`）和“采矿”（`mine`）。一局 20 分钟（可配置），到点自动结束并给出按金币的最终排名（`lifecycle.py`）；居民可以中途出局，游戏照常进行。居民可以持有物品，服务端内部有存入、移出、计数、列出和清空的接口（`inventory.py`），尚未开放给客户端或 AI。居民之间需要对方同意的请求（如购买）作为事件排进世界中的队列，由收件人同意或拒绝、发件人撤回（`events.py`）；第一批事件是交易（`trade.py`）：买方或卖方任一方用原语 `OfferToBuy`、`OfferToSell` 发起报价，自己的金币或物品立即预留，对方同意后互换，拒绝或撤回后退回；金币是居民的独立余额。这些原语和答复原语 `RespondToEvent`、`WithdrawEvent` 都还没有进入协议。居民的体力是 token（`tokens.py`）：开局每人 200，是物品种类 `"token"`，可以用上述报价买卖；走路按直线距离消耗，每秒走 2.5 单位，途中不能做别的；token 由正变零的居民出局。开局时随机放置 6 个矿池，整局固定，居民站在矿池范围内可以花 10 秒采 30 token（`mining.py`）。居民有感知与发现（`perception.py`）：每轮工作开始时调用 `begin_turn`，居民注意到周围 20 单位内的矿池、其他居民，以及自上次查看以来别人在附近的出发、到达、开始或完成采矿、出局；见过的矿池记在居民的记忆里，走远后仍记得位置和当时的储量。这些动态来自世界中的活动日志，由原语和对局时钟自动记录。居民还有由 Agent 自己决定的记忆（`memory.py`）：`remember` 写下一条文字笔记并选择长期（整局保留）或短期（120 秒游戏时间后清除），`forget` 忘掉任意一条；每条最多 300 字符，长期最多 30 条、短期最多 20 条，满了要先忘掉一条，每轮感知时一并交回。AI 接入后调用 `Game.begin_turn`，它先推进游戏时间再感知；记笔记用 `Game.remember`、`Game.forget`。感知和记忆都不在协议中。所有连接共享同一个世界，世界变化后推送给每个已握手的连接。

## 坐标系

- 地图边长为 `size` 个地图单位。
- 原点 (0, 0) 在左上角，x 向右增大，y 向下增大，与屏幕方向一致。
- 坐标是闭区间 [0, size] 内的实数，不是网格。构造越界或 id 重复的世界会抛出 `ValueError`。居民和矿池使用同一坐标系。

## 运行

需要 [uv](https://docs.astral.sh/uv/)，它会自动准备 Python 3.12 和依赖。

```sh
uv sync
uv run aiverse-server                 # 监听 127.0.0.1:8000
uv run aiverse-server --port 9000     # 换端口，网页端需同时改代理端口，见下
uv run aiverse-server --duration 120  # 一局的秒数，默认 1200（20 分钟）
uv run aiverse-server --seed 7        # 固定矿池布局；不给则每局随机
make verify                           # ruff、ruff format --check、mypy strict、pytest
```

一局游戏从服务端创建世界（即启动）时开始计时，到点后自动结束：停止所有采矿、撤销所有待处理报价、退回预留，世界冻结。要开新的一局，重启服务端。矿池在创建世界时放好，之后只有储量变化。

网页端开发与预览服务器默认把 `/ws` 代理到 `127.0.0.1:8000`。服务端换端口时，客户端用同一端口启动：`AIVERSE_SERVER_PORT=9000 npm run dev`。

尚无身份认证、Origin 检查和消息大小限制，任何连接都能操作任何居民，不要监听公网地址。每个连接只保留最新一份待发的 `state`，对自己操作的回复最多积压 256 条，超过就以 1008 关闭。

## 协议

WebSocket 地址为 `/ws`，消息是 JSON 文本帧，键名用 camelCase。当前协议版本为 6。

| 方向 | 消息 | 说明 |
| --- | --- | --- |
| 客户端 → 服务端 | `{"type": "hello", "protocolVersion": 6}` | 连接后发送；多余字段忽略 |
| 客户端 → 服务端 | `{"type": "action", "action": {...}}` | 操作原语，`hello` 成功后才能发送，见下 |
| 服务端 → 客户端 | `{"type": "welcome", "protocolVersion": 6}` | 回应 `hello` |
| 服务端 → 客户端 | `{"type": "state", "world": {...}}` | 紧跟 `welcome`；世界变化后、游戏结束时也推送给每个已握手的连接。内容见下 |
| 服务端 → 客户端 | `{"type": "action_result", "ok": true}` | 回应发送者的 `action`。被拒绝时 `ok` 为 `false`，附 `code`（如 `unknown_resident`、`out_of_bounds`、`resident_left`、`game_over`、`not_enough_tokens`、`busy`、`not_at_pool`、`pool_empty`、`unknown_pool`；客户端应接受未知代码）和 `message` |
| 服务端 → 客户端 | `{"type": "error", "code": "...", "message": "..."}` | `bad_message` 或 `protocol_mismatch`；`message` 不超过 200 字，连接保持可用 |

客户端可用的原语：

```json
{ "type": "move_to", "residentId": "r1", "target": { "x": 30.0, "y": 40.5 } }
{ "type": "leave_game", "residentId": "r1" }
{ "type": "mine", "residentId": "r1", "poolId": "p1" }
```

坐标必须是有限的数字（不接受布尔值、字符串、NaN 和无穷），否则回复 `bad_message`。居民不存在或目标在地图外时回复被拒绝的 `action_result`，世界不变。`move_to` 让居民沿直线以每秒 2.5 地图单位走向目标，到达前忙碌，除 `leave_game` 外的原语以 `busy` 拒绝；每回合能走多远尚未设计。出发时按直线距离每单位扣 1 token（向上取整，原地不动免费、不出发），token 不够时以 `not_enough_tokens` 拒绝。到达时推送状态；中途出局或游戏结束时停在当时所在的位置。花掉最后一个 token 的居民按 `leave_game` 出局：卖出的当即出局，走路的到达后出局；出售报价中预留的 token 和进行中的采矿已挖出的 token 都算拥有。

`mine` 要求居民站在矿池半径内（含边缘）。开始时从矿池扣出 30 token（剩余不足时扣剩余量），10 秒（游戏时间）后发放给居民并推送状态；期间居民忙碌，除 `leave_game` 外的原语以 `busy` 拒绝。中途出局时 token 退回矿池，游戏结束时作废。

`leave_game` 让居民出局：与其有关的待处理报价撤销并退回预留，物品清空，金币保留并参与排名；居民仍留在地图上，之后其操作一律以 `resident_left` 拒绝。游戏结束后，所有操作以 `game_over` 拒绝。交易与答复事件的原语还不在协议中。

`world` 的结构：

```json
{
  "map": { "size": 100.0 },
  "pools": [{ "id": "p1", "position": { "x": 35.9, "y": 22.1 }, "radius": 4.0, "reserve": 316 }],
  "residents": [
    {
      "id": "r1", "name": "阿青", "color": "#e4572e", "position": { "x": 20.0, "y": 25.0 },
      "coins": 10000, "left": false, "rank": 1, "tokens": 200,
      "mining": { "poolId": "p1", "amount": 30, "remainingSeconds": 9.5 }
    },
    {
      "id": "r2", "name": "小麦", "color": "#f3a712", "position": { "x": 60.0, "y": 30.0 },
      "coins": 10000, "left": false, "rank": 1, "tokens": 150,
      "walking": { "target": { "x": 40.0, "y": 50.0 }, "remainingSeconds": 11.3 }
    }
  ],
  "game": { "ended": false, "remainingSeconds": 1199.5 }
}
```

- `coins` 是金币总额，含待处理报价中预留的金币，对所有人公开。`rank` 按总额排名，1 为最多，金额相同名次并列（1、1、3）；游戏结束后即为最终排名。
- `position` 是发送时所在的位置，行走中即途中的位置。`walking` 只在行走期间出现：居民从 `position` 沿直线匀速走向 `target`，`remainingSeconds` 后到达，客户端据此本地播放走路动画。
- `tokens` 是拥有的 token：携带的加出售报价中预留的。`mining` 只在采矿期间出现，`remainingSeconds` 是发送时该次采矿的剩余秒数。`pools` 的位置和半径整局不变，`reserve` 是剩余储量。
- `remainingSeconds` 是发送时的剩余秒数，结束后为 0。状态只在变化时推送，客户端应以收到时刻为起点本地倒计时，到点后以服务端的 `ended` 为准。

协议在客户端 `packages/protocol` 中还有一份 TypeScript 定义。改动时两边同时修改并更新测试；不兼容的改动同时提升两边的 `PROTOCOL_VERSION`。

## 代码结构

| 文件 | 用途 |
| --- | --- |
| `src/aiverse_server/world.py` | 坐标、居民（含金币、是否出局、采矿与行走任务、记忆与笔记）、物品栏、事件队列、矿池、活动日志、地图与世界模型（含是否结束、游戏时间），越界、重复 id 与事件、采矿、行走、记忆和日志一致性校验 |
| `src/aiverse_server/layout.py` | 固定的初始地图与居民（含初始金币与 token），按种子随机放置的矿池 |
| `src/aiverse_server/actions.py` | 操作原语：玩家与 AI 改变世界的唯一入口，校验并执行；含答复事件、发起购买与出售、退出游戏、采矿的原语，执行后处理 token 耗尽并记录活动 |
| `src/aiverse_server/tokens.py` | token 体力：物品种类、拥有量、走路消耗 |
| `src/aiverse_server/mining.py` | 矿池采矿：开始、按游戏时间发放、取消、忙碌检查（含行走） |
| `src/aiverse_server/walking.py` | 行走：速度、所需时间、途中位置、出发、到达、中途停下、下一次到达时刻 |
| `src/aiverse_server/clock.py` | 游戏时间到点的事：发放采矿、行走到达（及到达时 token 耗尽出局）并记录活动，下一个到点时刻 |
| `src/aiverse_server/perception.py` | 感知与发现：每轮开始时 `begin_turn` 返回范围内的矿池、居民和新的活动，并更新居民记住的矿池；`record_activity` 比较前后世界记录活动 |
| `src/aiverse_server/memory.py` | 居民自选的记忆：写入长期或短期笔记、忘掉笔记、清除过期的短期笔记 |
| `src/aiverse_server/inventory.py` | 居民物品的存入、移出、计数、列出与清空，金币的收付；服务端内部接口，不是原语 |
| `src/aiverse_server/events.py` | 事件队列：可扩展的事件种类（`EventPayload` 及其发送、同意、拒绝钩子），发送、答复、撤回与收件箱查询 |
| `src/aiverse_server/trade.py` | 交易事件：购买报价 `PurchaseOffer` 与出售报价 `SaleOffer`，条款校验、预留与互换 |
| `src/aiverse_server/lifecycle.py` | 对局生命周期：能否行动的检查、出局（含 token 耗尽，行走者到达后出局；出局和结束时停下行走）、结束、按金币总额排名，默认时长 |
| `src/aiverse_server/protocol.py` | 消息模型、客户端消息解析、消息到原语与世界到视图的转换 |
| `src/aiverse_server/app.py` | FastAPI 应用与 WebSocket `/ws`：共享世界、对局计时（到点的事见 `clock.py`）、每轮开始的 `begin_turn`、写入与忘掉笔记、握手、执行原语、广播状态 |
| `src/aiverse_server/__main__.py` | 启动入口 |
| `tests/` | 世界模型、操作原语、物品栏与金币、事件、交易、对局生命周期、token、采矿、行走、感知、记忆与 WebSocket 接口测试 |
