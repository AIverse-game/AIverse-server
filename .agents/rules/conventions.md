# 文件命名与目录约定

规则从现有代码整理而来；新增、移动或重命名文件时遵循本文。子包的职责与依赖方向见 `README.md` 的“代码结构”，本文只管文件叫什么、放在哪里。

## 通用原则

- **名字说明内容是什么**：用领域名词或名词短语，只看路径就能说出里面主要是什么，例如 `residents/walking.py`、`agents/scripted_brain.py`。以下几种名字不合格：
  - **只写用了什么技术**：`llm.py`、`http.py` 只说明依赖，没说明是什么东西；应写成 `llm_brain.py`。
  - **只有形容词或限定词**：`scripted.py` 缺少被修饰的名词；应写成 `scripted_brain.py`。
  - **和领域概念撞名**：在游戏里“rules”指游戏规则，用它命名给模型的提示就会误导；应按实际内容写成 `system_prompt.py`。
  - **泛名**：`utils`、`helpers`、`common`、`misc`、`manager`、`stuff`；确实是共用的小函数，按它们服务的主题命名。
- **与主要导出对应**：文件以一个类或函数为主时，文件名取它名字的 snake_case，例如 `ScriptedBrain` 在 `scripted_brain.py`、`system_prompt()` 在 `system_prompt.py`。自检方法：不打开文件，能否从路径说出里面主要的类或函数。
- **不带版本和状态**：不用 `new`、`old`、`v2`、`temp`、`final`、`backup` 等后缀，历史交给 Git。
- **字符**：只用 ASCII 小写字母、数字、`-`、`_` 和 `.`，不用空格、中文和其他符号。大写只用于约定文件名。
- **跟随邻居**：本文没覆盖的情况，按同一目录现有文件的写法；同一目录里不混用两种风格。
- **一个文件一个职责**：一个文件放一个类或一组紧密相关的函数。文件明显承担了两件事，或长到难以通读（经验上超过约 500 行）时，考虑按职责拆分，不按行数机械切割。

## 命名规则

### Python

| 内容 | 规则 | 示例 |
| --- | --- | --- |
| 包与模块 | snake_case（PEP 8），领域名词；以一个类或函数为主时取它的名字 | `residents/perception.py`、`agents/llm_brain.py` |
| 子包 | 按领域划分，一个单词；职责见 `README.md` 的“代码结构” | `economy/`、`residents/` |
| 启动入口 | `__main__.py` | `aiverse_server/__main__.py` |
| 测试 | 镜像源码子包：`tests/<子包>/test_<模块>.py`；覆盖多个模块的一项功能时用 `test_<功能>.py` | `tests/economy/test_market.py`、`tests/server/test_agent_control.py` |

测试目录不加 `__init__.py`，沿用 pytest 默认导入方式，所以**测试文件名在整个 `tests/` 中必须唯一**；重名时在名字里加上子包或功能区分。

### 配置

| 内容 | 规则 | 示例 |
| --- | --- | --- |
| 本项目的配置文件 | 放在所属子包的 `config/` 目录，见下文“配置文件放在哪里”；YAML 用 `.yml`，按内容命名；可提交的示例命名为 `<名称>.example.<扩展名>`，本地实际文件被 `.gitignore` 忽略 | `agents/config/llm.example.yml` 与被忽略的 `agents/config/llm.yml` |
| 工具规定名称的文件 | 按工具要求，不改名 | `pyproject.toml`、`uv.lock`、`Makefile` |

### 文档

| 内容 | 规则 | 示例 |
| --- | --- | --- |
| 约定文件 | 全大写，名称固定 | `README.md`、`AGENTS.md`、`CLAUDE.md`、`SKILL.md` |
| Agent 规则 | `.agents/rules/<主题>.md`，kebab-case | `.agents/rules/testing.md` |
| Skill | `.agents/skills/<动词-名词>/SKILL.md` | `implement-change/SKILL.md` |

## 目录结构

### 顶层

| 位置 | 内容 |
| --- | --- |
| `src/aiverse_server/` | 源码，按领域分子包 |
| `tests/` | 测试，按同样的子包分组 |
| `.agents/rules/` | 给 Agent 的工作规则：开发流程、命名与目录、测试 |
| `.agents/skills/` | Codex 与 Claude Code 共用的 Skill |

### 新文件放在哪里

1. **先找已有子包**：按领域放进 `core/`、`economy/`、`residents/`、`game/`、`server/`、`agents/`；`core/` 只放世界模型与共用机制，且只依赖 `core/`。归属以 `README.md` 的“代码结构”为准。
2. **新建子包要有理由**：已有子包的职责明显不符，且新主题会有不止一个文件时才新建，不为单个文件建子包。新建子包时同时更新 `README.md` 的“代码结构”；改变依赖方向时先记录决策。
3. **测试跟着源码**：测试放进与源码同名的 `tests/<子包>/`。
4. **不进仓库的东西**：构建产物、虚拟环境、缓存、日志、本地配置与密钥，由 `.gitignore` 排除；新增此类产物时同步补忽略规则。

### 配置文件放在哪里

- **跟着所属子包**：某个子包的配置文件放在该子包下的 `config/` 目录，例如大模型的配置属于 `agents/`，放在 `src/aiverse_server/agents/config/`。其他子包需要配置时同样新建 `<子包>/config/`，不放在仓库根目录，也不集中到一处。
- **`config/` 只放配置文件**：示例和本地实际文件，不放 Python 代码，不加 `__init__.py`；读取与校验的代码按职责放在 `.py` 模块里，例如 `server/config.py`。
- **默认路径按包定位**：启动时的默认路径相对源码包确定（`Path(__file__)`），与从哪个目录启动无关；命令行参数可以另指路径。
- **本地文件不外流**：`.gitignore` 的 `src/aiverse_server/*/config/*.yml` 忽略本地文件、保留 `*.example.yml`；`pyproject.toml` 的 `source-exclude` 把这些 YAML 排除在构建产物外。新增其他扩展名的配置时，两处一起补上。

## 新增、移动与重命名

- 新增、移动或重命名文件时，在同一次改动中更新 `README.md` 的“代码结构”，以及引用该路径的说明和文档字符串。
- 重命名属于重构：单独成一次提交，不与功能或缺陷修复混在一起；用 `git mv` 保留历史，导入与引用一并改完，检查照常通过。
- 不为符合本文而批量改名现有文件；确有必要时作为单独的重构任务处理。

## 代码中的名字（摘要）

文件名之外，代码标识遵循 PEP 8 与现有代码：

| 场景 | 规则 | 示例 |
| --- | --- | --- |
| 函数、变量、模块 | snake_case | `begin_turn` |
| 类 | PascalCase | `PurchaseOffer` |
| 模块级常量 | UPPER_SNAKE_CASE | `WALK_SPEED`、`CHIP_MODELS` |
| 协议 JSON 键 | camelCase；pydantic 模型用别名对应 | `protocolVersion` |
| 协议中的消息与原语类型值 | snake_case | `move_to`、`start_agent` |

## 检查方式

目前没有自动检查文件命名；ruff 未启用命名规则（`N`）。开发时按本文自查，审查时把违反本文的新文件名或位置作为问题提出。
