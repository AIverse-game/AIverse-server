# 通用 AI 开发流程

适用于 `aiverse-client` 与 `aiverse-server` 两个仓库。本文与同目录的其他规则、`.agents/skills/` 中的 Skill 在两个仓库各有一份相同的副本，修改方法见 [规则如何同步](#规则如何同步)。下文的“客户端”“服务端”分别指这两个仓库。

## 上下文如何组织

| 层次 | 位置 | 内容 |
| --- | --- | --- |
| 长期约定 | 各仓库根目录 `AGENTS.md` | 稳定、简短、可执行的规则，以及本仓库的检查命令 |
| 工具入口 | 各仓库根目录 `CLAUDE.md` | 引导 Claude Code 读取 `AGENTS.md` |
| 工作规则 | `.agents/rules/`，两个仓库相同 | 开发流程（本文）、`conventions.md` 命名与目录、`testing.md` 测试 |
| 开发工作方法 | `.agents/skills/implement-change/SKILL.md`，两个仓库相同 | 实现、验证与审查交接 |
| 审查工作方法 | `.agents/skills/review-change/SKILL.md`，两个仓库相同 | 默认只读的问题发现与修复复核 |
| 当前状态 | 客户端 `docs/status.md` | 已完成、实际命令、下一步与阻塞 |
| 文件导航 | 客户端 `docs/code-map.md`；服务端 `README.md` 的“代码结构” | 真实存在的代码和文档职责 |
| 功能设计 | 客户端 `docs/design/` | 每个功能的问题背景、设计思路、争议与决定、暂未实现 |
| 单次任务 | 客户端 `changes/` | 目标、范围、验收与实际证据 |
| 重要决定 | 客户端 `docs/decisions/` | 选项、理由、影响与替代关系 |

规则保持简短；任务细节不塞进 `AGENTS.md`。同一事实指定一个主要位置，其他文件引用，避免 Codex 与 Claude Code 各自维护一套上下文。`.agents/` 放 Agent 怎么工作，`docs/` 放项目本身的知识。

## 规则如何同步

两个仓库的 `.agents/rules/` 与 `.agents/skills/` 逐字相同，各自都能独立使用。改其中任一文件时，同一改动在两个仓库各提交一次，提交说明相同；只适用于一个仓库的约定写进该仓库的 `AGENTS.md`，不写进这些共用文件。共用文件只链接彼此和根目录 `AGENTS.md`，提到某个仓库独有的文件时写明是哪个仓库、用行内代码写路径。两个仓库位于同级目录时，客户端的 `python3 scripts/check_repo.py` 会比较这些文件，不一致时报错。

## 一次任务的闭环

1. **理解**：读取规则与状态，检查工作区和相关实现，明确目标与完成条件。
2. **界定**：小修直接推进；较大任务使用客户端的变更模板 `changes/TEMPLATE.md`，列出范围和关键验收。
3. **决定**：新功能新建设计文档，调整已有功能时修改原设计文档，见客户端 `docs/design/README.md`；存在重要选型或兼容性取舍时，使用客户端的决策模板 `docs/decisions/TEMPLATE.md` 记录依据。
4. **实现**：按小步推进，遵循现有结构，不扩充未请求的需求。
5. **验证**：执行与风险相称的格式、类型、测试或构建检查，修复本次引入的问题。
6. **审查**：阅读最终差异，确认行为满足目标，检查无关修改和遗漏。
7. **交接**：记录实际证据，按最终实现更新设计文档（含暂未实现的功能）、其他必要文档与当前状态，总结结果和限制。
8. **提交**：验证通过、审查无误后，按 [提交约定](#提交约定) 提交本次改动。

默认在用户授权范围内持续推进，不把每一步都变成确认环节。只有缺少必要信息或存在影响目标的歧义时，集中澄清；等待期间继续可独立完成的工作。

## Skill 如何调用

Codex 支持发现项目 `.agents/skills/` 中的 Skill，按描述选择并加载。新文件未出现在当前会话的技能列表时，直接读取对应 `SKILL.md`，或在新会话中检查发现情况。参考 [官方 Skill 实践](https://developers.openai.com/blog/skills-agents-sdk)。

Claude Code 从 `CLAUDE.md` 进入共同规则，再按任务显式读取同一份 `SKILL.md`；本仓库未配置 Claude Code 的原生技能注册或快捷命令，也不依赖它自动扫描 `.agents/skills/`。

可用的任务示例：

- 开发：“读取 `.agents/skills/implement-change/SKILL.md`，实现［需求］，验收条件为［结果］，完成验证、独立审查和提交。”
- 审查：“读取 `.agents/skills/review-change/SKILL.md`，只审查［文件或差异范围］，基线为［引用或快照］，需求为［预期行为］，不要改文件。”

Skill 提供工作方法，不会自行启动 Agent。本仓库的调用约定由主 Agent 在当前开发会话执行，尚无 CI 或后台任务触发。

## 独立审查调用约定

代码、可执行脚本或构建配置改动完成并验证后，主 Agent 应调用一个独立审查子 Agent，使用 [review-change](../skills/review-change/SKILL.md)。这项委派由根目录规则明确要求；纯文档小修默认自查，较大的规则或 Skill 改动按风险决定。只要求审查时不进入开发流程，不递归委派。

给审查 Agent 提供最少必要上下文：

- 需求、范围和验收条件；相关变更说明路径。
- 本次改动文件、比较基线，以及任务开始前已有修改的归属。
- 实际验证命令、结果和无法验证的部分。
- 只读职责和报告要求；不让它代替开发 Agent 更新文件。

尽量使用独立上下文，提供原始需求和代码入口，不预置预期审查结论。审查运行期间，开发 Agent 不再修改被审查文件。尚无首次提交或涉及未跟踪文件时，提供明确文件清单并直接审阅；存在重叠修改时，使用开始前快照或补丁区分归属，不暂存、清理或回滚用户文件来制造基线。

审查报告交回后，开发 Agent 判断并修复确认的问题，执行受影响检查，再把修复后的范围交回复核。尚有问题时继续处理或记录具体阻塞。报告中的待确认疑点保留为疑点，不自动当成缺陷。

环境没有子 Agent 能力时，主 Agent 加载同一 Skill 自查，并在交付中明确“独立审查未执行”及原因。自查、独立审查、自动化检查分别报告，不互相替代。

## 验证如何选择

| 改动类型 | 验证重点 |
| --- | --- |
| 文档和配置 | 链接、格式、语法，以及工具实际能否读取配置 |
| 缺陷修复 | 复现原问题，确认修复，按需保留回归用例 |
| 功能或接口 | 可观察行为、错误路径、输入边界与兼容性 |
| 数据或状态变更 | 迁移、失败恢复、已有数据与不变量 |
| 界面 | 实际用户流程、交互反馈及适用的可访问性检查 |
| 性能改动 | 可重复的基线和前后测量 |

客户端：`make check` 只检查仓库骨架；`npm run verify` 依次执行类型检查、ESLint（含分层规则）、Vitest 和生产构建，`make verify` 两者都跑。CI 复用同一组命令。服务端：`make verify` 运行 ruff、mypy、pytest，CI 不包含这一项。各仓库的命令以其 `AGENTS.md` 为准。不要添加占位命令、强制覆盖率数字或无效测试来制造通过结果。测试放在哪一层、怎么写见 [测试约定](testing.md)。

界面改动除自动检查外，应先在服务端仓库启动服务端（`uv run aiverse-server`），再在客户端仓库运行 `npm run dev` 或 `npm run build && npm run preview`，在浏览器实际操作一次。

## Git 与多会话协作

开始前查看 `git status`，结束前审查差异。当前文件尚未跟踪时，`git diff` 不会显示其内容，应直接阅读和检查文件。

不自动安装提交钩子，不改变全局 Git 配置。

### 提交约定

开发任务同时满足以下条件后，主 Agent 直接提交，不必等待用户另行要求：

- 与改动相关的检查已通过。
- 独立审查（或工具不支持时的自查）中确认的问题都已修复并复核；待确认疑点在交接中写明。
- 状态、变更说明等交接文档已按需更新，与代码一同提交。

任一条件不满足时不提交，在交接中说明原因。推送、发布仍只在用户要求时进行。

提交范围只含本次任务的改动：按路径逐个暂存，不使用 `git add -A` 或 `git add .`；任务开始前已有的修改不纳入。同一文件中已有修改与本次修改无法分开时，不提交该文件，说明情况交给用户处理。以一次可解释的改动为单位提交；改动涉及两个仓库时（如协议或共用规则），在两个仓库分别提交，并在说明中注明对应仓库的改动。不修改已有提交（不 amend、rebase 或强制推送），不使用 `--no-verify` 跳过检查。

提交说明遵循 Google 的 [CL 描述规范](https://google.github.io/eng-practices/review/developer/cl-descriptions.html)，用英文书写，不加 `feat:` 一类的类型前缀：

- **首行**：简短、具体地概括这次改动做了什么，使用祈使句（如 “Add …”“Fix …”），独立成句；只读首行也能知道改了什么。首行后空一行。不写 “Fix bug”“Update code” 这类空泛描述。
- **正文**：说明解决什么问题、为什么这样做；有取舍或已知不足时写明。按需附上背景，例如相关变更说明、决策记录路径或实际验证结果。小改动正文可以简短，但要说明原因。
- **提交前复核**：审查修复后差异可能变化，提交前对照最终差异确认说明仍然准确。
- **不署名 AI 工具**：不加 `Co-Authored-By` 等把 Claude、Codex 或其他 AI 工具列为作者或协作者的尾注，也不写“由某工具生成”一类的说明。

示例：

```text
Show a retry button on the map panel after disconnect

When the WebSocket closed, the map panel stayed in its loading state and
the only way to recover was reloading the page. Show the error and a
retry button instead; retrying reuses the existing reconnect logic, so
no new connection state is added.

Change notes: changes/0006-reconnect.md
Tested: npm run verify passes; stopped the server in the browser session
and confirmed retry recovers.
```

### 多会话协作

跨会话通过状态、变更说明和决策记录交接。确需并行工作时，先明确文件所有权与接口，避免共享工作区内覆盖修改；分支或 worktree 根据实际需要选择。

## 技术栈与分层

应用技术栈与分层已在客户端的决策 0001 与 0002（`docs/decisions/`）确定，结构说明见客户端 `docs/architecture.md`。新增依赖、改变层间依赖方向或引入服务端、数据库时，先补充或替代决策记录。
