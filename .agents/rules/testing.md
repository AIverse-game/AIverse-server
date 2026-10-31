# 测试约定

适用于 `aiverse-client` 与 `aiverse-server` 两个仓库，两边的 `.agents/rules/` 各有一份相同的副本。规则从现有测试整理而来，规定两件事：一项行为该在哪一层测（测试分层），以及一个测试内部怎么写（准备、执行、断言）。测试文件叫什么、放在哪里见 [文件命名与目录约定](conventions.md)；要跑哪些检查见 [开发流程](development.md#验证如何选择)。

新写或修改的测试遵循本文；不为符合本文批量改写现有测试。

## 测试分层

| 层 | 测什么 | 怎么测 | 示例 |
| --- | --- | --- | --- |
| 单元 | 一个模块的规则与计算：原语成败、数值、不变量、边界与拒绝路径 | 直接调用函数或原语，不经网络、不等真实时间 | `tests/economy/test_market.py` 对 `perform(WORLD, SellTokens(...))` 断言；`state/market.test.ts` 对 `marketQuote` 断言 |
| 集成 | 多个模块经真实入口协作：消息进出、广播与按观看者过滤、定时器、Agent 循环与大脑 | 服务端用 `TestClient` 连上 `create_app` 的 WebSocket，或运行真实的 Agent 循环；只替换下文列出的边界 | `tests/server/test_app.py`、`tests/agents/test_agents.py` |
| 端到端 | 用户在浏览器里的完整流程，客户端与服务端一起运行 | 目前人工：启动服务端，在浏览器实际操作一次，结果写进变更说明的验证证据 | 见 [开发流程](development.md#验证如何选择) |

- **放在能证明行为的最低一层**：游戏规则与数值在单元层测全，含边界和拒绝路径；集成层只证明各部分接得上，不重复单元层已经测过的数值组合。
- **数量随层递减**：单元测试最多，集成测试覆盖每条入口和跨模块流程，端到端只走关键用户流程。不设覆盖率数字。
- **协议改动两端都测**：服务端在集成层测消息的收发，客户端在 `packages/protocol` 测校验；两端各自实现同一计算时，用同一组数字断言，并注明来源（如 `state/market.test.ts` 与 `tests/economy/test_market.py` 都断言卖 30 token 得 524 金币）。
- **缺陷修复**：在能复现缺陷的最低一层加回归测试。
- **自动化端到端**：目前没有。引入 Playwright 等工具属于新增依赖，先写决策记录。

## 一个测试的结构

每个测试按 **准备（Arrange）→ 执行（Act）→ 断言（Assert）** 三段写，也就是 Given / When / Then。

```python
def test_one_seller_lowers_the_price_for_everyone() -> None:
    after_a_sale = perform(WORLD, SellTokens("a", 100))

    alone = perform(WORLD, SellTokens("b", 50))
    after = perform(after_a_sale, SellTokens("b", 50))

    assert coins(after, "b") < coins(alone, "b")
```

- **一个测试一个行为**：名字是一句描述行为的英文句子，Python 写成 `test_<行为>`，TypeScript 写在 `it('<行为>')` 里，读名字就知道测的是什么、预期什么。同一行为的几个方面可以一起断言；名字要用 `and` 连接两个互不相干的结论时，拆成两个测试。
- **三段按顺序，不交替**：不写“执行→断言→再执行→再断言”。行为本身是一段过程时（如卖出、等待、再卖出），整段过程算作执行。中间只允许两种断言：为了收窄类型的（`assert market is not None`），以及确认前提成立的；其余断言都放到最后。
- **用空行分段**：测试超过几行时，三段之间各空一行。执行与断言能写进一行时可以合并，例如 `assert coins(perform(WORLD, SellTokens("a", 30)), "a") == 624`。
- **准备**：几个测试共用的场景，放在模块顶部的不可变常量和工厂函数里（如 `WORLD`、`resident(...)`），各测试用 `dataclasses.replace` 或对象展开派生出自己的场景，不修改共用对象。只有一个测试用到的数据写在测试里。pytest fixture 用于需要启动或清理的对象，例如 `TestClient` 和假时钟；不需要清理的数据不用 fixture。
- **执行**：调用公开入口，也就是原语、`Game` 的方法、WebSocket 消息或模块导出的函数，不调私有函数。
- **断言**：断言可观察的结果，也就是返回的世界、发出的消息、视图和异常，不断言内部的中间状态。期望值是推导出来的时，写出推导过程或引用常量，必要时同时断言字面值，让公式出错时能被发现（见 `test_selling_pays_the_price_summed_over_the_sale`）。拒绝路径断言异常类型和消息，Python 用 `pytest.raises(..., match=...)`。
- **参数化边界**：同一行为在多组输入下成立时，Python 用 `@pytest.mark.parametrize`，TypeScript 用 `it.each`，不复制多个测试。
- **只替换边界**：可以替换的只有外部 HTTP（`httpx.MockTransport`）、时钟（`FakeClock`）、浏览器 WebSocket（`FakeSocket`），以及为缩短等待而用 `monkeypatch` 调小的时长常量。不 mock 本项目自己的模块；要替换时说明这个模块难以直接使用，先考虑改进它的接口。
- **不写只复述实现的测试**：测试说明行为应当如何，不逐行镜像实现代码。
