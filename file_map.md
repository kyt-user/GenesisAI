
让我统计每个模块的代码量，以便给出更准确的阅读建议。


下面是 `src/genesisai/` 的完整目录地图和阅读指南。

---

## 一、目录地图

```
src/genesisai/                          # ~6000 行 Python
├── __init__.py (2)                     # 版本号 __version__
├── __main__.py (3)                     # python -m genesisai 入口
│
├── app/                    ~900 行     # 【应用层】CLI 交互 + 终端渲染
│   ├── cli.py              436         #   主入口：工作区选择、命令解析、交互循环
│   ├── terminal_view.py    456         #   Rich 终端渲染：流式输出、Markdown、工具面板
│   └── maintenance.py      ~30         #   会话清理、日志轮转等维护命令
│
├── agent/                  ~1300 行    # 【智能层】ReAct 循环核心
│   ├── runner.py           782         #   ★ Runner.drive() 主循环：思考→行动→观察
│   ├── research.py         400         #   网络研究控制器：预算、去重、证据管理
│   ├── state_machine.py     59         #   阶段状态机：prepare→plan→execute→verify→answer
│   ├── evidence.py          47         #   来源证据登记
│   └── completion.py        10         #   完成校验：continue/answer/recover
│
├── prompt/                 ~500 行     # 【提示层】系统提示词组装
│   ├── context_budgeter.py 371         #   ★ 上下文预算管理：60K 字符分配
│   ├── composer.py         127         #   提示词拼装：身份+政策+协议+工具+契约
│   └── templates/                      #   YAML/Markdown 提示词模板
│       ├── system/         (identity, policies)
│       ├── protocols/      (coding, debugging, web_research... 11个)
│       ├── states/         (prepare, plan, execute, verify, answer, recover)
│       ├── tools/          (catalog_usage, evidence_usage, skill_usage)
│       └── completion/     (final_answer, recovery)
│
├── model/                  ~250 行     # 【模型层】LLM 调用
│   ├── openai_compatible.py 151        #   OpenAI 兼容协议：chat() + stream_chat()
│   ├── config.py            56         #   模型配置加载：provider→endpoint/key
│   ├── base.py              ~40        #   ModelClient 抽象基类
│   └── providers/                      #   DeepSeek / 百炼 / Ollama 适配
│
├── runtime/                ~980 行     # 【工具运行时】五层架构
│   ├── base.py             191         #   ToolSpec / ToolResult / ToolError 数据定义
│   ├── catalog.py          213         #   工具目录：Core→Active→Available→Disabled
│   ├── registry.py         140         #   工具注册表：扫描 + 注册 + 查找
│   ├── loader.py           ~80         #   动态加载器：从 spec 加载 implementation
│   ├── executor.py         152         #   执行器：参数校验→执行→结果封装
│   ├── tool_runtime.py     171         #   ★ 组装入口：把五层粘合在一起
│   ├── permissions.py       65         #   权限策略：network/writes/shell
│   ├── ledger.py            49         #   工具调用账本
│   └── builtin_tools/core/ 6 个工具    #   tool_load/search/describe + skill_load/search/describe
│
├── capabilities/           ~1600 行    # 【能力层】42 个工具实现
│   ├── filesystem/           11 工具   #   file_create/read/patch/delete/copy/move/list/search
│   │   ├── shared.py        116        #     路径校验、Access 边界
│   │   ├── patching.py       53        #     diff 补丁引擎
│   │   └── code_index/      113        #     代码索引
│   ├── web/                   2 工具   #   search_query + search_fetch
│   │   ├── contracts.py     128        #     搜索/抓取协议定义
│   │   ├── network.py       125        #     HTTP 客户端封装
│   │   ├── structured_data.py 104     #     结构化数据提取
│   │   ├── links.py          51        #     链接规范化
│   │   └── providers/                 #     Brave / DuckDuckGo / Bing
│   ├── office/               11 工具   #   docx/xlsx/pptx/pdf 的 create/read/edit
│   │   └── helpers.py       156        #     Office 文件通用辅助
│   ├── shell/                 3 工具   #   shell_run + test_run + python_project
│   │   ├── process.py        88        #     子进程管理
│   │   ├── python_support.py 158      #     Python 项目检测
│   │   └── static_validate.py 127     #     静态语法检查
│   ├── git/                   8 工具   #   add/commit/diff/log/show/status/branch
│   ├── memory_tools/          6 工具   #   read/write/search/forget/validate + project_plan
│   └── novel/                 4 工具   #   chapter_read/list/outline + consistency_check
│
├── state/                  ~350 行     # 【状态层】持久化
│   ├── store.py            321         #   ★ JSON 会话存储 + Schema 迁移 v0→v3
│   └── trace.py             ~30        #   执行追踪记录
│
├── memory/                 ~250 行     # 【记忆层】长期知识
│   └── manager.py          250         #   记忆读写、搜索、过期清理
│
├── project_docs/           ~370 行     # 【项目文档层】agent_docs 管理
│   └── manager.py          368         #   策划/计划/进度/决策/验收文件读写
│
├── skills/                 ~106 行     # 【技能层】可复用任务模板
│   ├── registry.py         106         #   技能注册与查找
│   └── builtin/             7 个技能   #   bug_fix, code_review, document_generation...
│
├── shared/                 ~200 行     # 【共享层】公共定义
│   ├── security.py          85         #   Access 边界、HostLock、确认冻结
│   ├── changes.py           82         #   文件变更记录
│   ├── messages.py          ~30        #   Message / Response / ToolCall 数据类
│   └── filetypes.py         ~20        #   文件类型常量
│
└── evals/                  ~727 行     # 【评测层】自动化测试场景
    ├── simulation.py        282        #   产品模拟测试
    ├── python_development.py 280       #   Python 开发评测
    └── source_development.py 165       #   源码开发评测
```

---

## 二、数据流全景

```
用户输入
  │
  ▼
app/cli.py ──────────────────────────────────────┐
  │ 解析命令、组装运行时                            │
  ▼                                               │
agent/runner.py  ◄── prompt/composer.py           │
  │ drive() ReAct 循环              组装系统提示    │
  │   ├─ model/openai_compatible.py → LLM API     │
  │   ├─ runtime/tool_runtime.py → 工具执行        │
  │   │     ├─ catalog.py (四层状态)               │
  │   │     ├─ executor.py → capabilities/*       │
  │   │     └─ permissions.py                     │
  │   ├─ state/store.py → 会话持久化               │
  │   └─ agent/research.py → 网络研究              │
  ▼                                               │
app/terminal_view.py ──── 渲染输出到终端 ◄─────────┘
```

---

## 三、建议阅读顺序

### 第一轮：骨架理解（约 1.5h）

| 顺序 | 文件 | 行数 | 目的 |
|:---:|------|:---:|------|
| 1 | `shared/messages.py` | 31 | 理解核心数据流：Message/Response/ToolCall |
| 2 | `model/base.py` | ~40 | 理解模型抽象接口 |
| 3 | `runtime/base.py` | 191 | 理解工具系统的数据契约：ToolSpec/ToolResult |
| 4 | `model/config.py` | 56 | 理解模型配置如何加载 |
| 5 | `agent/state_machine.py` | 59 | 理解五阶段状态机 |
| 6 | `agent/completion.py` | 10 | 理解决策：继续/回答/恢复 |

> 第一轮目标：搞清楚 **数据在模块间怎么流动**

### 第二轮：核心引擎（约 3h）

| 顺序 | 文件 | 行数 | 目的 |
|:---:|------|:---:|------|
| 7 | `app/cli.py` | 436 | 理解入口：工作区、权限、交互循环 |
| 8 | `agent/runner.py` | 782 | **★ 最重要**：ReAct 主循环、恢复机制、开发延续 |
| 9 | `runtime/tool_runtime.py` | 171 | 理解工具运行时如何组装五层 |
| 10 | `runtime/catalog.py` | 213 | 理解工具四层状态管理 |
| 11 | `runtime/executor.py` | 152 | 理解工具执行流程 |
| 12 | `prompt/composer.py` | 127 | 理解系统提示词如何拼装 |

> 第二轮目标：搞清楚 **Runner 是怎么转起来的**

### 第三轮：关键子系统（约 2h）

| 顺序 | 文件 | 行数 | 目的 |
|:---:|------|:---:|------|
| 13 | `prompt/context_budgeter.py` | 371 | 上下文预算管理——影响模型表现的关键 |
| 14 | `state/store.py` | 321 | 会话持久化 + Schema 迁移 |
| 15 | `agent/research.py` | 400 | 网络研究控制器 |
| 16 | `model/openai_compatible.py` | 151 | 模型调用实现（含流式） |
| 17 | `app/terminal_view.py` | 456 | 终端渲染 |

> 第三轮目标：搞清楚 **上下文、状态、网络、渲染** 这四个关键子系统

### 第四轮：能力工具抽查（约 1h）

| 顺序 | 文件 | 行数 | 目的 |
|:---:|------|:---:|------|
| 18 | `capabilities/filesystem/file_create/` | 89+18+10 | 看一个完整工具的 spec→impl→yaml 三件套 |
| 19 | `capabilities/web/search_query/` | 53+10 | 搜索工具实现 |
| 20 | `capabilities/shell/test_run/` | impl+spec | 测试运行工具 |
| 21 | `shared/security.py` | 85 | Access 边界与安全策略 |

> 第四轮目标：**抽查工具实现模式**，确认理解 spec/impl/yaml 三件套约定

### 第五轮：按需深入（视审计问题而定）

- `memory/manager.py` — 记忆系统
- `project_docs/manager.py` — agent_docs 管理
- `skills/registry.py` — 技能系统
- `capabilities/filesystem/shared.py` + `patching.py` — 文件操作核心
- `capabilities/web/contracts.py` + `network.py` — 网络研究底层
- `evals/*` — 评测场景

---

## 四、审计重点关注建议

| 优先级 | 文件 | 原因 |
|:---:|------|------|
| ★★★ | `agent/runner.py` | 782 行核心循环，上次分析发现恢复机制对小模型不足 |
| ★★★ | `prompt/context_budgeter.py` | 上下文预算直接影响模型决策质量 |
| ★★★ | `prompt/composer.py` + `templates/` | 提示词工程核心 |
| ★★☆ | `runtime/catalog.py` + `tool_runtime.py` | 工具系统架构 |
| ★★☆ | `state/store.py` | 持久化 + Windows 兼容性问题（atomic_json） |
| ★★☆ | `capabilities/filesystem/shared.py` | 路径安全校验 |
| ★☆☆ | `evals/*` | 评测覆盖度 |