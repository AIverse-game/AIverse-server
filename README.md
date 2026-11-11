# AIverse 服务端

AIverse 的权威游戏服务端。游戏状态只在这里计算，网页客户端（同级目录 `aiverse-client`）只接收并渲染。开发规则见本仓库的 `AGENTS.md` 与 `.agents/`；架构、设计与决策记录在客户端仓库的 `docs/` 中，从 `docs/architecture.md` 读起。

当前功能：生成一张固定布局的正方形地图（边长 150，5 × 5 个 30 × 30 的街区，中间街区是出生大厅）和 8 名居民，所有居民从地图中心出生，每人开局 10000 金币，客户端经 WebSocket 读取；通过操作原语改变世界，客户端可用的有行走（`move_to`）、退出（`leave_game`）、说话（`say`）、买地（`buy_parcel`）、建设施（`build_facility`）、制作内容（`make_content`）和放入内容（`place_content`）。一局 20 分钟（可配置），到点自动结束并给出按金币的最终排名（`lifecycle.py`），地块和物品结束时不折算；居民可以中途出局，游戏照常进行。居民可以持有物品，服务端内部有存入、移出、计数、列出和清空的接口（`inventory.py`），当前游戏里的物品是广告与游戏内容（`ad_content`、`game_content`）。居民之间需要对方同意的请求（如购买）作为事件排进世界中的队列，由收件人同意或拒绝、发件人撤回（`events.py`）；交易（`trade.py`）：买方或卖方任一方用原语 `OfferToBuy`、`OfferToSell` 发起报价，自己的金币或物品立即预留，对方同意后互换，拒绝或撤回后退回；金币是居民的独立余额。这些原语和答复原语 `RespondToEvent`、`WithdrawEvent` 都还没有进入协议。地图划成 10 × 10 的地块（`land.py`，150 的地图共 15 × 15 块，编号 `L<列>-<行>`，从西北角数起），地块按 3 × 3 编成街区，站在没人买的街区里可以花 3200～8480 金币（旧地价的 4 倍，越靠近地图中央越贵，最贵的一块不到开局的 10000 金币但也占去大半；出生大厅不卖）向镇上买下整个街区，地块整局归买主，出局后也不收回，结束时不折算。游戏厅和工作空间（`build_venue`）各占街区里 2 × 2 块地（20 × 20，`WorldMap.venue_cells`），建在方块西北角那块地上，方块内的地块都得是自己的、空着的，建好和在建时别的设施都不能建在这四块地上（错误码 `parcel_covered`、`no_room_for_venue`），所以一个街区至多一个场馆，其余 5 块地仍可建广告位和传送门；脚本访客走到场馆方块的中心。商场（`build_venue` 的 `mall`）是第三种场馆，只占两块地（10 × 20，同一街区内，不看 `venueCells`）：朝向 `orientation` 为 `north_south`（默认，西北角那块地加正南的一块，入口朝南）或 `east_west`（加正东的一块，入口朝东），由地主定：自己建时在 `build_venue` 里选，委托建造时地主提议就写明，或建造者提议、地主同意时在 `RespondToEvent` 里指定（只有地主能改），许可 `Permit` 带着朝向，建造者只能照办（错误码 `wrong_orientation`、`invalid_orientation`），工作量 200（自带算力约 6.7 分钟，雇节点 3000 金币），不需要内容、建好就有访客，但吸引力只有工作空间的一半不到（`MALL_PIECES`），每人入场费只有 2 金币（`FEES`），收入远低于游戏厅，不在工作路线上；观察里 `land.mine[].mall_orientations` 列出能在此开建的朝向。算力（`compute.py`）是速度：工作量除以速率就是用时。每名居民有免费但慢的自带算力（每秒 1，一次一件）；也可以用金币向系统雇一台节点做一件工作（`on_demand`，每单位工作量 15 金币（刻意定得很贵，只作应急）），或订阅一台生成式节点一段时间（`buy_node`，一次订 300～1200 秒，不能按秒零买，每秒 12 金币，约为雇用的 1/12.5 每工作量，忙闲都收，期间这台节点归自己、一次一件、每秒 10，每人最多 4 台）。用工作在自己的空地块上建广告位（`build_ad`，工作量 120：自带算力 2 分钟，节点 12 秒；雇节点 360 金币），不用站在地块里，也不用守着，边走边做都行，订阅到期时没做完的工作，剩下的部分按自带算力的速度（每秒 1）做完，这条尾巴不占自带算力“一次一件”的名额；出局或对局结束时进行中的工作和订阅的节点取消，不退款，广告位不会出现。订阅的节点分生成式（建广告位）和渲染式（做内容）两种。内容（`ads.py`）是物品 `ad_content`，用渲染工作做出来（`make_content`，工作量 60：自带算力 1 分钟，节点 6 秒；雇节点 180 金币），可以买卖，也可以放进自己地块上的广告位（`place_content`，不用站在地块里）。广告位一次展示至多 5 份，每份从放进去起展示 300 秒后过期。广告位按看到它的人数赚钱（`sponsors.py`）：三个赞助商（系统扮演的镇外买家）各带一份预算，每游戏秒按各自的速率增加（共 10 金币/秒，最多攒 120 秒），按各自的出价（每人 2.4、1.8、1.2 金币）买展示；每 30 秒（游戏时间 30 的整倍数，时钟晚醒也在醒来后第一刻举行、只举行一次）是付款日，所有广告位自上次以来的展示合在一起，出价高的赞助商先买，预算不够时每个广告位按同一比例、以平均价付钱，买不到的展示作废，不留到下次；钱来自镇外（流水账渠道 `sponsors`），按份额分给地块的持有人。没有内容的广告位没有访客，也就没有收入。每块地分成 100 份，买地时全部归买主，持有人可以用原语 `OfferShares` 把份额按街区整体转给别人（点名街区里任一块地，街区里 9 块地同时转）（`shares.py`，对方同意并付款后生效，价格可以是 0），收入按份额比例分账、凑不整的金币按小数部分大的先得；同一人给同一名居民最多同时挂 3 份份额报价。持有任意份额的人都能在这块地上建设施（`build_ad`、`build_venue` 等，不需要许可）和放内容；同一块地上可以同时有几件建造工作，先完工的建成设施，其余的取消，委托酬金退回地主。两个人也可以合买一块地（`joint_purchase.py`）：站在没人买的地块里的人用原语 `OfferJointPurchase` 向另一名居民提议，写明对方出多少金币、拿多少份，自己出剩下的价款并留下其余份额，发出时不预留、不锁任何金币，对方同意时双方才各付镇上一笔（流水账两条 `land`），任一方当时钱不够则同意失败、报价留着；买地人是提议的人，双方都持有份额、都能建；对方不必在场，发出 300 秒（`JOINT_PURCHASE_SECONDS`）无人答复就自动作废（观察里 `joint_purchases_to_me[].expires_in` 是剩余秒数），地块被别人先买走时同意会失败、报价留着等撤回，同一人给同一名居民最多同时挂 3 份。内容在交易所有挂单簿（`ad_content`）。传送门（`portals.py`）是另一种设施，用生成式工作建在自己的空地块上（`build_portal`，工作量 180：自带算力 3 分钟，节点 18 秒；雇节点 540 金币；每人最多 4 座，建好和在建的合计），本身不收钱；两座相距至少 30 单位的传送门可以连成一条线路（`LinkPortals`），线路带过路费（0～1000 金币），两座门都是自己的立即连通，否则向另一座门的主人提议（事件 `LinkProposal`，同意后连通），每座门最多连 3 条线路。线路缩短“有效距离”：到地图中央的距离取直线，和“走到一座门、过线路不花时间、再从同一线路网里离中央最近的门走”里较短的那个，`effective_distance` 只算出到中央的距离，没有人用；脚本访客走路时用 `Router`（直走与走线路网里较短的）。站在传送门所在地块的居民用原语 `Teleport` 立刻传到线路另一端，不花行走的时间，付线路的过路费，一半归出发端、一半归到达端（奇数的一枚归出发端），各自按份额分给持有人（流水账渠道 `toll`，自己付给自己的部分不记）。地图上有 200 个脚本访客（`visitors.py`）：从中央入口进来，按内容份数（游戏厅里的游戏放得越久吸引力越小，每 600 秒减半，`ads.appeal`）、走路时间、拥挤度和多样性（刚离开的场馆同类的场馆效用减 `SAME_KIND_COST`）的偏好依次逛 2～4 个展示着内容的广告位再离开，走不走传送门由他们自己选（每走过一条线路，每人付系统定的 `VISITOR_TOLL` = 1 金币，一半归线路两端的地块的持有人，不付主人定的过路费，选路时不计这笔钱，走过的线路数取最少的）；地图按街区分岛时访客在岛内走直线，跨岛只能过桥（居民不受限），见 `islands.py`，路过或停留够 4 秒算一次展示，累计到各地块被看到的人数（AI 的观察里是 `land.mine[].people_seen`），赞助商按这个人数付钱；随机用种子加编号的哈希，同一局可重现，访客的走动不进 `state`，由单独的 `visitors` 消息每 2 秒发一次（见“协议”）。另外，别人的地块也能请人来建（`commissions.py`）：地主向建造者、或建造者向地主，用原语 `OfferBuildCommission` 提议在某块地上建广告位、游戏厅或工作空间并写明酬金，对方同意后才生效（拒绝、撤回、出局时酬金退回地主）；同意后地主的酬金预留（计入排名），建造者得到一张 300 秒有效的许可 `Permit`，用 `build_ad` / `build_venue` 在那块地上开工（用自己的算力或节点），建成时酬金付给建造者（流水账渠道 `commission`），许可过期没开工、建造者出局或对局结束则酬金退回；设施仍属地块，收入归份额持有人，没有份额的建造者靠许可开工。这是[虚拟设施](../aiverse-client/docs/design/facilities.md)第 5 步之后的状态，旧玩法（算力泉、采集、采集节点、收购点、数据与数据矿、程序、工作室、素材、服务器与会议室、广告位）保留在 `legacy` 分支。

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
uv run aiverse-server --duration 120  # 本次启动覆盖游戏配置中的对局时长
uv run aiverse-server --config my.yml # 大模型配置文件，默认读 agents/config/llm.yml（可以没有），见下
uv run aiverse-server --game-config my-game.yml # 游戏时间配置，默认读 game/config/game.yml（可以没有）
uv run aiverse-server --agent r1=scripted --agent r2=llm:模型名   # r1、r2 由 Agent 驱动，见下
make verify                           # ruff、ruff format --check、mypy strict、pytest
uv run pytest tests/unit              # 只跑单元测试；集成测试在 tests/integration
```

一局游戏从服务端创建世界（即启动）时开始计时，到点后自动结束：停下所有行走、撤销所有待处理报价、终止所有合约、退回预留，世界冻结。要开新的一局，重启服务端。

网页端开发与预览服务器默认把 `/ws` 代理到 `127.0.0.1:8000`。服务端换端口时，客户端用同一端口启动：`AIVERSE_SERVER_PORT=9000 npm run dev`。

尚无身份认证、Origin 检查和消息大小限制，任何连接都能操作任何居民、把居民交给 AI（服务端会用配置文件里的 key 发请求），API key 也在 WebSocket 上明文传输，不要监听公网地址。每个连接只保留最新一份待发的 `state`，对自己操作的回复最多积压 256 条，超过就以 1008 关闭。

## AI 居民

居民可以由 Agent 驱动，有两种方式：

- **页面**：在“AI 驱动”面板选中居民，填写 API 地址、API key 和模型，点“交给 AI”；之后可以按新配置重新交给 AI，或“停止 AI，改为手动”。页面按居民把填过的设置记在本机浏览器的 localStorage。
- **启动参数**：`--agent 居民=大脑`，可重复。

调度与选型见客户端 `docs/decisions/0003-event-driven-agents-and-openrouter.md` 与 `0004-player-configured-llm-endpoints.md`。

```sh
uv run aiverse-server --agent r1=scripted                    # 固定策略的脚本机器人，不调用模型
uv run aiverse-server --agent r2=llm:模型名                  # 大模型，地址和 key 来自配置文件
uv run aiverse-server --agent r1=scripted --log-dir logs    # 每名居民独立日志，每局覆盖
```

大模型接口须兼容 OpenAI 的 Chat Completions 并支持工具调用。服务端不内置任何厂商：API 地址填到 `/v1` 一级，例如 `https://openrouter.ai/api/v1`，服务端在后面加 `/chat/completions`。

配置文件是 YAML，用 `--config` 指定，不指定时读 `src/aiverse_server/agents/config/llm.yml`（存在时；与从哪个目录启动无关）。复制同目录的 `llm.example.yml` 为 `llm.yml` 再填写；`llm.yml` 可能含 key，已被 Git 忽略，也不打进构建产物。

```yaml
override:
  base_url: https://openrouter.ai/api/v1
  api_key: sk-...
  model: nvidia/nemotron-3.5-lightning
  max_tokens: 16384
groups:            # 可选：给部分居民换一套接口
  - residents: [r5, r6, r7, r8]
    base_url: https://other.example/v1
    api_key: sk-...
    model: other-model
```

`override` 下填了的地址、key 和模型代替页面上填的值和 `--agent 居民=llm:模型` 中的模型，对所有由模型驱动的居民生效；没填的项仍用玩家给的。`groups` 可选，每组用 `residents` 列出居民 id，再填任意几项 `base_url`、`api_key`、`model`：组里填了的项代替 `override` 的同名项，只对所列居民生效，没填的项仍用 `override`，比如测试时让一半居民用一个接口、另一半用另一个。一名居民最多在一个组里，`max_tokens` 不能按组设，组里列了不存在的居民时启动报错。组里只换了 `base_url`、没写 `api_key` 时，用的是 `override` 的 key，会把它发到组的地址；两组是不同服务商时，每组都要写自己的 key。页面 `welcome` 的 `llmOverrides` 只列对每名居民都固定了的设置。`max_tokens` 是所有模型请求共用的每次回复上限，必须是正整数，省略时为 8192；页面启动和命令行启动的 Agent 都使用它。进入游戏配置的第一条结束公告提前量后，每次请求的上限取配置值与 8192 中较小者，并提示模型简短推理、优先及时行动；同一轮中跨入该版本段也会生效。三项连接设置都填了时，页面上只需点“交给 AI”。`--agent 居民=llm` 的地址和 key 只能来自配置文件，模型可以来自参数或配置文件，缺项时启动报错。拼错的设置名或无效的 `max_tokens` 会让启动报错，错误信息不显示设置的值。

游戏时间配置单独放在 `src/aiverse_server/game/config/game.yml`，可复制同目录的 `game.example.yml`，也可用 `--game-config FILE` 指定路径；文件不存在时使用默认值。它与大模型的 `llm.yml` 分开，三个值单位均为秒：

```yaml
duration_seconds: 2400
early_notice_seconds: 300
final_notice_seconds: 120
```

默认值分别为 1200、180、60。第一条公告提前量必须大于第二条，三项必须是有限的正数；对局时长不大于某条公告的提前量时跳过该条。`--duration SECONDS` 只覆盖对局时长，其他两项仍取游戏配置。配置中的时间变化会同步到公告文案、模型的系统提示和临近结束时的回复上限触发时刻；公告仍要等当前模型请求结束才进入下一轮观察。游戏配置和模型配置的未知键都会在启动时拒绝。

key 只放在服务端内存里，用来调用接口，不写进状态、控制台或角色/汇总日志；服务商在错误信息里回显的 key 替换为 `***`。httpx 的请求日志调到 WARNING，不打印请求地址。

- 每名居民一个异步循环（`agents/runner.py`）：感知 → 大脑决策一轮并调用工具 → 等待唤醒。唤醒条件：行走结束、自己的工作做完（建成设施或做出内容，走路时也唤醒；工作在一轮之内开始又做完的不算）、空闲时收到报价、空闲时自己挂在交易所的订单被别人成交、空闲时自己的合约因别人或时间而结束、听到别人说话（忙碌时也唤醒）、空闲 20 秒无事、空闲时大脑上一轮出错。两轮开始至少相隔 3 秒，大脑出错后 10 秒。居民出局或游戏结束时停止。
- 工具（`agents/tools.py`）绑定到一名居民，参数里没有“谁来做”：`move_to`、`buy_parcel`、`joint_buy_parcel`、`commission_build`、`buy_node`、`build_ad`、`build_portal`、`build_venue`、`link_portals`、`teleport`、`make_content`、`place_content`、`place_order`、`cancel_order`、`say`、`offer_to_buy`、`offer_to_sell`、`offer_information`、`offer_shares`、`respond_to_offer`、`withdraw_offer`、`propose_contract`、`deliver`、`cancel_contract`、`remember`、`forget`。`respond_to_offer` 与 `withdraw_offer` 也用来答复和撤回合约提议、信息报价、份额报价、合买提议、线路提议和建造委托。不提供退出游戏。参数错误、未知工具、原语被拒绝都作为失败结果交回，世界不变。
- 通常只发送当前观察适用的工具定义（`agents/tool_selection.py`）：例如脚下是没人买的地块时显示买地，有空着的自有地块时显示 `build_ad` 和 `build_venue`，任何时候显示 `make_content`（金币够时还有 `buy_node`），手里有某种内容且有收这种内容、未满的广告位或游戏厅时显示 `place_content`，有待答复报价时显示答复工具；有两座传送门（或一座加别人的一座）时显示 `link_portals`，站在有线路的传送门里时显示 `teleport`，`build_portal` 只在 `transport` 组里。`say`、`remember` 始终可见；有笔记时显示 `forget`，有自己的挂单时显示 `cancel_order`。模型可用 `discover_tools(group)` 在本轮展开 `land`、`compute`、`content`、`transport`、`exchange`、`offers`、`contracts` 或 `memory` 组；它只改变本轮后续请求发送的定义，不执行游戏动作，但占用一次模型请求。若本轮或本局只剩最后一次模型请求，则直接发送当前状态下仍可能执行的全部动作定义，不再提供展开入口。下一轮重新按新的观察选择。实际动作仍由 `ResidentTools` 校验和执行。
- 观察（`agents/observation.py`）：感知结果加上自己的金币、物品、行走状态（`walking_to`、`arrives_in`），所有居民的名字、金币和名次，发给自己的和自己发出的报价、信息报价（只含摘要和价格）与合约提议，自己参与的合约和最近结束的几份（结束原因、没交付的次数、赔付的押金）；算力（`compute`）：自带算力是否在忙、自己订阅的节点（剩余时间、是否在工作）、进行中的工作，以及建一个广告位（`ad_slot`）、传送门（`portal`）、游戏厅或工作空间（`venue`）和做一份广告（`content`）或游戏（`game`）在各种来源上要多久、多少金币；地块（`land`）：网格大小、广告位容量、内容展示时长与付款周期（`slot_capacity`、`content_lasts`、`pay_period`）、脚下地块（`here`，价格、主人、此刻经过的人数 `people_near`）、自己的地块（`mine`，广告位里每份内容还剩多久过期、上一个付款日的收入 `income_last_payday` 与自己的份、累计看到的人数 `people_seen`）、赞助商（`sponsors`，出价、预算速率、现有预算）和此刻经过人数最多的几块空地（`best_free`），都带离自己的距离与走路秒数；交易所（`exchange`）每种商品买卖各前 3 个价位与数量、最近成交价与 120 秒成交量，以及最近 5 分钟至多 8 笔匿名成交（不含买卖双方），自己的挂单和最近 10 笔成交，`me.coins_in_orders` 是买单预留的金币。上一轮和此前各轮平均用了多少游戏秒（`last_thinking_seconds`、`avg_thinking_seconds`，从感知到大脑结束本轮，含失败的轮次，第一轮为 null），系统提示同时告诉模型思考占用游戏时间。
- 脚本机器人（`agents/scripted_brain.py`）：每到一个点说一句 “Hello!”，按固定的四个点走一圈，买下脚下第一块买得起的空地（只买一次），用自带算力在上面建一个广告位，然后只要自带算力空闲就做内容、把手里的内容放进广告位，走完一圈后继续照看广告位。
- 大模型大脑（`agents/llm_brain.py`）：系统提示是精简的稳定规则说明（`agents/system_prompt.py`，数值取自代码常量与游戏时间配置），工具参数由工具定义说明，实时状态由观察给出；提示说明广告位靠路过获得展示、访客去场馆并在门口付费、内容怎么做怎么放、怎么按流量计酬，只有金币计入排名、地块和物品结束时一文不值；用户消息是观察和上一轮的行动结果；模型每轮最多请求 8 次，每局每名居民最多 400 次（停止或更换 Agent 不重置），每次回复默认最多 8192 token，可通过配置文件的 `override.max_tokens` 调整（推理模型要先思考再调用工具，2048 时一局里一半以上的轮次被截断）；进入第一条结束公告提前量后按上述 8192 上限收紧，每次请求最多等 180 秒。一批工具调用之后居民处于行走中，本轮即结束，不再请求，等行走结束时唤醒。居民空闲且本轮尚未调用工具时，空回复或因长度截断的回复再问一次，仍然如此则作为失败（错误中带 `finish_reason`）。请求失败时由循环记录，居民空闲时 10 秒后再问。停止或更换 Agent 时关闭它的 HTTP 客户端。
- 控制台每轮打印一行摘要；默认同时在服务端项目 `logs/` 为每名居民创建独立的 JSONL 文件，每次开新局覆盖。详见下文“角色日志”。

## 信息售卖

居民可以把自己知道的信息卖给别人（`residents/information_offer.py`）：信息是可复制的，卖方成交后仍保留，也可以再卖给其他人。

- 卖方用原语 `OfferInformation` 发出报价（事件 `InformationOffer`）：`summary`（摘要，最多 60 字符）、`content`（内容，自由文本，由 AI 自己撰写）和 `price`（金币，至少 1）。买方成交前只能看到摘要和价格，看不到内容。目前只有卖方能发起，买方不能先求购。
- 发出时不预留任何东西，所以同一卖方对同一买方最多同时有 3 条待答复的信息报价（超过以 `too_many_offers` 拒绝），免得刷屏。买方用 `RespondToEvent` 同意时，金币付给卖方，内容连同卖方 id 和摘要写成买方的一条长期笔记（`Bought from <卖方> (<摘要>): <内容>`），所以摘要、内容和这些前缀合起来不得超过笔记的 600 字符上限。买方金币不足，或长期笔记已满（要先 `forget` 一条）时，同意被拒绝（`not_enough_coins`、`memory_full`），报价仍待处理。拒绝、撤回、出局、游戏结束时没有什么需要退回。
- 服务端不核实内容是否属实，买方只能凭对卖方的信任判断；这是有意的，真假与信誉交给居民博弈。
- AI 居民用工具 `offer_information` 发起，用 `respond_to_offer`、`withdraw_offer` 答复和撤回；观察中的 `information_offers_to_me`、`my_information_offers` 只含摘要和价格。
- 尚未进入协议：页面不显示信息报价（`offers` 里只有物品报价），也不能操作。

## 合约

合约规定两名居民之间未来的交付与付款（`economy/contracts.py`，设计见客户端 `docs/design/contracts.md`）：供货方应在签订后 `duration` 秒内交付 `deliveries` 次（1 即一次性），每次 `quantity` 个 `kind`，每次得到 `price` 金币；供货方可以为每次交付押 `deposit` 金币（默认 0）。

- 任一方用原语 `ProposeContract` 发出提议（事件 `ContractProposal`），对方用 `RespondToEvent` 同意即签订，发件人可撤回。发件人提议时立即预留自己的一份：买方是全部货款（`price × deliveries`），供货方是全部押金（`deposit × deliveries`）；收件人的一份在同意时扣，不够则拒绝且提议仍待处理。
- 生效后货款和押金都留在合约里。供货方用 `Deliver` 交出一批物品，立即从预留中得到这一批的价款，并拿回这一批的押金，买方不能拒付；最后一批交付后合约结束。
- 只有供货方能 `CancelContract`，买方不能单方面取消。到期（`settle_due`）、任一方出局、游戏结束时合约终止。终止时余款退回买方；还没交付的押金在到期、供货方取消或出局时赔给买方，在买方出局或游戏结束时退回供货方。
- 世界保留最近 100 份结束的合约（`ContractBook.ended`：结束原因、由谁造成、赔付的押金），Agent 从中得知合约怎样结束，并据此唤醒。
- 预留的货款和押金分别计入买方和供货方的金币总额和排名。交付与普通原语一样，在行走中不能进行。
- 页面可以在状态中看到所选居民参与的合约与提议（见下文“协议”），但还不能发起、答复、交付或取消。

## 协议

WebSocket 地址为 `/ws`，消息是 JSON 文本帧，键名用 camelCase。当前协议版本为 32。

| 方向 | 消息 | 说明 |
| --- | --- | --- |
| 客户端 → 服务端 | `{"type": "hello", "protocolVersion": 28}` | 连接后发送；多余字段忽略 |
| 客户端 → 服务端 | `{"type": "view_as", "residentId": "r1"}` | `hello` 成功后才能发送：选择这个连接以哪名居民的身份看私聊、报价、合约与挂单，`null` 为不以任何人身份。回复一份新的 `state`；居民不存在时回复 `bad_message`，原选择不变。尚无归属，可选任何居民 |
| 客户端 → 服务端 | `{"type": "action", "action": {...}}` | 操作原语，`hello` 成功后才能发送，见下 |
| 客户端 → 服务端 | `{"type": "start_agent", "residentId": "r1", "llm": {"baseUrl": "...", "apiKey": "...", "model": "..."}}` | `hello` 成功后才能发送：把居民交给大模型，同时替换原有的 Agent。三项都可以省略或留空，由配置文件覆盖的项不用填；每项最多 2048 字符。尚无归属，可选任何居民 |
| 客户端 → 服务端 | `{"type": "stop_agent", "residentId": "r1"}` | `hello` 成功后才能发送：停止驱动该居民的 Agent，没有 Agent 时什么也不做 |
| 服务端 → 客户端 | `{"type": "welcome", "protocolVersion": 28, "llmOverrides": ["apiKey"]}` | 回应 `hello`。`llmOverrides` 是配置文件覆盖的设置（`baseUrl`、`apiKey`、`model`），只列名字、不含值，没有时为空列表 |
| 服务端 → 客户端 | `{"type": "state", "world": {...}}` | 紧跟 `welcome`；世界变化后、游戏结束时也推送给每个已握手的连接。内容见下 |
| 服务端 → 客户端 | `{"type": "visitors", "speed": 2.5, "visitors": [{"id": 3, "position": {...}, "legs": [{"start": {...}, "end": {...}}]}]}` | 脚本访客不在 `state` 里：握手后紧跟 `state` 发一次，之后每 2 秒（`VISITOR_PUSH_SECONDS`）发给每个已握手的连接，世界里没有访客时不发。每份是一张快照：`position` 是发送时的位置，`legs` 是还要走的直线段（按 `speed` 每秒走多少地图单位，第一段从 `position` 起；一段的终点和下一段的起点不同表示走了传送门，不花时间），走完停在最后一段的终点，`legs` 为空表示站着。下一份会整个替换这一份，不在其中的访客已离开地图（在城外的访客不发）。每个连接只保留最新一份待发的 |
| 服务端 → 客户端 | `{"type": "action_result", "ok": true}` | 回应发送者的 `action`。被拒绝时 `ok` 为 `false`，附 `code`（如 `unknown_resident`、`out_of_bounds`、`resident_left`、`game_over`、`busy`、`empty_speech`、`speech_too_long`、`invalid_speech`、`unknown_listener`；客户端应接受未知代码）和 `message` |
| 服务端 → 客户端 | `{"type": "agent_result", "residentId": "r1", "ok": true}` | 回应发送者对 `residentId` 的 `start_agent` 或 `stop_agent`，成功后所有连接收到新的 `state`。被拒绝时 `ok` 为 `false`，附 `code`（`unknown_resident`、`resident_left`、`game_over`，与配置文件合并后缺项的 `missing_llm_setting`，地址不是 http(s) URL 或 key 不是不含空格的可打印 ASCII 的 `invalid_llm_setting`；客户端应接受未知代码）和 `message`，不含 key |
| 服务端 → 客户端 | `{"type": "error", "code": "...", "message": "..."}` | `bad_message` 或 `protocol_mismatch`；`message` 不超过 200 字，连接保持可用 |

客户端可用的原语：

```json
{ "type": "move_to", "residentId": "r1", "target": { "x": 30.0, "y": 40.5 } }
{ "type": "leave_game", "residentId": "r1" }
{ "type": "say", "residentId": "r1", "text": "有人想合伙买地吗？" }
{ "type": "say", "residentId": "r1", "text": "我出高价", "to": "r4" }
{ "type": "buy_parcel", "residentId": "r1" }
{ "type": "build_facility", "residentId": "r1", "parcelId": "L7-7", "kind": "game_hall", "source": "on_demand" }
{ "type": "make_content", "residentId": "r1", "content": "game", "source": "self" }
{ "type": "place_content", "residentId": "r1", "parcelId": "L7-7" }
```

坐标必须是有限的数字（不接受布尔值、字符串、NaN 和无穷），否则回复 `bad_message`。居民不存在或目标在地图外时回复被拒绝的 `action_result`，世界不变。`move_to` 让居民沿直线以每秒 2.5 地图单位走向目标，到达前忙碌，除 `leave_game` 和 `say` 外的原语以 `busy` 拒绝；每回合能走多远尚未设计。走路只花时间，不花别的；原地不动不出发。到达时推送状态；中途出局或游戏结束时停在当时所在的位置。

`say` 让居民说一句话，`text` 去掉首尾空白后为 1～300 字符，否则以 `empty_speech` 或 `speech_too_long` 拒绝；含孤立代理字符（JSON 中 `\ud800` 一类转义）、不能编码为 UTF-8 的文字以 `invalid_speech` 拒绝。省略 `to` 时当众说，说话那一刻 20 单位内（含边缘，行走者按途中位置）仍在局中的其他居民听到；给出 `to` 时只有那名居民听到，不论远近，对象不存在、已出局或是自己时以 `unknown_listener` 拒绝。谁听到在说话时定下，之后走近的人听不到。说话不花东西，行走中也能说。

`leave_game` 让居民出局：与其有关的待处理报价撤销并退回预留，物品清空，金币保留并参与排名；居民仍留在地图上，之后其操作一律以 `resident_left` 拒绝。游戏结束后，所有操作以 `game_over` 拒绝。交易与答复事件的原语还不在协议中。

居民的 `agent` 在不升协议版本的前提下多了两个可选字段（旧页面忽略）：`thinking: true` 表示 Agent 正在等大脑回答（`runner.py` 在大脑一轮的前后设置），`cue: {kind, seq}` 是它最近一次演出（`talk` 或 `happy`，`seq` 在整局内递增，页面见到新的 `seq` 才播放）；只有经 `Game.act` 做的原语和到点建成的设施产生提示，页面手动操作不产生。

`world` 在协议 32 还包括 `map.parcelSize`、`map.blockCells`、`map.venueCells`（游戏厅和工作空间占的方块边长，单位是地块，默认世界为 2，即 20 × 20） 与按行排列的 `map.parcelPrices`、公开的 `parcels`（产权人、份额、设施种类、有效内容数、每份内容的 `expiresIn` 剩余秒数，以及上一个付给它钱的付款日里它收到的 `income`：`at` 是那个付款日的游戏时间，`coins` 是赞助商和入场费合计的整数金币，没有付过时省略；页面靠 `at` 变大判断是新的一笔）、`jobs`（进行中的工作及剩余秒数）；只有 `view_as` 选择居民后才有 `content`（`viewerId`、该居民携带的广告与游戏数量）。设施可为 `ad`、`portal`、`game_hall`、`workspace`、`mall`、`garden`（占一块地，与广告位同样的建设来源）；`game` 带 `ended`、`remainingSeconds` 和 `elapsedSeconds`（发出时的游戏时间，对局结束后不再增长，页面的花园季节与昼夜按它算）；游戏厅和工作空间占一个 `venueCells` × `venueCells` 的方块，商场占两块（西北角在设施所在的地块，`orientation` 为 `north_south` 时向南一块、`east_west` 时向东一块，设施和进行中的工作的视图都带这个字段），设施记在方块西北角的那块地上，方块里其余的地块没有设施，别的设施也不能建在上面（页面按 `venueCells` 自己推算）；建设来源为 `self` 或 `on_demand`，订阅节点尚未进入页面操作。

`world` 的部分结构示意（省略 `parcelPrices`、`parcels`、`jobs` 等必填字段，不能直接作为完整协议消息复制使用）：

```json
{
  "map": { "size": 150.0, "parcelSize": 10.0, "blockCells": 3, "venueCells": 2, "lobby": true },
  "residents": [
    {
      "id": "r1", "name": "阿青", "color": "#e4572e", "position": { "x": 20.0, "y": 25.0 },
      "coins": 9700, "spendableCoins": 9700, "left": false, "rank": 2
    },
    {
      "id": "r2", "name": "小麦", "color": "#f3a712", "position": { "x": 60.0, "y": 30.0 },
      "coins": 10000, "spendableCoins": 10000, "left": false, "rank": 1,
      "walking": { "target": { "x": 40.0, "y": 50.0 }, "remainingSeconds": 11.3 },
      "agent": { "model": "vendor/model-name" }
    }
  ],
  "game": { "ended": false, "remainingSeconds": 1199.5 },
  "exchange": {
    "books": [],
    "fills": [],
    "viewerId": "r1",
    "orders": [],
    "maxOrders": 8
  },
  "speech": [
    { "seq": 1, "time": 12.5, "speakerId": "r1", "text": "有人想合伙买地吗？", "listeners": ["r3"] },
    { "seq": 2, "time": 15.0, "speakerId": "r1", "text": "我出高价", "to": "r4", "listeners": ["r4"] }
  ],
  "offers": [],
  "contractProposals": [],
  "contracts": []
}
```

- `coins` 是金币总额，含待处理报价、合约（见上文“合约”）和买单中预留的金币；`spendableCoins` 是其中可以花的，对所有人公开。`rank` 按总额排名，1 为最多，金额相同名次并列（1、1、3）；游戏结束后即为最终排名。
- `position` 是发送时所在的位置，行走中即途中的位置。`walking` 只在行走期间出现：居民从 `position` 沿直线匀速走向 `target`，`remainingSeconds` 后到达，客户端据此本地播放走路动画。
- `agent` 只在居民由 Agent 驱动时出现，`model` 是所用的模型，脚本机器人没有这一项。居民出局、游戏结束或 Agent 被停止后消失。
- `exchange` 是匿名交易所。`books` 每种有挂单簿的商品一本（目前游戏里没有任何商品，所以为空），`bids` 是最高的 5 个买价、`asks` 是最低的 5 个卖价，每个价位给出单价和总数量，不显示是谁挂的。`fills` 是所有商品最近 100 笔成交，从旧到新，只有时间（取到 0.1 秒）、商品、数量和单价；`view_as` 所选居民参与的那笔带 `own`（`bought` 或 `sold`）。`viewerId` 是生成这份状态时所选的居民，没选时省略，页面换选居民后可以据此认出还没换过来的旧状态。`orders` 是所选居民的挂单，没选居民时为空，`remainingSeconds` 是发送时距到期的秒数。`maxOrders` 是每人最多的挂单数。
- `speech` 是这个连接可见的最近 50 句，按先后排列：所有当众说的话，加上 `view_as` 所选居民发出或收到的私聊；没选居民时没有私聊。`time` 是开局以来的游戏秒数，`to` 只在私聊时出现，`listeners` 是听到的居民（不含说话人）。居民本人只从感知中拿到自己听到的。
- `offers`、`contractProposals`、`contracts` 是 `view_as` 所选居民参与的待答复报价、待答复合约提议和生效的合约，按先后排列；和私聊一样，没选居民时为空，别人的交易不发给这个连接。报价的 `side` 为 `sell` 时发件人卖、为 `buy` 时发件人买，`price` 是整批总价；合约的 `price` 是每次交付的价款，`deposit` 是供货方每次的押金（0 为不押），`escrow` 是托管的余款（`price × deliveriesLeft`），`depositHeld` 是托管的押金（`deposit × deliveriesLeft`），`remainingSeconds` 是发送时距到期的秒数。页面还不能发起或答复它们。
- `worldLog` 是世界日志：全镇最近 200 件事，从旧到新，每个连接都一样，不看 `view_as`，所以别人的私聊和报价也在里面（页面的“世界日志”窗口，见客户端仓库的 `docs/design/game-screen.md`）。每条有 `seq`、`time`（游戏秒数）、`verb`（发生了什么）、`actorId`，其余字段按动作出现：`otherId`（对方居民）、`text`（说的话、信息报价的摘要）、`parcelId`、`otherParcelId`、`facility`、`item`、`quantity`（物品、份额、地块、节点秒数或听到的人数）、`coins`、`deliveries`、`position`、`deal`（请求种类：`purchase`、`sale`、`information`、`shares`、`contract`、`joint_purchase`、`commission`、`link`）与 `side`。`verb` 有 `said`、`walked`、`teleported`、`left_game`、`bought_parcel`、`bought_node`、`started_building`、`started_making`、`built`、`made`、`placed_content`、`offered`、`accepted`、`declined`、`withdrew`、`delivered`、`cancelled_contract`、`placed_order`、`cancelled_order`、`linked_portals`。被拒绝的原语不记；交易所成交、付款日收入和各种到期不记。
- `remainingSeconds` 是发送时的剩余秒数，结束后为 0。状态只在变化时推送，客户端应以收到时刻为起点本地倒计时，到点后以服务端的 `ended` 为准。

协议在客户端 `packages/protocol` 中还有一份 TypeScript 定义。改动时两边同时修改并更新测试；不兼容的改动同时提升两边的 `PROTOCOL_VERSION`。

## 代码结构

源码按领域分成几个子包。测试目录 `tests/` 先按层分为 `unit/` 与 `integration/`，层内按同样的子包分组。根目录的 `.agents/` 放给 Agent 的规则与 Skill，入口是 `AGENTS.md`。

| 子包 | 内容 |
| --- | --- |
| `core/` | 世界模型与各玩法共用的机制：居民、坐标、物品栏与金币、事件队列、金币流水账、挂单簿模型、地块；只依赖 `core/` 内的模块 |
| `economy/` | 经济：算力与节点、地块、花园、脚本访客、赞助商、挂单交易所、交易报价、合约 |
| `residents/` | 居民的行动与认知：行走、感知、记忆、对话、信息售卖 |
| `game/` | 一局的规则与调度：操作原语、生命周期、到点结算、开局布局、经济报告 |
| `server/` | 对外接口：FastAPI 应用与 WebSocket、协议 |
| `agents/` | AI 居民：驱动居民的 Agent 循环、工具与大脑 |

`game/actions.py` 汇集所有原语，`server/` 与 `agents/` 在最上层；`economy/` 只依赖 `core/`；`residents/` 与 `game/` 互相引用（`residents/` 用 `game/lifecycle.py` 检查能否行动，`game/` 用行走、感知等），所以不是严格分层。

| 文件 | 用途 |
| --- | --- |
| `src/aiverse_server/core/world.py` | 坐标、居民（含金币、是否出局、行走任务、记忆与笔记）、物品栏、事件队列、生效的合约（`Contract`、`ContractBook`）、居民记忆（`Memory`，长短期笔记 `Note`）、活动日志、发言记录、公告、地图与世界模型（含是否结束、游戏时间），越界、重复 id 与事件、行走、记忆、日志和发言一致性校验 |
| `src/aiverse_server/core/inventory.py` | 居民物品的存入、移出、计数、列出与清空，金币的收付；服务端内部接口，不是原语 |
| `src/aiverse_server/core/land.py` | 不可变地块 `Parcel`（列、行、主人、设施、份额）、设施 `Facility`（广告位：展示中的内容的过期时刻、下一个付款时刻；游戏厅：展示中的内容的过期时刻（游戏恒为 1e9 秒后，不会过期），不付款；`land.mine[].content_appeal` 是内容对游客的吸引力折算成几份；传送门、工作空间：只有建成时刻）与线路 `Link`（两座传送门的地块编号和过路费） |
| `src/aiverse_server/core/compute.py` | 不可变的算力状态：订阅的节点 `Node`、进行中的工作 `Job`、在别人地块上开工的许可 `Permit`、`ComputeBook` 及校验 |
| `src/aiverse_server/core/admissions.py` | 不可变的入场费状态 `AdmissionBook`（未付的费用、累计进门的人数、上次付款日、上个付款日各设施收到的钱） |
| `src/aiverse_server/core/sponsors.py` | 不可变的赞助商状态：赞助商 `Sponsor`（出价、预算速率、现有预算）与 `SponsorBook`（上次充值时刻、各地块已结算的展示、上个付款日各地块的收入） |
| `src/aiverse_server/core/visitors.py` | 不可变的脚本访客状态：访客 `Visitor`（阶段、出发点与目标、起止时刻、走过的各段直线、目的、已路过的广告位、这一趟还要去的大致路线 `route`，空表示普通游客去任一场馆）与人群 `Crowd`（种子、全部访客、各地块被路过的人数） |
| `src/aiverse_server/core/ledger.py` | 金币流水账：每次真实转手的付款方、收款方、金额与渠道，有上限并计数丢弃的条目 |
| `src/aiverse_server/core/events.py` | 事件队列：可扩展的事件种类（`EventPayload` 及其发送、同意、拒绝钩子），发送、答复、撤回与收件箱查询 |
| `src/aiverse_server/core/orders.py` | 交易所的订单、成交与挂单簿模型，挂单预留的金币与物品 |
| `src/aiverse_server/economy/exchange.py` | 挂单交易所：下单、价格优先与时间优先的连续撮合、撤单、到期，挂单簿深度、最近成交价与成交量 |
| `src/aiverse_server/economy/compute.py` | 算力：速率与价格常量，买节点（生成式、渲染式）、开工建广告位、传送门、花园、游戏厅、工作空间或做内容（广告或游戏）、完工、节点和许可到期、取消 |
| `src/aiverse_server/economy/ads.py` | 广告位、游戏厅与内容：内容物品 `ad_content`（广告位）和 `game_content`（游戏厅）、放进对应的设施（广告展示 300 秒后过期；游戏不过期但吸引力每 600 秒减半，游戏厅最多 5 份，放满时新游戏顶掉最旧的一份；广告位放满则拒绝 `slot_full`）；收入见 `sponsors.py` 和 `admissions.py` |
| `src/aiverse_server/economy/gardens.py` | 花园：一块地的小设施，建成时镇上一次性奖励地主 `GARDEN_REWARD`（渠道 `garden_reward`），在的时候同街区所有设施的外来收入（赞助商付的、入场费，不含居民之间的过路费）加成 `GARDEN_BONUS` 十分之一（镇上另付，渠道 `garden_bonus`，按份额分给持有人），一个街区至多一座（建成或在建），只有地主能建；也吸引少量访客（`visitors.py` 的 `GARDEN_PIECES`，入场费 1 金币） |
| `src/aiverse_server/economy/payouts.py` | 设施上一个付款日收到的钱 `latest_payout`（赞助商与入场费两本账合计，带付款日时间 `at`），给协议里设施的 `income` 用 |
| `src/aiverse_server/economy/shares.py` | 地块份额：按份额分账 `split_income`，份额报价事件 `ShareOffer`（持有人把份额转给别人，可收费，可免费），已承诺的份额不能再承诺 |
| `src/aiverse_server/economy/commissions.py` | 在别人的地块上建：委托事件 `BuildCommission`（地主或建造者提议、对方同意，酬金由地主预留，同意后生成许可，建成时付给建造者），预留酬金的汇总 `commission_coins` |
| `src/aiverse_server/economy/joint_purchase.py` | 合买地块：提议事件 `JointPurchase`（对方出多少钱、拿多少份，不预留金币），同意时两人各付镇上、地块归提议人，超时未答复的提议自动作废 |
| `src/aiverse_server/economy/land.py` | 地块：网格、按离地图中央远近定价、购买，地图中央 `town_centre` |
| `src/aiverse_server/economy/portals.py` | 传送门与线路（[虚拟设施](../aiverse-client/docs/design/facilities.md)第 5 步）：线路提议事件 `LinkProposal`、`link_portals`、居民传送 `teleport`（过路费按份额分给两端的持有人）、有效距离 `effective_distance`（线路让偏远地块离中央更近，备用，流量暂不用）、数量与间距上限 |
| `src/aiverse_server/economy/islands.py` | 岛与桥：每个街区是一座岛（每边缩进 `ISLAND_INSET` = 2，与客户端一致），相邻的岛在相对两侧的中点有一座桥；`walk_from` 给出访客从一点到另一点的最短步行（岛内直线，跨岛过桥，没有岛的地图一律直线）的距离和各段直线，`on_island` 把游荡的目标留在自己的岛上；居民不受限制 |
| `src/aiverse_server/economy/visitors.py` | 脚本访客（[虚拟设施](../aiverse-client/docs/design/facilities.md)第 7 步）：200 个访客（都是游客）从地图中央入口进来，按带随机项的偏好（内容份数、走路时间、拥挤度）每趟去 2～4 个场馆（工作空间、展示着游戏的游戏厅）并在门口付入场费再离开，广告位不是目的地；每趟有 30% 走一条大致的工作路线（工作游客，除此之外和其他游客没有区别）：先去工作空间再去展示着游戏的游戏厅；入场费见 `admissions.py`；走传送门由自己选，每走过一条线路付 1 金币的过路费，见 `admissions.py`；路过展示着内容的广告位够 4 秒的算一次展示，累计到各地块 `Crowd.views`，由赞助商付钱；`Traffic` 统计此刻经过某处的人数；随机用种子加编号的哈希，不用有状态的随机数 |
| `src/aiverse_server/economy/admissions.py` | 入场费（第 7 步第三部分）：每人的价 `FEES`（工作空间 8、游戏厅 12、商场 2、花园 1 金币），`admit` 在门口收费，`VISITOR_TOLL`（访客每走过一条线路每人付 1 金币）由 `charge` 记在线路两端传送门的地块上，`pay_admissions` 在付款日整枚硬币地按份额付给持有人（零头留到下次，渠道 `visitors`），`next_admission_payday` |
| `src/aiverse_server/economy/sponsors.py` | 赞助商（第 7 步第二部分）：三个赞助商 `default_sponsors`，每 30 秒的付款日 `pay_sponsors`（出价高的先买、预算不够按同一比例缩水、按份额分给持有人，渠道 `sponsors`），预算充值与上限，付款日 `latest_payday`、下一个付款日 `next_payday` |
| `src/aiverse_server/economy/trade.py` | 交易事件：购买报价 `PurchaseOffer` 与出售报价 `SaleOffer`，条款校验、预留与互换 |
| `src/aiverse_server/economy/contracts.py` | 合约：提议事件 `ContractProposal`，签订时预留买方全部货款，交付 `deliver` 自动付款，供货方取消、到期、出局与结束时退回余款 |
| `src/aiverse_server/residents/walking.py` | 行走：速度、所需时间、途中位置、出发、到达、中途停下、下一次到达时刻；走路只花时间 |
| `src/aiverse_server/residents/perception.py` | 感知：每轮开始时 `begin_turn` 返回范围内的居民和新的活动、当前笔记、自上次以来说过和听到的话与公告；`record_activity` 比较前后世界，记录走出发、到达、出局 |
| `src/aiverse_server/residents/memory.py` | 居民自选的记忆：写入长期或短期笔记、忘掉笔记、清除过期的短期笔记 |
| `src/aiverse_server/residents/information_offer.py` | 信息报价：事件 `InformationOffer`（摘要、内容、价格），条款校验；成交时买方付金币、内容作为长期笔记写入买方记忆，卖方保留原信息 |
| `src/aiverse_server/residents/speech.py` | 对话：听力半径、当众说或私聊 `say`（不花东西），听到的人在说话时定下 |
| `src/aiverse_server/game/actions.py` | 操作原语：玩家与 AI 改变世界的唯一入口，校验并执行；含走路、答复事件、发起购买与出售、提议合约、交付与取消合约、退出游戏、买地、买节点、建广告位、做内容、把内容放进广告位、下单与撤单、说话 |
| `src/aiverse_server/game/world_log.py` | 世界日志：`perform_logged` 执行原语并把做了什么写进 `World.world_log`（网页、`Game.act` 与 Agent 工具都经它执行原语），`record_finished_jobs` 记下设施建成和内容做好；只保留最近 200 条 |
| `src/aiverse_server/game/cues.py` | 页面演出用的提示：说话、报价算 `talk`，报价被同意、设施建成算 `happy`；不属于世界，由 `Game` 记在 Agent 居民名下 |
| `src/aiverse_server/game/lifecycle.py` | 对局生命周期：能否行动与忙碌的检查、出局（停下行走，撤掉挂单）、结束、按金币总额排名、公告、默认时长 |
| `src/aiverse_server/game/settings.py` | 读取并校验独立的游戏时间 YAML 配置 |
| `src/aiverse_server/game/economy_report.py` | 由流水账与世界算出经济指标（居民间流量占比、各渠道、价格、合约、基尼系数、收入集中度），写成 JSON 与控制台摘要 |
| `src/aiverse_server/game/config/` | 游戏时间配置：示例 `game.example.yml`；本地的 `game.yml` 被 Git 忽略 |
| `src/aiverse_server/game/clock.py` | 游戏时间到点的事：行走到达并记录活动，工作完成（记入世界日志）与节点到期，访客走完一段或待完一段，赞助商付款日，合约与挂单到期，下一个到点时刻 |
| `src/aiverse_server/game/layout.py` | 固定的初始地图、8 名居民（含初始金币，全部出生在地图中央）与还没进城的 200 个访客 |
| `src/aiverse_server/server/protocol.py` | 消息模型、客户端消息解析（错误信息不回显输入）、消息到原语与世界到视图（含谁由 Agent 驱动）的转换，`start_agent` 的设置，访客的 `visitors` 消息 `visitors_message` |
| `src/aiverse_server/server/config.py` | 读取大模型的 YAML 配置文件：`override` 覆盖模型的地址、key、模型及回复 token 上限，拒绝未知的键，错误信息不含设置的值 |
| `src/aiverse_server/server/app.py` | FastAPI 应用与 WebSocket `/ws`：共享世界、对局计时（到点的事见 `clock.py`）、每轮开始的 `begin_turn`、写入与忘掉笔记、Agent 与网页共用的执行入口 `act`/`commit`、世界变化时唤醒 Agent 的 `subscribe`、启动、更换与停止 Agent（`start_agent`、`start_llm_agent`、`stop_agent`，后者合并配置文件的覆盖项）、握手、执行原语、广播状态、定时推送访客（`run_visitors`、`push_visitors`，发件箱里单独留一份最新的） |
| `src/aiverse_server/agents/` | AI 居民：`runner.py` 事件驱动的循环与唤醒规则，`observation.py` 每轮的观察，`tools.py` 绑定居民的工具，`tool_selection.py` 按状态选择和按需展开模型可见的工具，`scripted_brain.py` 脚本机器人，`llm_brain.py` OpenAI 兼容接口的大模型大脑与设置的合并、校验，`system_prompt.py` 给模型的系统提示（规则说明） |
| `src/aiverse_server/agents/turn_logger.py` | 当前对局的按角色 JSONL 日志、独立轮号、可选汇总文件，创建/覆盖/实时写入与关闭 |
| `src/aiverse_server/agents/config/` | 大模型配置：示例 `llm.example.yml`；本地的 `llm.yml` 被 Git 忽略 |
| `src/aiverse_server/__main__.py` | 启动入口，`--config` 读模型配置、`--game-config` 读游戏配置、`--duration` 覆盖对局时长，`--agent` 指定由 Agent 驱动的居民，`--agent-log` 写每轮记录 |
| `tests/unit/` | 单元测试：世界模型、操作原语、物品栏与金币、事件、交易、合约、对局生命周期、地块、脚本访客、赞助商、入场费、交易所、行走、感知、记忆、对话、AI 居民的工具、观察与设置、`Game` 的执行入口与发件箱、协议视图、配置文件、命令行参数 |
| `tests/integration/` | 集成测试：WebSocket 接口、从页面启动与停止 Agent、Agent 循环的唤醒、模拟 HTTP 的大模型大脑、脚本机器人的快进对局 |

## 页面 AI 行动时间线与经营方针

页面选中居民后，地图下方显示其最近 30 个已完成的 AI 决策轮，含模型最终回复的短计划、每次工具调用的时间、参数与成败，以及金币总额（含托管）的实际净变化。轮间变化单独显示，不归因给下一次动作。摘要不是隐藏推理，也不作为收益原因的证据。

`start_agent` 可附带 `strategy`（最多 1000 个 Unicode 字符，留空不追加方针），传给模型系统提示；不受 YAML 的接口配置覆盖。页面按居民保存草稿，修改后需重新交给 AI。

`state.world.agentTimeline` 只在选择了 `view_as` 时出现，包含 `residentId` 与 `turns`。记录保存在 `Game.agent_turns` 中，与可选的控制台/JSONL 记录独立；停止、更换 Agent 和重连不清除，重启服务端清除。页面投影不包含完整观察和异常原文；文本与调用数有上限，模型回显的当前 API key 会替换为 `***`。没有居民归属时，任何页面仍可选中任意居民查看。

`agents/tools.py` 的 `ResourceBalance` 和 `resource_balance` 测量动作前后总额，`runner.py` 生成带首尾资源的 `TurnRecord`，`server/protocol.py` 转为有界的 `AgentTurnView`，`server/app.py` 按观看者发送。

## 角色日志

用正常启动命令即可自动记录，无需额外开关：

```sh
uv run aiverse-server --agent r1=llm --agent r2=llm
```

默认位置为服务端项目的 `logs/`，与启动时的工作目录无关。按居民 id 分别保存 `r1.jsonl`、`r2.jsonl`、`r3.jsonl`、`r4.jsonl`；未启动 AI 的居民文件为空。每次启动服务端就是新一局，因此会覆盖这些文件，同局内停止、更换 AI 继续追加；除上一局的经济报告 `summary.json`（见下文“经济报告”）外，不删除目录里的其他文件。日志文件已被 Git 忽略。

用 `--log-dir /绝对路径/日志目录` 改位置（相对路径相对启动目录），目录不存在时自动创建。旧的 `--agent-log FILE` 仍可额外保存所有角色的汇总记录，现在也改为每局覆盖，不能与任一角色文件同名。

每行是一个 AI 决策轮的 JSON，写完立即 flush，可以边运行边读取。字段如下：

| 字段 | 内容 |
| --- | --- |
| `resident_id`、`resident_name`、`round` | 居民 id、名字、该居民本局第几轮（停止/更换 AI 不重置） |
| `model`、`strategy` | 模型名和经营方针；脚本机器人为 null |
| `started_at`、`finished_at` | UTC ISO 8601 现实起止时间，含时区 |
| `time`、`ended_at` | 自开局起的游戏秒数 |
| `observation` | 本轮完整观察，含 `long_term_notes`、`short_term_notes`、收到的发言与交易，以及服务端的公告 `notices` |
| `calls` | 按调用顺序记录动作名、参数、游戏时间 `time`、现实 UTC `timestamp`、成败和状态、金币净变化 |
| `memory_after` | 本轮结束时存储的长短期笔记等记忆，含写入和过期时间 |
| `outputs`、`remark` | 每次模型响应的文本输出（不含隐藏推理）、最终计划摘要；脚本只记录返回的摘要 |
| `completions` | 每次模型请求的 `finish_reason`（如 `length` 表示输出用尽被截断）和服务商返回的 AI API token 用量（不是游戏里的算力）：读入 `prompt_tokens`（其中命中缓存的 `cached_tokens`）、写出 `completion_tokens`（其中用于推理的 `reasoning_tokens`）、合计 `total_tokens`，服务商未给出的字段省略；一轮的消耗是各条相加；只含得到回答的请求，失败的请求只在 `error` 里；脚本为空 |
| `before`、`after` | 本轮首尾金币总额（含托管） |
| `error`、`cancelled` | 失败详情与是否在思考时被停止；已执行动作与轮后记忆仍保留 |

模型返回错误、被停止或更换时的未完成轮次也写文件；取消轮次不进入页面的已完成时间线。本局已使用的 API key 在 JSON 转义前从文本、参数、笔记和输出中脱敏；非法 JSON 参数不保存原文。日志包含局内私聊与记忆，只保存在本机。

本功能记录 AI 决策轮，手动操作不是独立日志行，但其造成的状态变化会出现在后续观察和资源变化中。直接嵌入 `create_app` 的程序仍可自行传 `record` 回调，默认文件记录由命令行入口创建。

## 经济报告

世界里有一本金币流水账（`core/ledger.py`）：金币每次真实转手都记一条，包括游戏时间、付款方、收款方、金额、渠道，买物品时还记物品和数量。系统一方记为 `system`。渠道包括 `offer`（报价成交）、`contract_payment`、`contract_forfeit`（押金赔给买方）、`information`、`exchange`（交易所成交）、`land`（向镇上买地块）、`sponsors`（赞助商按展示付给广告位，按份额分给地块的持有人，每人一条，见 `economy/sponsors.py`）、`visitors`（走工作路线的游客付给工作空间和游戏厅的入场费，以及访客走过线路时付给线路两端传送门的过路费，在付款日按份额分给持有人，见 `economy/admissions.py`）、`shares`（转让地块份额的价款，见 `economy/shares.py`）、`toll`（传送的过路费，付给两端地块的持有人，见 `economy/portals.py`）、`garden_reward`（花园建成时镇上付给地主的一次性奖励）、`garden_bonus`（同街区有花园时镇上在每笔赞助商收入和入场费之外多付的十分之一，按份额分给持有人，见 `economy/gardens.py`）。预留和退回（报价、合约托管、挂单）不记。流水账最多保留 20000 条，超出时丢掉最早的并计数。

对局结束时，命令行入口在日志目录写 `summary.json`，并在控制台打印几行摘要（`game/economy_report.py`）。主要字段：

| 字段 | 内容 |
| --- | --- |
| `between_residents`、`from_system`、`to_system` | 居民之间转手的金币、系统付给居民的、居民付给系统的 |
| `resident_share` | `between_residents ÷ (between_residents + from_system)`，衡量钱主要来自别人还是来自系统；没有金币流动时为 null |
| `channels` | 各渠道的笔数与金额 |
| `prices` | 渠道 → 物品 → 成交笔数、数量、按数量加权的均价、最低与最高单价、单价的标准差 |
| `contracts` | 签订数、交付次数、押金赔付次数与金额、按原因统计的已结束合约（世界只保留最近 100 份） |
| `coin_gini`、`mean_income_concentration` | 最终金币总额（排名口径）的基尼系数；每人收入来源按渠道的赫芬达尔指数的平均，越高越专于一门营生 |
| `residents` | 每人的金币总额、按渠道的收入与支出、收入集中度 |
| `transfers_dropped` | 流水账丢掉的条数；大于 0 时金币数字不完整 |

服务端启动时删掉上一局的 `summary.json`，所以目录里的报告一定是本局的；对局没到时间就停掉服务端时不写报告。嵌入 `create_app` 的程序可以传 `on_end` 回调，在对局结束时拿到最终的世界。设计见客户端 `docs/design/economy-metrics.md`。
