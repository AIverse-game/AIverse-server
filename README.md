# AIverse 服务端

AIverse 的权威游戏服务端。游戏状态只在这里计算，网页客户端（同级目录 `aiverse-client`）只接收并渲染。设计、规则与协作约定都在客户端仓库的文档中，先读那边的 `AGENTS.md` 与 `docs/architecture.md`。

当前功能：生成一张固定布局的正方形地图（边长 100）和 4 名居民，客户端经 WebSocket 读取；通过操作原语改变世界，目前只有“移动到坐标”（`move_to`）。所有连接共享同一个世界，世界变化后推送给每个已握手的连接。

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
make verify                           # ruff、ruff format --check、mypy strict、pytest
```

网页端开发与预览服务器默认把 `/ws` 代理到 `127.0.0.1:8000`。服务端换端口时，客户端用同一端口启动：`AIVERSE_SERVER_PORT=9000 npm run dev`。

尚无身份认证、Origin 检查和消息大小限制，任何连接都能操作任何居民，不要监听公网地址。每个连接只保留最新一份待发的 `state`，对自己操作的回复最多积压 256 条，超过就以 1008 关闭。

## 协议

WebSocket 地址为 `/ws`，消息是 JSON 文本帧，键名用 camelCase。当前协议版本为 3。

| 方向 | 消息 | 说明 |
| --- | --- | --- |
| 客户端 → 服务端 | `{"type": "hello", "protocolVersion": 3}` | 连接后发送；多余字段忽略 |
| 客户端 → 服务端 | `{"type": "action", "action": {...}}` | 操作原语，`hello` 成功后才能发送，见下 |
| 服务端 → 客户端 | `{"type": "welcome", "protocolVersion": 3}` | 回应 `hello` |
| 服务端 → 客户端 | `{"type": "state", "world": {...}}` | 紧跟 `welcome`；世界变化后也推送给每个已握手的连接。内容见下 |
| 服务端 → 客户端 | `{"type": "action_result", "ok": true}` | 回应发送者的 `action`。被拒绝时 `ok` 为 `false`，附 `code`（`unknown_resident`、`out_of_bounds`）和 `message` |
| 服务端 → 客户端 | `{"type": "error", "code": "...", "message": "..."}` | `bad_message` 或 `protocol_mismatch`；`message` 不超过 200 字，连接保持可用 |

目前唯一的原语：

```json
{ "type": "move_to", "residentId": "r1", "target": { "x": 30.0, "y": 40.5 } }
```

坐标必须是有限的数字（不接受布尔值、字符串、NaN 和无穷），否则回复 `bad_message`。居民不存在或目标在地图外时回复被拒绝的 `action_result`，世界不变。移动即时到达，速度与回合限制尚未设计。

`world` 的结构：

```json
{
  "map": { "size": 100.0 },
  "residents": [
    { "id": "r1", "name": "阿青", "color": "#e4572e", "position": { "x": 20.0, "y": 25.0 } }
  ]
}
```

协议在客户端 `packages/protocol` 中还有一份 TypeScript 定义。改动时两边同时修改并更新测试；不兼容的改动同时提升两边的 `PROTOCOL_VERSION`。

## 代码结构

| 文件 | 用途 |
| --- | --- |
| `src/aiverse_server/world.py` | 坐标、居民、地图与世界模型，越界与重复 id 校验 |
| `src/aiverse_server/layout.py` | 固定的初始地图与居民 |
| `src/aiverse_server/actions.py` | 操作原语：改变世界的唯一入口，校验并执行 |
| `src/aiverse_server/protocol.py` | 消息模型、客户端消息解析、消息到原语与世界到视图的转换 |
| `src/aiverse_server/app.py` | FastAPI 应用与 WebSocket `/ws`：共享世界、握手、执行原语、广播状态 |
| `src/aiverse_server/__main__.py` | 启动入口 |
| `tests/` | 世界模型、操作原语与 WebSocket 接口测试 |
