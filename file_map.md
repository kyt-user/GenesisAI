# GenesisAI 源码地图与阅读指南

> 本文件反映《GenesisAI 五层架构重构》S0–S7 完成后的结构：`shared ← model ← agent ← core ← app` 单向依赖，由 `import-linter` 机械强制（`pyproject.toml` 的 `[tool.importlinter]`）。
>
> 历史版本的目录（`runtime/`、`capabilities/`、`prompt/`、`state/`、`skills/`、`evals/`、`project_docs/` 顶层位置等）已被迁移到五层结构或删除；早期「按需加载」的 6 个元工具目录已移除。详见 `docs/GenesisAI五层架构重构规格.md`。

---

## 一、目录地图

```text
src/genesisai/
├── __init__.py  __main__.py         # 薄壳：python -m genesisai → app.cli:entry
│
├── shared/                          # L1 纯数据契约，不依赖任何内部层
│   ├── messages.py                  #   Message / Response / ToolCall
│   ├── changes.py                   #   文件变更记录 + 原子写 + 备份
│   ├── filetypes.py  security.py    #   文件类型常量 + Access 边界 / 安全
│   ├── ids.py  errors.py            #   sha/uid + 通用错误（承接原 V1/V4 下沉值）
│   └── budgets.py  results.py  run_states.py  web.py
│
├── model/                           # L2 模型契约与提供方
│   ├── base.py  config.py           #   ModelClient ABC + YAML 配置加载
│   ├── openai_compatible.py         #   chat() / stream_chat()
│   └── providers/                   #   deepseek / qwen
│
├── agent/                           # L3 ReAct 循环 + 端口
│   ├── loop.py                      #   循环骨架：drive/stop/fail/cancel/resume/confirm
│   ├── ports.py                     #   ToolPort / PromptPort / MemoryPort / SessionPort
│   ├── recovery.py                  #   模型轮次与协议恢复
│   ├── runner.py                    #   兼容壳（转发到 loop）
│   └── state_machine.py  completion.py  evidence.py
│
├── core/                            # L4 工具引擎 + 扩展缝 + 领域实现
│   ├── tools/                       #   工具执行引擎（原 runtime/*）
│   │   └── registry.py  catalog.py  loader.py  executor.py
│   │       permissions.py  ledger.py  base.py  tool_runtime.py
│   ├── extensions/
│   │   ├── tools/kernel/            #     9 个内核工具（read_files/list_files/
│   │   │                            #     search_codebase/editor/run_commands/
│   │   │                            #     fetch_web_content/use_skill/memory/attempt_completion）
│   │   ├── tools/office|novel/      #     保留为工具包的领域能力
│   │   ├── skills/                  #     Skill 执行缝（内核工具 use_skill）
│   │   ├── rules/                   #     AGENTS.md 注入缝
│   │   ├── mcp/                     #     MCP 动态桥接缝
│   │   └── workflows/               #     斜杠工作流缝
│   ├── prompt/                      #   composer + context_budgeter + task_protocols + templates/
│   ├── memory/                      #   长期记忆
│   ├── state/                       #   store.py + trace.py
│   ├── project_docs/                #   agent_docs 管理
│   ├── development/                 #   开发验证策略（原 loop 的自动验证/恢复，经端口调用）
│   └── research.py                  #   网络研究编排（原 agent/research.py）
│
└── app/                             # L5 生成根 + 呈现 + 验收
    ├── cli.py  terminal_view.py  maintenance.py
    └── evals/                       #   仿真与验收数据集（原 evals/）
```

---

## 二、数据流全景

```text
用户输入
  │
  ▼
app/cli.py ────────────────────────────────────┐
  │ 组装 core 实现并注入 agent 端口              │
  ▼                                             │
agent/loop.py ◄── core/prompt/composer           │
  │ ReAct 循环（经 PromptPort 取系统提示）        │
  │   ├─ model/openai_compatible → 云端模型      │
  │   ├─ core/tools/tool_runtime → 工具执行      │
  │   │     ├─ registry/catalog（全量下发）       │
  │   │     ├─ executor → core/extensions/tools  │
  │   │     └─ permissions                      │
  │   ├─ core/state/store → 会话持久化            │
  │   ├─ core/research → 网络研究                 │
  │   └─ core/development → 开发验证策略（经端口） │
  ▼                                             │
app/terminal_view.py ── 渲染输出到终端 ◄─────────┘
```

---

## 三、建议阅读顺序

### 第一轮：骨架理解

| 顺序 | 文件 | 目的 |
|:---:|------|------|
| 1 | `shared/messages.py` | 核心数据流：Message/Response/ToolCall |
| 2 | `model/base.py` | 模型抽象接口 |
| 3 | `core/tools/base.py` | 工具系统数据契约：ToolSpec/ToolResult/ToolError |
| 4 | `model/config.py` | 模型配置加载（provider→endpoint/key） |
| 5 | `agent/ports.py` | 端口定义：agent 需要什么 |
| 6 | `agent/state_machine.py` | 五阶段状态机 |

### 第二轮：核心引擎

| 顺序 | 文件 | 目的 |
|:---:|------|------|
| 7 | `app/cli.py` | 入口：工作区、权限、交互循环、组合根 |
| 8 | `agent/loop.py` | ★ ReAct 主循环（原 runner 的循环骨架） |
| 9 | `agent/recovery.py` | 模型轮次、协议恢复与证据校验 |
| 10 | `core/tools/tool_runtime.py` | 工具运行时如何组装各层 |
| 11 | `core/tools/catalog.py` | 全量下发：`definitions()` 返回全部可用工具 |
| 12 | `core/prompt/composer.py` | 系统提示词拼装 |

### 第三轮：关键子系统

| 顺序 | 文件 | 目的 |
|:---:|------|------|
| 13 | `core/prompt/context_budgeter.py` | 上下文预算管理 |
| 14 | `core/state/store.py` | 会话持久化 + Schema 迁移 |
| 15 | `core/research.py` | 网络研究控制器 |
| 16 | `model/openai_compatible.py` | 模型调用实现（含流式） |
| 17 | `app/terminal_view.py` | 终端渲染 |

### 第四轮：工具与扩展缝抽查

| 顺序 | 路径 | 目的 |
|:---:|------|------|
| 18 | `core/extensions/tools/kernel/editor/` | 看一个内核工具的 spec→impl→yaml 三件套 |
| 19 | `core/extensions/tools/kernel/run_commands/` | 命令/测试/git 统一入口 |
| 20 | `core/extensions/skills/` | Skill 执行缝 |
| 21 | `core/extensions/rules/` + `mcp/` + `workflows/` | 其余三条扩展缝 |
| 22 | `shared/security.py` | Access 边界与安全策略 |

---

## 四、内核工具索引（9 个）

| 工具 | 权限 | 吸收原工具 |
| --- | --- | --- |
| `read_files` | read | `file_read` |
| `list_files` | read | `file_list` |
| `search_codebase` | read | `file_search` + `code_index` |
| `editor` | write | `file_create/patch/move/delete/copy` + `directory_create` |
| `run_commands` | shell | `shell_run` + `test_run` + `python_project` + `git_*` |
| `fetch_web_content` | network | `search_query` + `search_fetch` |
| `use_skill` | core | skills 缝 |
| `memory` | write | `memory_*` + `project_plan` |
| `attempt_completion` | core | `submit_and_exit`（同构） |

> 领域能力另由 `office`（10）/`novel`（4）工具包与 MCP 动态工具承载，详见《GenesisAI 五层架构重构规格》§4 与升级路线 §8。

---

## 五、审计重点关注建议

| 优先级 | 文件 | 原因 |
|:---:|------|------|
| ★★★ | `agent/loop.py` + `agent/recovery.py` | 核心循环与恢复机制 |
| ★★★ | `core/prompt/context_budgeter.py` | 上下文预算直接影响模型决策质量 |
| ★★★ | `core/prompt/composer.py` + `templates/` | 提示词工程核心 |
| ★★☆ | `core/tools/catalog.py` + `tool_runtime.py` | 全量下发与工具运行时架构 |
| ★★☆ | `core/state/store.py` | 持久化 + Windows 兼容性（atomic_json） |
| ★★☆ | `core/extensions/tools/filesystem/shared.py` | 路径安全校验 |
| ★☆☆ | `app/evals/*` | 评测覆盖度 |