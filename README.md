# AIverse 服务端

AIverse 的权威游戏服务端。游戏状态只在这里计算，网页客户端（同级目录 `aiverse-client`）只接收并渲染。设计、规则与协作约定都在客户端仓库的文档中，先读那边的 `AGENTS.md` 与 `docs/architecture.md`。

当前功能：生成一张固定布局的正方形地图（边长 100）和 4 名居民，每人开局 10000 金币，客户端经 WebSocket 读取；通过操作原语改变世界，客户端可用的有“走到坐标”（`move_to`，按固定速度走过去，`walking.py`）、“退出游戏”（`leave_game`）、“采矿”（`mine`）、“卖 token”（`sell_tokens`）、“买芯片”（`buy_chip`）和“说话”（`say`）。一局 20 分钟（可配置），到点自动结束并给出按金币的最终排名（`lifecycle.py`）；居民可以中途出局，游戏照常进行。居民可以持有物品，服务端内部有存入、移出、计数、列出和清空的接口（`inventory.py`），尚未开放给客户端或 AI。居民之间需要对方同意的请求（如购买）作为事件排进世界中的队列，由收件人同意或拒绝、发件人撤回（`events.py`）；第一批事件是交易（`trade.py`）：买方或卖方任一方用原语 `OfferToBuy`、`OfferToSell` 发起报价，自己的金币或物品立即预留，对方同意后互换，拒绝或撤回后退回；金币是居民的独立余额。这些原语和答复原语 `RespondToEvent`、`WithdrawEvent` 都还没有进入协议。居民的体力是 token（`tokens.py`）：开局每人 200，是物品种类 `"token"`，可以用上述报价买卖；走路按直线距离消耗，每秒走 2.5 单位，途中不能做别的；token 由正变零的居民出局。开局时随机放置 6 个矿池，整局固定，居民站在矿池范围内、手上有芯片时可以花 10 秒采一次 token（`mining.py`）。芯片（`chips.py`）开局谁都没有，要在收购点用金币按定价买：MK1 300 金币、每次采 30 token，MK2 1200 金币、60 token，MK3 3000 金币、100 token；有几块时用最好的一块，采矿期间守着芯片，不能走开或卖掉。芯片是以型号命名的物品，可以用报价转手，出局时清空。地图中心有一个收购点（`market.py`），代表镇外的买家，是除开局资金外金币唯一的新来源：居民站在收购点范围内可以卖出携带的 token 换金币、买芯片，token 单价随近期卖出量下跌、随时间回升，所有人共用一个价格；矿池离收购点至少 20 单位。居民有感知与发现（`perception.py`）：每轮工作开始时调用 `begin_turn`，居民注意到周围 20 单位内的矿池、其他居民，无论站在哪里都能看到收购点及其现价和芯片价目，以及自上次查看以来别人在附近的出发、到达、开始或完成采矿、出局；见过的矿池记在居民的记忆里，走远后仍记得位置和当时的储量。这些动态来自世界中的活动日志，由原语和对局时钟自动记录。居民还有由 Agent 自己决定的记忆（`memory.py`）：`remember` 写下一条文字笔记并选择长期（整局保留）或短期（120 秒游戏时间后清除），`forget` 忘掉任意一条；每条最多 300 字符，长期最多 30 条、短期最多 20 条，满了要先忘掉一条，每轮感知时一并交回。居民可以对话（`speech.py`）：当众说的话由说话那一刻 20 单位内的在局居民听到，私聊不论远近只有对方听到，每句最多 100 字符；当众说每句 2 token、私聊 1 token，行走、采矿中也能说，每轮感知时交回自上次查看以来自己说的和听到的话。页面上显示所有当众说的话，私聊只发给选中了说话人或对象的页面（`view_as`）。居民可以由服务端里的 Agent 驱动（`agents/`，见下文“AI 居民”）：每轮调用 `Game.begin_turn`，它先推进游戏时间再感知；原语经 `Game.act` 执行，与网页的操作走同一条路径并广播；记笔记用 `Game.remember`、`Game.forget`。感知和记忆都不在协议中。所有连接共享同一个世界，世界变化后推送给每个已握手的连接。

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
uv run aiverse-server --seed 7        # 固定矿池布局；不给则每局随机（收购点总在地图中心）
uv run aiverse-server --agent r1=scripted --agent r2=openrouter   # r1、r2 由 Agent 驱动，见下
make verify                           # ruff、ruff format --check、mypy strict、pytest
```

一局游戏从服务端创建世界（即启动）时开始计时，到点后自动结束：停止所有采矿、撤销所有待处理报价、退回预留，世界冻结。要开新的一局，重启服务端。矿池在创建世界时放好，之后只有储量变化。

网页端开发与预览服务器默认把 `/ws` 代理到 `127.0.0.1:8000`。服务端换端口时，客户端用同一端口启动：`AIVERSE_SERVER_PORT=9000 npm run dev`。

尚无身份认证、Origin 检查和消息大小限制，任何连接都能操作任何居民，不要监听公网地址。每个连接只保留最新一份待发的 `state`，对自己操作的回复最多积压 256 条，超过就以 1008 关闭。

## AI 居民

启动时用 `--agent 居民=大脑` 指定由 Agent 驱动的居民，可重复；未指定的居民仍可在页面上手动操作。调度与选型见客户端 `docs/decisions/0003-event-driven-agents-and-openrouter.md`。

```sh
uv run aiverse-server --agent r1=scripted                    # 固定策略的脚本机器人，不花钱
export OPENROUTER_API_KEY=...                                # 只从环境变量读取，不要写进仓库
uv run aiverse-server --agent r2=openrouter                  # 默认模型 nvidia/nemotron-3.5-lightning
uv run aiverse-server --agent r2=openrouter:nvidia/nemotron-3-super-120b-a12b
uv run aiverse-server --agent r1=scripted --agent-log game.jsonl   # 每轮决策追加一行 JSON
```

- 每名居民一个异步循环（`agents/runner.py`）：感知 → 大脑决策一轮并调用工具 → 等待唤醒。唤醒条件：行走或采矿结束、空闲时收到报价、听到别人说话（忙碌时也唤醒）、空闲 20 秒无事。两轮开始至少相隔 3 秒，大脑出错后 10 秒。居民出局或游戏结束时停止。
- 工具（`agents/tools.py`）绑定到一名居民，参数里没有“谁来做”：`move_to`、`mine`、`sell_tokens`、`buy_chip`、`say`、`offer_to_buy`、`offer_to_sell`、`respond_to_offer`、`withdraw_offer`、`remember`、`forget`。不提供退出游戏。参数错误、未知工具、原语被拒绝都作为失败结果交回，世界不变。
- 观察（`agents/observation.py`）：感知结果加上自己的金币、token、芯片和忙碌状态，所有居民的名字、金币和名次，发给自己的和自己发出的报价，到收购点和已知矿池的距离与走路花费。
- 脚本机器人（`agents/scripted.py`）：买得起的最好芯片 → 最近的有储量矿池（找不到就到固定点探索）→ 采到 260 个 token 回收购点卖到剩 60 → 快结束时卖到剩 1。
- OpenRouter 大脑（`agents/openrouter.py`）：系统提示是规则说明（`agents/rules.py`，数值取自代码常量），用户消息是观察和上一轮的行动结果；模型每轮最多请求 8 次，每局最多 400 次，每次最多写 2048 token。请求失败时由循环记录，10 秒后再问。
- 控制台每轮打印一行摘要；`--agent-log` 另外把完整记录（观察、工具调用与结果、模型的话、错误）写成 JSONL。

## 协议

WebSocket 地址为 `/ws`，消息是 JSON 文本帧，键名用 camelCase。当前协议版本为 10。

| 方向 | 消息 | 说明 |
| --- | --- | --- |
| 客户端 → 服务端 | `{"type": "hello", "protocolVersion": 10}` | 连接后发送；多余字段忽略 |
| 客户端 → 服务端 | `{"type": "view_as", "residentId": "r1"}` | `hello` 成功后才能发送：选择这个连接以哪名居民的身份看私聊，`null` 为不以任何人身份。回复一份新的 `state`；居民不存在时回复 `bad_message`，原选择不变。尚无归属，可选任何居民 |
| 客户端 → 服务端 | `{"type": "action", "action": {...}}` | 操作原语，`hello` 成功后才能发送，见下 |
| 服务端 → 客户端 | `{"type": "welcome", "protocolVersion": 10}` | 回应 `hello` |
| 服务端 → 客户端 | `{"type": "state", "world": {...}}` | 紧跟 `welcome`；世界变化后、游戏结束时也推送给每个已握手的连接。内容见下 |
| 服务端 → 客户端 | `{"type": "action_result", "ok": true}` | 回应发送者的 `action`。被拒绝时 `ok` 为 `false`，附 `code`（如 `unknown_resident`、`out_of_bounds`、`resident_left`、`game_over`、`not_enough_tokens`、`busy`、`not_at_pool`、`pool_empty`、`unknown_pool`、`not_at_market`、`invalid_quantity`、`price_too_low`、`no_chip`、`unknown_chip`、`not_enough_coins`、`empty_speech`、`speech_too_long`、`invalid_speech`、`unknown_listener`；客户端应接受未知代码）和 `message` |
| 服务端 → 客户端 | `{"type": "error", "code": "...", "message": "..."}` | `bad_message` 或 `protocol_mismatch`；`message` 不超过 200 字，连接保持可用 |

客户端可用的原语：

```json
{ "type": "move_to", "residentId": "r1", "target": { "x": 30.0, "y": 40.5 } }
{ "type": "leave_game", "residentId": "r1" }
{ "type": "mine", "residentId": "r1", "poolId": "p1" }
{ "type": "sell_tokens", "residentId": "r1", "quantity": 30 }
{ "type": "buy_chip", "residentId": "r1", "model": "MK1" }
{ "type": "say", "residentId": "r1", "text": "有人卖 token 吗？" }
{ "type": "say", "residentId": "r1", "text": "我出高价", "to": "r4" }
```

坐标必须是有限的数字（不接受布尔值、字符串、NaN 和无穷），否则回复 `bad_message`。居民不存在或目标在地图外时回复被拒绝的 `action_result`，世界不变。`move_to` 让居民沿直线以每秒 2.5 地图单位走向目标，到达前忙碌，除 `leave_game` 和 `say` 外的原语以 `busy` 拒绝；每回合能走多远尚未设计。出发时按直线距离每单位扣 1 token（向上取整，原地不动免费、不出发），token 不够时以 `not_enough_tokens` 拒绝。到达时推送状态；中途出局或游戏结束时停在当时所在的位置。花掉最后一个 token 的居民按 `leave_game` 出局：卖出的当即出局，走路的到达后出局；出售报价中预留的 token 和进行中的采矿已挖出的 token 都算拥有。

`mine` 要求居民站在矿池半径内（含边缘），并携带至少一块芯片（出售报价中预留的不算），否则以 `no_chip` 拒绝。携带几种芯片时用产量最高的一块：开始时从矿池扣出该型号每次的产量（剩余不足时扣剩余量），10 秒（游戏时间）后发放给居民并推送状态；期间居民忙碌，除 `leave_game` 和 `say` 外的原语以 `busy` 拒绝。中途出局时 token 退回矿池，游戏结束时作废。采矿期间芯片留在居民手上，忙碌规则也让它不能被报价卖出。

`buy_chip` 要求居民站在收购点半径内（含边缘），按 `market.chips` 中的定价用携带的金币（报价中预留的不算）买一块该型号的芯片。型号区分大小写，不在出售列表中以 `unknown_chip` 拒绝，金币不够以 `not_enough_coins` 拒绝，不在范围内或没有收购点以 `not_at_market` 拒绝。

`sell_tokens` 要求居民站在收购点半径内（含边缘），卖出携带的 token（出售报价中预留的不算），`quantity` 必须是整数（否则 `bad_message`），不是正数时以 `invalid_quantity` 拒绝，超过携带量以 `not_enough_tokens` 拒绝，不在范围内以 `not_at_market` 拒绝。收入按市场价计算：近期卖出压力 p 下，再卖一个 token 值 `basePrice × depth / (depth + p)` 金币，一次卖 q 个得 `⌊basePrice × depth × ln((depth + p + q) / (depth + p))⌋` 金币，不足 1 金币时以 `price_too_low` 拒绝；之后 p 增加 q，并每 `halfLifeSeconds` 秒减半。卖光拥有的 token 按 token 耗尽出局，金币保留。

`say` 让居民说一句话，`text` 去掉首尾空白后为 1～100 字符，否则以 `empty_speech` 或 `speech_too_long` 拒绝；含孤立代理字符（JSON 中 `\ud800` 一类转义）、不能编码为 UTF-8 的文字以 `invalid_speech` 拒绝。省略 `to` 时当众说，说话那一刻 20 单位内（含边缘，行走者按途中位置）仍在局中的其他居民听到；给出 `to` 时只有那名居民听到，不论远近，对象不存在、已出局或是自己时以 `unknown_listener` 拒绝。谁听到在说话时定下，之后走近的人听不到。每句从携带的 token 中扣除：当众说 2 个、私聊 1 个（出售报价中预留的不算），不够时以 `not_enough_tokens` 拒绝；花掉最后一个 token 按 `leave_game` 出局（行走中的到达后出局）。行走、采矿中也能说。

`leave_game` 让居民出局：与其有关的待处理报价撤销并退回预留，物品清空，金币保留并参与排名；居民仍留在地图上，之后其操作一律以 `resident_left` 拒绝。游戏结束后，所有操作以 `game_over` 拒绝。交易与答复事件的原语还不在协议中。

`world` 的结构：

```json
{
  "map": { "size": 100.0 },
  "pools": [{ "id": "p1", "position": { "x": 35.9, "y": 22.1 }, "radius": 4.0, "reserve": 316 }],
  "residents": [
    {
      "id": "r1", "name": "阿青", "color": "#e4572e", "position": { "x": 20.0, "y": 25.0 },
      "coins": 9700, "left": false, "rank": 2, "tokens": 200, "chips": { "MK1": 1 },
      "mining": { "poolId": "p1", "amount": 30, "chip": "MK1", "remainingSeconds": 9.5 }
    },
    {
      "id": "r2", "name": "小麦", "color": "#f3a712", "position": { "x": 60.0, "y": 30.0 },
      "coins": 10000, "left": false, "rank": 1, "tokens": 150, "chips": {},
      "walking": { "target": { "x": 40.0, "y": 50.0 }, "remainingSeconds": 11.3 }
    }
  ],
  "game": { "ended": false, "remainingSeconds": 1199.5 },
  "speech": [
    { "seq": 1, "time": 12.5, "speakerId": "r1", "text": "有人卖 token 吗？", "listeners": ["r3"] },
    { "seq": 2, "time": 15.0, "speakerId": "r1", "text": "我出高价", "to": "r4", "listeners": ["r4"] }
  ],
  "market": {
    "position": { "x": 50.0, "y": 50.0 }, "radius": 5.0,
    "basePrice": 20.0, "depth": 100.0, "halfLifeSeconds": 60.0, "pressure": 30.0,
    "chips": [
      { "model": "MK1", "price": 300, "tokensPerJob": 30 },
      { "model": "MK2", "price": 1200, "tokensPerJob": 60 },
      { "model": "MK3", "price": 3000, "tokensPerJob": 100 }
    ]
  }
}
```

- `coins` 是金币总额，含待处理报价中预留的金币，对所有人公开。`rank` 按总额排名，1 为最多，金额相同名次并列（1、1、3）；游戏结束后即为最终排名。
- `position` 是发送时所在的位置，行走中即途中的位置。`walking` 只在行走期间出现：居民从 `position` 沿直线匀速走向 `target`，`remainingSeconds` 后到达，客户端据此本地播放走路动画。
- `tokens` 是拥有的 token：携带的加出售报价中预留的。`chips` 是拥有的芯片，型号到块数，同样含出售报价中预留的，没有的型号省略。`mining` 只在采矿期间出现，`chip` 是干活的芯片型号，`remainingSeconds` 是发送时该次采矿的剩余秒数。`pools` 的位置和半径整局不变，`reserve` 是剩余储量。
- `market` 是收购点；没有收购点的地图省略它。`basePrice`、`depth`、`halfLifeSeconds` 是定价参数，`pressure` 是发送时的近期卖出压力。`chips` 是出售的芯片型号，按从差到好排列，`price` 是固定价格，`tokensPerJob` 是每次采矿的产量。客户端可以按收到时刻起经过的时间推算当前单价，只用于显示，成交以服务端为准。
- `speech` 是这个连接可见的最近 50 句，按先后排列：所有当众说的话，加上 `view_as` 所选居民发出或收到的私聊；没选居民时没有私聊。`time` 是开局以来的游戏秒数，`to` 只在私聊时出现，`listeners` 是听到的居民（不含说话人）。居民本人只从感知中拿到自己听到的。
- `remainingSeconds` 是发送时的剩余秒数，结束后为 0。状态只在变化时推送，客户端应以收到时刻为起点本地倒计时，到点后以服务端的 `ended` 为准。

协议在客户端 `packages/protocol` 中还有一份 TypeScript 定义。改动时两边同时修改并更新测试；不兼容的改动同时提升两边的 `PROTOCOL_VERSION`。

## 代码结构

源码按领域分成几个子包，测试目录 `tests/` 按同样的子包分组：

| 子包 | 内容 |
| --- | --- |
| `core/` | 世界模型与各玩法共用的机制：居民、坐标、物品栏与金币、事件队列；只依赖 `core/` 内的模块 |
| `economy/` | 经济：token 体力、芯片、矿池采矿、收购点、交易报价 |
| `residents/` | 居民的行动与认知：行走、感知、记忆、对话 |
| `game/` | 一局的规则与调度：操作原语、生命周期、到点结算、开局布局 |
| `server/` | 对外接口：FastAPI 应用与 WebSocket、协议 |
| `agents/` | AI 居民：驱动居民的 Agent 循环、工具与大脑 |

`game/actions.py` 汇集所有原语，`server/` 与 `agents/` 在最上层；`economy/` 只依赖 `core/`；`residents/` 与 `game/` 互相引用（`residents/` 用 `game/lifecycle.py` 检查能否行动，`game/` 用行走、感知等），所以不是严格分层。

| 文件 | 用途 |
| --- | --- |
| `src/aiverse_server/core/world.py` | 坐标、居民（含金币、是否出局、采矿与行走任务、记忆与笔记）、物品栏、事件队列、矿池、收购点、活动日志、发言记录、地图与世界模型（含是否结束、游戏时间），越界、重复 id 与事件、采矿、行走、记忆和日志一致性校验 |
| `src/aiverse_server/core/inventory.py` | 居民物品的存入、移出、计数、列出与清空，金币的收付；服务端内部接口，不是原语 |
| `src/aiverse_server/core/events.py` | 事件队列：可扩展的事件种类（`EventPayload` 及其发送、同意、拒绝钩子），发送、答复、撤回与收件箱查询 |
| `src/aiverse_server/economy/tokens.py` | token 体力：物品种类、拥有量、走路消耗 |
| `src/aiverse_server/economy/chips.py` | 芯片：三个型号的定价与产量，在收购点购买，居民携带的最好一块 |
| `src/aiverse_server/economy/mining.py` | 矿池采矿：需要芯片、按最好的一块定产量，开始、按游戏时间发放、取消、忙碌检查（含行走） |
| `src/aiverse_server/economy/market.py` | 收购点：随卖出下跌、随时间回升的市场价，卖出 token 换金币 |
| `src/aiverse_server/economy/trade.py` | 交易事件：购买报价 `PurchaseOffer` 与出售报价 `SaleOffer`，条款校验、预留与互换 |
| `src/aiverse_server/residents/walking.py` | 行走：速度、所需时间、途中位置、出发、到达、中途停下、下一次到达时刻 |
| `src/aiverse_server/residents/perception.py` | 感知与发现：每轮开始时 `begin_turn` 返回范围内的矿池、居民和新的活动、收购点与现价、芯片价目，并更新居民记住的矿池；`record_activity` 比较前后世界记录活动 |
| `src/aiverse_server/residents/memory.py` | 居民自选的记忆：写入长期或短期笔记、忘掉笔记、清除过期的短期笔记 |
| `src/aiverse_server/residents/speech.py` | 对话：听力半径、每句的 token 消耗、当众说或私聊 `say`，听到的人在说话时定下 |
| `src/aiverse_server/game/actions.py` | 操作原语：玩家与 AI 改变世界的唯一入口，校验并执行；含答复事件、发起购买与出售、退出游戏、采矿、卖 token 给收购点、买芯片的原语，执行后处理 token 耗尽并记录活动 |
| `src/aiverse_server/game/lifecycle.py` | 对局生命周期：能否行动的检查、出局（含 token 耗尽，行走者到达后出局；出局和结束时停下行走）、结束、按金币总额排名，默认时长 |
| `src/aiverse_server/game/clock.py` | 游戏时间到点的事：发放采矿、行走到达（及到达时 token 耗尽出局）并记录活动，下一个到点时刻 |
| `src/aiverse_server/game/layout.py` | 固定的初始地图与居民（含初始金币与 token）、地图中心的收购点，按种子随机放置的矿池 |
| `src/aiverse_server/server/protocol.py` | 消息模型、客户端消息解析、消息到原语与世界到视图的转换 |
| `src/aiverse_server/server/app.py` | FastAPI 应用与 WebSocket `/ws`：共享世界、对局计时（到点的事见 `clock.py`）、每轮开始的 `begin_turn`、写入与忘掉笔记、Agent 与网页共用的执行入口 `act`/`commit`、世界变化时唤醒 Agent 的 `subscribe`、握手、执行原语、广播状态，随应用启停 Agent |
| `src/aiverse_server/agents/` | AI 居民：`runner.py` 事件驱动的循环与唤醒规则，`observation.py` 每轮的观察，`tools.py` 绑定居民的工具，`scripted.py` 脚本机器人，`openrouter.py` OpenRouter 模型大脑，`rules.py` 给模型的规则说明 |
| `src/aiverse_server/__main__.py` | 启动入口，`--agent` 指定由 Agent 驱动的居民，`--agent-log` 写每轮记录 |
| `tests/` | 世界模型、操作原语、物品栏与金币、事件、交易、对局生命周期、token、采矿、芯片、收购点、行走、感知、记忆、对话、AI 居民（工具、观察、唤醒、脚本机器人的快进对局、模拟 HTTP 的 OpenRouter 大脑）与 WebSocket 接口测试 |
