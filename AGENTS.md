# AI 开发规则

本项目是 AIverse 的 Python 服务端。通用规则、开发与审查 Skill、项目状态、决策记录都在同级的客户端仓库中：先读 `../aiverse-client/AGENTS.md` 和 `../aiverse-client/docs/status.md`，并按其中的流程工作。

本目录的补充约定：

- 用中文沟通与写说明；代码遵循 Python 惯例，类型标注完整，通过 mypy strict。
- 文件命名与放置遵循客户端 `docs/conventions.md`：模块 snake_case、按领域放进子包，测试放在 `tests/<子包>/test_<模块>.py`，文件名全局唯一。
- 测试的分层与写法遵循客户端 `docs/testing.md`：规则在单元层测全，`TestClient` 集成测试只证明各部分接得上；每个测试按准备、执行、断言三段写。
- 游戏状态只在服务端计算；客户端发来的消息都是不可信输入，用 pydantic 校验。
- 协议改动同时修改客户端 `packages/protocol`，见本目录 `README.md` 的“协议”一节。
- 交付前运行 `make verify`；涉及界面时，同时按客户端文档在浏览器中实测。
- 本目录是独立的 Git 仓库，服务端改动在这里单独提交；提交条件和说明格式见客户端 `docs/development.md` 的“提交约定”。
