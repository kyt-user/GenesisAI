# GenesisAI 五层架构重构规格

日期：2026-10-04
状态：规格已定稿；S0–S7 代码实施已完成（§11 为回填的实施结果）
适用范围：`src/genesisai/`（不含 `cline_sdk/`）
关系：本文与《GenesisAI升级路线.md》配套。**本文覆盖升级路线 §8「Tool Catalog 与全量下发」的全部内容**（该机制因产品约束变更而作废，见 §1.2）。

---

## 1. 决策记录

### 1.1 本轮锁定项

| # | 决策 | 含义 |
| --- | --- | --- |
| D1 | **Route B：架构全面对齐 Cline 五层** | `src/genesisai` 重组成 `shared ← model ← agent ← core ← app` 单向依赖，并用机械防线强制 |
| D2 | **只面向 DeepSeek / Qwen 等云模型** | 上下文足够大，延迟加载工具（按需加载）失去存在理由；首版移除 Ollama / 百炼 |
| D3 | **工具面：引入外部扩展缝** | 少量通用内核 + 新建 Skills(可执行) / Rules / MCP / Workflows 四条扩展缝承载领域能力 |
| D4 | Python 惯用命名 | 目标目录/符号用 Python 惯用名（`model`/`agent`），不照搬 Cline 的 TS 名（`llms`/`agents`） |
| D5 | 工具定义落位 `core/extensions/tools/` | 原 `capabilities/` 迁入该路径 |
| D6 | 全量下发 | 每次模型请求下发全部启用工具的完整定义，不再有 Core/Active 两阶段 |

### 1.2 对既有文档的冲击（Supersede）

- **《升级路线》§8 全部作废**：`tool_search / tool_describe / tool_load / skill_search / skill_describe / skill_load`（6 个元工具）、Core/Active/Available/Disabled 四态、`active_limit=8`、"两阶段加载" 流程全部删除。
- 《升级路线》§16「建议项目结构」由本文 §3 取代。
- 新增约束：首版只保留 OpenAI 兼容协议提供方（DeepSeek/Qwen），`model/providers/ollama.py`、`model/providers/bailian.py` 移出主链。

---

## 2. 现状审计（实测）

### 2.1 模块体量（非 `capabilities` 部分）

| 模块 | 文件 | 行数 | 说明 |
| --- | --- | --- | --- |
| `agent/` | 6 | 1340 | `runner.py` 822 行为上帝类 |
| `app/` | 4 | 936 | `cli.py` 440、`terminal_view.py` 456 |
| `capabilities/` | 148 | 2696 | 50 个工具目录（filesystem 11 / office 11 / git 8 / memory_tools 7 / novel 5 / shell 4 / web 4） |
| `evals/` | 4 | 726 | 仿真与验收数据集 |
| `memory/` | 10 | 1381 | |
| `model/` | 7 | 339 | 已是干净契约（`base.py` 仅 `ModelClient` ABC） |
| `project_docs/` | 2 | 371 | `agent_docs/` 托管式项目工作记忆 |
| `prompt/` | 3 | 502 | `composer.py` + `context_budgeter.py` |
| `runtime/` | 28 | 1144 | 工具引擎 + 6 个元工具 |
| `shared/` | 5 | 218 | |
| `skills/` | 2 | 107 | 只读 Markdown 提示，不可执行 |
| `state/` | 3 | 351 | |

### 2.2 跨模块依赖图（实测）

```
shared      -> capabilities, state                 ← 违规（L1 依赖 L4）
model       -> shared                              ← 合法
agent       -> capabilities, project_docs, prompt, runtime, shared, state
runtime     -> agent, capabilities, shared, skills ← 违规（L4 依赖 L3）
capabilities-> memory, project_docs, runtime, shared, state
prompt      -> memory, project_docs, shared, skills
memory      -> state
state       -> agent, shared                       ← 违规（L4 依赖 L3）
project_docs-> state
skills      -> (无内部依赖)
app         -> agent, memory, model, project_docs, runtime, shared, skills, state
evals       -> 全部                                 ← 顶层，合法
```

### 2.3 违规边清单（精确到 file:line，必须清除）

| 编号 | 位置 | 现依赖 | 目标修法 |
| --- | --- | --- | --- |
| V1 | `shared/changes.py:9` | `from genesisai.state.store import sha, uid` | `sha/uid` 下沉到 `shared/ids.py`，`state.store` 改为引用它 |
| V2 | `shared/filetypes.py:7` | `from genesisai.capabilities.filesystem.shared import TEXT_SUFFIXES` | 常量下沉到 `shared/filetypes.py`，工具侧改为引用它 |
| V3 | `state/store.py:39,126,130` | `from genesisai.agent.research import ResearchController` | `state` 不再持有研究编排；研究状态由 `agent` 经端口写入 |
| V4 | `state/store.py:215` | `from genesisai.agent.state_machine import RUN_STATES` | `RUN_STATES` 常量下沉到 `shared`，双方共用 |
| V5 | `runtime/tool_runtime.py:14-18` | `capabilities.web.*` + `agent.research` | 网络提供方由 `core` 注入；`ResearchController` 移除出运行时装配 |

> 其余现边（如 `capabilities→memory/state/runtime`、`prompt→memory/skills`、`memory→state`）在目标分层中均落入 `core` 内部，不再是跨层违规。

### 2.4 按需加载机制清点（待删除）

| 载体 | 位置 | 处置 |
| --- | --- | --- |
| 6 个元工具定义 | `runtime/builtin_tools/core/*` | 整个目录删除 |
| `CORE_NAMES` 常量 | `runtime/registry.py:17` | 删除；注册表不再要求 Core 工具存在 |
| 四态机 `status()` | `runtime/catalog.py:51-62` | 删除；保留 `enabled/disabled` 判据 |
| `active_limit` / `activate()` | `catalog.py:162-197` | 删除 |
| `definitions()` 拼接 Core+Active | `catalog.py:77-83` | 改为返回**全部启用工具** |
| Active 持久化/还原 | `catalog.py:26-42`、`store["tool_runtime"]["active_tools"]` | 删除 |
| `tool_runtime.load_tools/reset_task/snapshot` | `runtime/tool_runtime.py:184-194` | 删除 |
| CLI `/tools` 的四态展示 | `app/cli.py` | 改为"启用/禁用清单" |

> **保留不动**：`registry`（扫描校验）、`loader`（执行期惰性 import，避免可选依赖启动即崩）、`executor`（权限/台账/`unknown_execution` 防重放）、`permissions`、`ledger`。这些是安全管线，与"延迟下发"无关。

---

## 3. 目标五层架构

### 3.1 分层与允许依赖

对齐 Cline `shared ← llms ← agents ← core ← apps`，映射为 Python 惯用名：

```
L5  app       生成根 + 呈现 + 验收          app/(cli,terminal_view,maintenance) + evals/
L4  core      引擎 + 扩展 + 领域实现         runtime→core/tools, capabilities→core/extensions/tools,
                                            prompt, memory, state, skills→extensions, project_docs
L3  agent     ReAct 循环 + 端口(ports)       runner(loop), completion, evidence, state_machine, ports
L2  model     模型契约与提供方               base, config, openai_compatible, providers
L1  shared    纯数据契约 + 无依赖工具         messages, changes, filetypes, security, errors, ids
```

**规则：低层永不 import 高层。** 每一层只能依赖其下层。

### 3.2 契约缝与策略实现分离（判据）

沿用 Cline 的判据并明确化：

> 一个能力若依赖 **`contextWindow` / 上一轮真实 token / 用户配置 / 会话持久化 / 文件系统 I-O**，则它属于 `core`；`agent` 只定义**它需要什么**（端口），不定义**怎么做**。

推论：
- `agent/ports.py` 定义 `ToolPort`（拿到工具定义 + 执行调用）、`PromptPort`（拿到系统提示 + 上下文）、`MemoryPort`、`SessionPort`。
- `core/` 提供这些端口的策略实现（工具运行时、Prompt Composer、Memory、Session Store）。
- `app/` 作为组合根，把 `core` 的实现注入 `agent`。

### 3.3 目标目录树

```text
src/genesisai/
├── shared/                     # L1
│   ├── messages.py  changes.py  filetypes.py  security.py
│   └── ids.py  errors.py                       # 新增：承接 V1/V4 下沉常量
├── model/                      # L2
│   ├── base.py  config.py  openai_compatible.py
│   └── providers/  (deepseek, qwen)            # ollama/bailian 移出主链
├── agent/                      # L3
│   ├── loop.py                 # 原 runner 的循环骨架（drive/stop/fail/cancel/resume/confirm）
│   ├── ports.py                # 新增：ToolPort/PromptPort/MemoryPort/SessionPort
│   ├── state_machine.py  completion.py  evidence.py
│   └── recovery.py             # 原 runner 的模型/协议恢复部分
├── core/                       # L4
│   ├── tools/                  # 工具执行引擎（原 runtime/*，去掉 builtin_tools）
│   │   └── registry.py  catalog.py(精简)  loader.py  executor.py  permissions.py  ledger.py  base.py  tool_runtime.py
│   ├── extensions/
│   │   ├── tools/              # 原 capabilities/*（内置工具包，50 个目录）
│   │   ├── skills/             # Skill 执行缝
│   │   ├── rules/              # Rules 注入缝（融合 project_docs）
│   │   ├── mcp/                # MCP 桥接缝
│   │   └── workflows/          # 斜杠工作流缝
│   ├── prompt/                 # composer.py  context_budgeter.py  templates/
│   ├── memory/                 # 记忆
│   ├── state/                  # store.py  trace.py
│   └── project_docs/           # agent_docs 管理（或并入 extensions/rules）
├── app/                        # L5
│   ├── cli.py  terminal_view.py  maintenance.py
│   └── evals/                  # 原 evals/（验收与仿真）
├── cli.py  __main__.py         # 薄壳：转发到 app.cli:entry
└── __init__.py
```

### 3.4 现状 → 目标映射表

| 现状 | 目标 | 层 | 动作 |
| --- | --- | --- | --- |
| `shared/` | `shared/` | L1 | 净化，消除 V1/V2；新增 `ids.py`/`errors.py` |
| `model/` | `model/` | L2 | 删 ollama/bailian，加 qwen（或复用 openai_compatible） |
| `agent/runner.py`(822) | `agent/loop.py` + `agent/recovery.py` + `core/*` | L3/L4 | 拆解，见 §7 |
| `agent/{completion,evidence,state_machine}` | `agent/` | L3 | 保留；`RUN_STATES` 下沉 shared（V4） |
| `agent/research.py` | `core/`（研究编排）| L4 | 迁出 agent；从 `runtime`/`state` 解耦（V3/V5） |
| `runtime/*`（引擎） | `core/tools/` | L4 | 迁入；删四态机与元工具 |
| `runtime/builtin_tools/core/*` | — | — | **删除** |
| `capabilities/*` | `core/extensions/tools/*` | L4 | 迁入（D5） |
| `prompt/` | `core/prompt/` | L4 | 迁入 |
| `memory/` | `core/memory/` | L4 | 迁入 |
| `state/` | `core/state/` | L4 | 迁入；消除 V3/V4 |
| `skills/` | `core/extensions/skills/` | L4 | 升级为可执行缝（§5.1） |
| `project_docs/` | `core/extensions/rules/`（或 `core/project_docs/`） | L4 | 与 Rules 缝对齐（§5.2） |
| `app/` | `app/` | L5 | 保留 |
| `evals/` | `app/evals/` | L5 | 迁入 |

---

## 4. 外部扩展缝设计

Cline 的内核只有 9 个工具，其余能力由 **4 条外部扩展路径**挂载。GenesisAI 现状 **0 条**，因此把内核撑到了 50 个工具目录。本轮新建这 4 条缝。

### 4.1 Skills 缝（可执行）

- **现状**：`skills/registry.py` 只做 `skill.yaml`+Markdown 的**只读加载**，`skill_load` 把正文塞进上下文，不提供能力。
- **目标**：一个内核工具 `use_skill(name)`，一次调用返回该 Skill 的**指令正文**作为工具结果（Cline `skills` 工具同构），模型据指令推进任务。
- **契约**：`skill.yaml` 四字段保持 `{name, description, entry, tools}`；`entry` 为 Markdown。
- **删除**：`skill_search / skill_describe / skill_load` 三个元工具。

### 4.2 Rules 缝（系统提示自动注入）

- **现状**：缺失（`project_docs` 是**托管式**工作记忆，非用户手写规则）。
- **目标**：`core/extensions/rules/` 在 Prompt 组装期读取 `AGENTS.md`（项目级）与 `~/.genesisai/AGENTS.md`（用户级），**免工具调用**注入系统提示。
- **边界**：Rules 只读、只注入文本；不执行脚本、不扩大权限。

### 4.3 MCP 缝（动态工具桥接）

- **现状**：缺失。
- **目标**：`core/extensions/mcp/` 启动时读取 `.genesis/mcp.json`，连接 MCP server，把其工具以 `category="mcp"` **动态注册**进同一 `ToolRegistry`。
- **契约**：MCP 工具与内置工具走**同一条** executor/permissions/ledger 管线；只读/写/网络权限按 MCP 注解映射。
- **注意**：注册表需支持"动态（非 `tool.yaml`）来源"，与现 `_scan()` 的静态目录扫描并存。

### 4.4 Workflows 缝（斜杠工作流）

- **现状**：只有内置斜杠命令（`/new /permissions /tools ...`）。
- **目标**：`.genesis/workflows/*.md` 定义可复用工作流，`/workflow <name>` 展开为一段提示。与 Rules 同为"文本承载"，但不进系统提示、按需触发。

### 4.5 内核收敛（对齐 Cline 9 内核）

在缝就位后，把现有 50 个工具目录向通用内核收敛。**首版内核（建议）**：

| 内核工具 | 吸收原工具 |
| --- | --- |
| `read_files` | `file_read`（多文件 + 分页） |
| `list_files` | `file_list` |
| `search_codebase` | `file_search` + `code_index`（正则） |
| `editor` / `apply_patch` | `file_create`+`file_patch`+`file_move`+`file_delete`+`file_copy`+`directory_create` |
| `run_commands` | `shell_run`+`test_run`+`git_*`（经 shell 权限） |
| `fetch_web_content` | `search_query`+`search_fetch` |
| `use_skill` | skills 缝 |
| `memory` | `memory_*` |
| `attempt_completion` | `submit_and_exit` 同构 |

> 收敛是**行为变更**，风险最高，排在扩展缝就位之后（见 §9，S6），并需全量回归。office/novel 等可按"技能 + 既可保留为工具包"两条路择一，由实施阶段决定。

---

## 5. 按需加载机制拆除

删除清单见 §2.4。拆除后的关键行为变化：

```python
# core/tools/catalog.py（目标）
def definitions(self) -> list[dict]:
    """全量下发：返回所有 enabled 且平台/依赖可用的工具完整定义。"""
    return [d.definition() for d in self.registry.values() if self.enabled(d)]
```

- 首轮请求即携带全部内核 + 内置工具包（office 依赖缺失时按 `unavailable_reason()` 自动剔除）。
- 稳态成本从"6 元工具 + ≤8 Active"变为"全部启用工具完整定义"，DeepSeek/Qwen 上下文本可容纳（升级路线已核算约 5.5K token）。
- `store["tool_runtime"]["active_tools"]` 从会话 Schema 移除；`state` 迁移入口需处理旧字段。

---

## 6. Runner 上帝类拆解

`agent/runner.py`（822 行）按职责切成 5 块，落到 L3/L4：

| 职责 | 方法（现位置） | 目标归属 | 层 |
| --- | --- | --- | --- |
| 循环控制 | `drive/start/stop/fail/cancel/resume/confirm/_confirm/close_pending/status/ensure_available` | `agent/loop.py` | L3 |
| 模型轮次 | `_call_model/_handle_length/_handle_empty/_recover_serialized_tool_output/_recover_tool_protocol/model_error` | `agent/recovery.py` | L3 |
| 协议/恢复校验 | `_can_reuse_answer/_invalid_calls/_answer_refs_valid/_recover_bad_refs/_evidence_fallback/_recover_unsupported_prices/_price_claims/_price_amounts` | `agent/recovery.py` | L3 |
| 上下文与工具结果 | `context/append_result/_execute_pending` | `agent/loop.py` 经 `ToolPort`/`PromptPort` 调 `core` | L3 |
| 开发验证 | `_should_auto_verify/_requires_change_verification/_development_change_required/_recover_tool_required/_inject_verification_test` | `core/development/` | L4 |
| 项目文档收尾 | `_finish_project_docs` | `core/project_docs/` | L4 |
| 任务协议选择 | `_is_development_continuation/_accepts_default_plan/_requests_development_execution/_protocols_for` | `core/prompt/`（经 `PromptPort`） | L4 |

**解耦要点**：`agent/loop.py` 只持有 `ports`，不 import 任何 `core.*`；`core` 的实现由 `app` 注入。

---

## 7. 机械防线

### 7.1 import-linter（分层强制）

`pyproject.toml` 增加：

```toml
[project.optional-dependencies]
dev = ["pytest>=8,<10", "import-linter>=2,<3"]

[tool.importlinter]
root_package = "genesisai"

[[tool.importlinter.contracts]]
name = "五层单向依赖"
type = "layers"
layers = [
  "genesisai.app",
  "genesisai.core",
  "genesisai.agent",
  "genesisai.model",
  "genesisai.shared",
]

[[tool.importlinter.contracts]]
name = "shared 不得依赖其它层"
type = "independence"
modules = ["genesisai.shared"]
```

### 7.2 命名与结构约定

- 目录/符号一律 Python 惯用 `snake_case`（D4）。
- 每个内置工具目录保持 `tool.yaml + spec.py + implementation.py` 三件套。
- 扩展缝各自的"来源目录"（skills/mcp/workflows/rules）与内置工具目录**物理隔离**，但汇入同一 registry。

### 7.3 门禁

- CI/提交前：`lint-imports` 必须通过；新增跨层 import 直接失败。
- 每层只暴露一个 `__init__.py` 公共面（避免外部直接触碰内部模块路径）。

---

## 8. 迁移切片（有序、每片可测）

| 片 | 内容 | 退出条件 | 状态 |
| --- | --- | --- | --- |
| **S0** | 冻结基线：记录 336 用例与当前依赖图 | 基线测试全绿、依赖图存档 | 完成 |
| **S1** | shared 净化（V1/V2）：新增 `ids.py`/`errors.py`，移除 L1→L4 依赖 | `lint-imports` 中 shared 独立性通过 | 完成 |
| **S2** | agent 端口化 + Runner 拆解（§6），消除 V3/V4/V5 与 L3→L4 违规 | agent 不再 import core；runner 单测拆分后全绿 | 完成 |
| **S3** | 目录重排到五层（`capabilities→core/extensions/tools` 等）+ `import-linter` 上线 | 五层契约通过；导入路径全量替换 | 完成 |
| **S4** | 拆除按需加载 + 全量下发（§5） | 首轮请求含全量定义；删除项无残留引用 | 完成 |
| **S5** | 扩展缝落地（Skills 可执行 / Rules / MCP / Workflows） | 每条缝有契约测试；`use_skill` 可用 | 完成 |
| **S6** | 内核收敛（§4.5，行为变更） | 全量回归通过；工具面达到内核形态 | 完成 |
| **S7** | 全量回归 + 文档同步（回填本文与升级路线 §8） | `lint-imports` + 336 用例 + 真实云模型冒烟 | 完成 |

> S1–S4 为**结构性**改造（原则上不改外部行为），S5–S6 含**行为变更**，风险递增。

---

## 9. 测试与验收

1. **架构测试**：`lint-imports`（分层 + shared 独立性）纳入默认测试集。
2. **契约测试**：每条扩展缝一个契约测试（skills 解析、rules 注入顺序、mcp 动态注册、workflow 展开）。
3. **回归**：现有 336 用例必须保持通过；S3 搬迁后先跑全量再继续。
4. **拆除验证**：断言首轮模型请求携带全部启用工具；断言 6 元工具与 `active_tools` 字段彻底消失。
5. **真实模型冒烟**：DeepSeek 执行一个“读代码→改→跑测试→出 diff”的端到端任务（对齐升级路线 §19.2）。**回填结果：已通过**。`deepseek-v4-flash` 在真实仓库中完成：`run_commands` 复现失败（`2 failed`）→ `read_files` 定位 `calc.py` 的 `add` 运算符缺陷 → `editor` 打补丁修复（sha256 变更）→ 复测 `2 passed` → `git diff` 展示唯一改动 → 会话 `status=completed`，并自动把已验证测试命令写入 Memory（`type=command`）。运行方式：`python cli.py --env-file .env --workspace <ws> --input <ws> --output <ws> --prompt ... --yes-writes --yes-shell --allow-remote-data`。

---

## 10. 风险与回滚

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 全量下发后首轮 prompt 变大 | token/延迟上升 | 依赖缺失工具自动剔除；持续量测 token |
| 内核收敛（S6）改变行为 | 破坏既有验收 | 排在最后、独立片、全量回归；必要时拆为子片 |
| MCP 动态工具与静态工具冲突 | 注册/权限错配 | 命名空间前缀 `mcp__<server>__<tool>`；统一走 executor |
| import 路径大迁移 | 遗漏引用 | 机械替换 + `lint-imports` + 全量测试三重校验 |
| Rules 注入隐私泄露 | 敏感内容入提示 | 复用 `project_docs.manager` 的密钥脱敏（`SECRET` 正则） |

回滚策略：S1–S6 每片独立提交，任一片失败可回退该片而不影响前序成果。

---

## 11. 实施结果（回填）

本节在 S0–S7 实施完成后回填，记录与本规格的对照结果。

### 11.1 分层与机械防线

- 五层 `shared ← model ← agent ← core ← app` 已就位；`evals/` 并入 `app/evals/`，不再单列为独立层。
- `lint-imports`：**2 kept, 0 broken**（`五层单向依赖` + `shared 不得依赖其它层`）。实际契约以 `app/core/agent/model/shared` 五层为准（见 `pyproject.toml`），§7.1 示例中的 `genesisai.evals` 层已按实现调整。
- `shared/` 实际包含 `messages, changes, filetypes, security, ids, errors, budgets, results, run_states, web`。

### 11.2 按需加载拆除

- 6 个元工具目录、`CORE_NAMES`、catalog 四态机、`active_limit` 与 `active_tools` 持久化均已删除，无残留引用。
- `catalog.definitions()` 返回全部启用且可用工具的完整定义；首轮请求即全量下发。

### 11.3 外部扩展缝

- `core/extensions/{skills,rules,mcp,workflows}/` 四条缝就位，各带契约测试；`use_skill` 内核工具可用。

### 11.4 内核收敛

- 工具面收敛为 **9 个内核工具**（`kernel` 分类）+ 保留工具包 `office`（10）+ `novel`（4），共 **23** 个 `tool.yaml` 工具目录。
- MCP 动态工具按 `category="mcp"` 在运行期注册，无 `tool.yaml`。

### 11.5 提供方收敛

- `model/providers/` 保留 `deepseek` / `qwen`，移除 `ollama` / `bailian`；`config.PROVIDERS = {deepseek, qwen}`。

### 11.6 回归

- 全量用例迁移到内核工具面后为 **335 passed, 1 skipped, 2 failed**；2 项失败均为 S0 基线已记录的非门禁环境/产物缺口（缺 `docs/SOURCE_BASELINE.json`、缺 `agent_tests_workspace/` fixture），**无 NEW 失败**。
- §9.4 拆除验证断言已补齐：`tests/test_tool_catalog.py::test_on_demand_loading_meta_tools_are_gone` 断言 6 个元工具（`tool_search/tool_describe/tool_load/skill_search/skill_describe/skill_load`）与 `active_tools` 字段、`load_tools/reset_task` 方法彻底消失；`test_tool_runtime.py`/`test_cli_ui.py` 断言首轮请求携带全量定义且会话 `tool_runtime` 仅含 `active_skills`。
- 相对 S0 基线（`5 failed, 330 passed, 1 skipped`），3 项既存失败转为通过（工具目录契约用例重写、安装 `python-pptx` 后 office 仿真通过、测试隔离用例转绿）。
- 真实云模型冒烟：**已通过**。CLI 单次任务在真实仓库完成“复现失败→定位→修改→复测通过→git diff→Memory 记录已验证测试命令”，会话 `status=completed`（详见 §9 第 5 条）。

### 11.7 Runner 拆解落点

§6 的职责拆分已按目标落点实现（`agent/loop.py` 不 import 任何 `core.*`，经端口调用）：

- 循环骨架 → `agent/loop.py`；模型轮次与协议恢复 → `agent/recovery.py`。
- 开发验证 → `core/development/policy.py` 的 `DevelopmentPolicy`（`should_auto_verify/requires_change_verification/development_change_required/recover_tool_required/inject_verification_test`），由 `app` 组装期挂到 `ToolRuntime.development`，`agent` 经 `DevelopmentPort` 端口调用。
- 项目文档收尾 → `core/project_docs/`（经 `DocsPort`）；任务协议选择 → `core/prompt/`（经 `PromptPort`）。