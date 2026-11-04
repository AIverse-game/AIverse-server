# 测试约定

规则从现有测试整理而来，规定两件事：一项行为该在哪一层测（测试分层），以及一个测试内部怎么写（准备、执行、断言）。测试文件叫什么、放在哪里见 [文件命名与目录约定](conventions.md)；要跑哪些检查见 [开发流程](development.md#验证如何选择)。

新写或修改的测试遵循本文；不为符合本文批量改写现有测试。

## 测试分层

| 层 | 测什么 | 怎么测 | 目录与示例 |
| --- | --- | --- | --- |
| 单元 | 一个模块的规则与计算：原语成败、数值、不变量、边界与拒绝路径 | 直接调用函数、原语或 `Game` 的方法，不经网络、不等真实时间 | `tests/unit/`；`tests/unit/economy/test_market.py` 对 `perform(WORLD, SellTokens(...))` 断言 |
| 集成 | 多个模块经真实入口协作：消息进出、广播与按观看者过滤、定时器、Agent 循环与大脑 | 用 `TestClient` 连上 `create_app` 的 WebSocket，或运行真实的 Agent 循环；只替换下文列出的边界 | `tests/integration/`；`tests/integration/server/test_websocket.py`、`tests/integration/agents/test_agent_loop.py` |
| 端到端 | 用户在浏览器里的完整流程，与网页客户端一起运行 | 目前人工：启动服务端和网页客户端，在浏览器实际操作一次 | 没有目录；见 [开发流程](development.md#验证如何选择) |

- **放在能证明行为的最低一层**：游戏规则与数值在单元层测全，含边界和拒绝路径；集成层只证明各部分接得上，不重复单元层已经测过的数值组合。
- **按怎么测分层，不按测哪个模块**：一个测试经 WebSocket、运行 Agent 循环或调用大脑（`take_turn`、`aclose` 等大脑的方法），就属于集成层；否则属于单元层。所以同一模块的测试可以分在两层，`server/app.py` 的 `Game` 与发件箱在 `tests/unit/server/test_app.py`，经它的 WebSocket 在 `tests/integration/server/test_websocket.py`。只跑单元层用 `uv run pytest tests/unit`。
- **数量随层递减**：单元测试最多，集成测试覆盖每条入口和跨模块流程，端到端只走关键用户流程。不设覆盖率数字。
- **协议改动**：在集成层经 WebSocket 测新消息的收发与拒绝。网页客户端会按服务端规则推算一些数值（如市场价），这类规则的单元测试断言具体数字，供客户端对照，例如 `tests/unit/economy/test_market.py` 断言卖 30 能量得 524 金币。
- **缺陷修复**：在能复现缺陷的最低一层加回归测试。

## 一个测试的结构

每个测试按 **准备（Arrange）→ 执行（Act）→ 断言（Assert）** 三段写，也就是 Given / When / Then。

```python
def test_one_seller_lowers_the_price_for_everyone() -> None:
    after_a_sale = perform(WORLD, SellTokens("a", 100))

    alone = perform(WORLD, SellTokens("b", 50))
    after = perform(after_a_sale, SellTokens("b", 50))

    assert coins(after, "b") < coins(alone, "b")
```

- **一个测试一个行为**：`test_<行为>` 是一句描述行为的英文句子，读名字就知道测的是什么、预期什么。同一行为的几个方面可以一起断言；名字要用 `and` 连接两个互不相干的结论时，拆成两个测试。
- **三段按顺序，不交替**：不写“执行→断言→再执行→再断言”。行为本身是一段过程时（如卖出、等待、再卖出），整段过程算作执行。中间只允许两种断言：为了收窄类型的（`assert market is not None`），以及确认前提成立的（如上一步的操作确实被接受：`assert ws.receive_json()["ok"] is True`）；其余断言都放到最后。
- **什么算执行**：改变状态的调用才算执行，即执行原语、调用会改变世界的函数、发 WebSocket 帧、拨动时钟、等待。读取结果不算：调用只读的查询函数（如 `item_count`、`world_view`）、收 WebSocket 帧，都是在观察上一步执行的结果，可以写在断言里。一次执行要观察几个时刻时，把每个时刻的结果存进变量（如 `walking`、`arrived`），执行完再一起断言。
- **用空行分段**：测试超过几行时，三段之间各空一行。执行与断言能写进一行时可以合并，例如 `assert coins(perform(WORLD, SellTokens("a", 30)), "a") == 624`。
- **准备**：几个测试共用的场景，放在模块顶部的不可变常量和工厂函数里（如 `WORLD`、`resident(...)`），各测试用 `dataclasses.replace` 派生出自己的场景，不修改共用对象。只有一个测试用到的数据写在测试里。pytest fixture 用于需要启动或清理的对象，例如 `TestClient` 和假时钟；不需要清理的数据不用 fixture。
- **执行**：调用公开入口，也就是原语、`Game` 的方法、WebSocket 消息或模块导出的函数，不调私有函数。
- **断言**：断言可观察的结果，也就是返回的世界、发出的消息、视图和异常，不断言内部的中间状态。期望值是推导出来的时，写出推导过程或引用常量，必要时同时断言字面值，让公式出错时能被发现（见 `test_selling_pays_the_price_summed_over_the_sale`）。拒绝路径用 `pytest.raises` 断言异常类型，再断言原因：本项目带 `code` 的异常（`ActionRejected`、`InventoryError` 等，`code` 也是发给客户端的错误码）断言 `raised.value.code`；其他异常用 `match=` 断言消息。
- **参数化边界**：同一行为在多组输入下成立时用 `@pytest.mark.parametrize`，不复制多个测试。
- **只替换边界**：可以替换的只有外部 HTTP（`httpx.MockTransport`）、时钟（`FakeClock`），以及为缩短等待而用 `monkeypatch` 调小的时长常量。Agent 的大脑是决策者的接口，测试可以传入自己写的大脑（如只记录观察的 `Recorder`），这不算替换本项目的模块。除此之外不 mock 本项目自己的模块；要替换时说明这个模块难以直接使用，先考虑改进它的接口。
- **不写只复述实现的测试**：测试说明行为应当如何，不逐行镜像实现代码。
