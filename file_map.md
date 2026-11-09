# GenesisAI 源码地图与阅读指南

本文件按 2026-11-09 的源码核对。五层依赖为 `shared ← model ← agent ← core ← app`，由 [pyproject.toml](pyproject.toml) 中的 import-linter 契约检查。现行用法见[用户指南](docs/用户指南.md)，历史重构过程见[五层架构重构规格](docs/GenesisAI五层架构重构规格.md)。

## 一、目录地图

以下为当前主要模块，省略测试夹具及辅助文件。

```text
src/genesisai/
├── __main__.py                    # python -m genesisai → app.cli
├── shared/                        # L1 数据契约、路径安全、预算与进程控制
│   ├── messages.py  session.py
│   ├── security.py  changes.py
│   └── context_config.py  request_timeouts.py  run_control.py
├── model/                         # L2 模型协议、配置与凭据
│   ├── base.py  config.py  settings.py  credentials.py
│   ├── capabilities.py            # 明确的端点/模型图片能力
│   ├── openai_compatible.py       # 流式与非流式模型调用
│   └── providers/                 # deepseek / qwen / qwen_token_plan
├── agent/                         # L3 单 Agent 循环及端口
│   ├── loop.py  ports.py
│   ├── recovery.py  progress.py
│   └── state_machine.py  completion.py  evidence.py
├── core/                          # L4 领域实现与服务组合
│   ├── tools/                     # 注册、选择、审批、执行与账本
│   │   ├── registry.py  catalog.py  selection.py  loader.py
│   │   ├── authorization.py  permissions.py  ledger.py
│   │   └── executor.py  worker.py  tool_runtime.py
│   ├── jobs/                      # 独立作业、输入回执、结果和写入协调
│   ├── persistence/               # SQLite 会话、资源、身份与文件持久化
│   ├── memory/                    # 任务账本、版本知识、索引与后台维护
│   ├── prompt/                    # 上下文预算、压缩、路由、来源与模板
│   ├── capabilities/              # 外挂资产、修订、测试、试用、注册
│   ├── media/                     # 图片资产与模型输入视图
│   ├── state/                     # JSONL 日志和执行跟踪
│   ├── development/               # 开发验证策略
│   ├── research.py  price_calculations.py
│   └── extensions/
│       ├── tools/kernel/          # 23 个 YAML 声明工具
│       ├── tools/office/          # 11 个文档工具（含 OCR）
│       ├── tools/media/           # 4 个本地音视频工具
│       ├── tools/novel/           # 4 个章节/事实工具
│       ├── tools/filesystem/      # 文件工具共享实现
│       ├── tools/shell/           # 命令解析、项目探测与验证
│       ├── tools/web/             # 搜索源、网络边界与网页来源
│       ├── skills/                # 阶段依赖图、注册表及 7 个内置 Skill
│       ├── rules/                 # 用户、项目及目录作用域 AGENTS.md
│       ├── mcp/                   # stdio / Streamable HTTP 工具与资源
│       ├── lsp/                   # 异步定义、引用、拉取诊断与结果分页
│       └── workflows/             # 项目斜杠工作流
└── app/                           # L5 CLI 组合根、交互和评估入口
    ├── cli.py  command_input.py  run_queue.py
    ├── terminal_view.py  session_picker.py  onboarding.py
    ├── *_commands.py  attachments.py
    ├── desktop_pet.py  pet_window.py
    └── evals/                     # 产品、Python 与源码开发仿真
```

会话持久化入口在 `core/persistence/session.py`；`core/state/` 负责日志与跟踪。任务及项目知识由 `core/memory/` 管理，应用状态不写入用户项目的 `agent_docs/`。

## 二、运行流程

1. [app/cli.py](src/genesisai/app/cli.py) 选择工作区、恢复或创建 Session、配置模型与权限，并组装 ToolRuntime 和 Runner。
2. [agent/loop.py](src/genesisai/agent/loop.py) 经 [ports.py](src/genesisai/agent/ports.py) 调用宿主服务，推进任务、工具调用、验收和恢复。
3. [context_budgeter.py](src/genesisai/core/prompt/context_budgeter.py) 组装政策、任务、规则、Skill、项目目录和历史，记录来源清单并按模型预算投影。
4. [selection.py](src/genesisai/core/tools/selection.py) 按任务、Skill、已发现工具及预算选择本轮定义；模型用 `discover_tools` 查找更多能力。Catalog 管理可用性，不负责全量下发。
5. [executor.py](src/genesisai/core/tools/executor.py) 校验参数、准备预览、执行授权检查并登记账本；同步工具走受管进程，独立作业通过 [jobs/](src/genesisai/core/jobs/) 提交和收取。模型必须核对实际结果。
6. 工具观察更新任务、变更和验证证据；[completion.py](src/genesisai/agent/completion.py) 与宿主完成检查共同决定最终状态。会话及事件由 [persistence/](src/genesisai/core/persistence/) 保存，CLI 呈现结果。

主会话是单 Agent 决策。独立作业默认并发 4 个、超额排队；冲突写入按路径协调。后台命令可以在 CLI 退出后继续，显式取消会请求停止。未知执行结果不自动重放。

## 三、工具清单与计数口径

当前有 **42 个通过 tool.yaml 声明的工具**，此外运行时还注册 **10 个内置能力管理接口**。这两个数字不包含按配置注册的 LSP、外挂工具及 MCP 发现结果。依赖缺失、禁用配置、权限和任务约束会影响实际可用性；以 `/tools`、`/capabilities` 和 `discover_tools` 为准。

| 分组 | 工具 |
| --- | --- |
| 文件与工程（5） | `read_files`、`list_files`、`search_codebase`、`editor`、`inspect_project` |
| 命令与作业（4） | `run_commands`、`read_job`、`stop_job`、`write_job_input` |
| 网络与价格证据（4） | `fetch_web_content`、`fetch_private_resource`、`render_web_page`、`calculate_price` |
| 记忆与计划（5） | `memory_search`、`memory_read`、`memory_write`、`memory_maintenance`、`plan` |
| 交互、发现与图片（5） | `ask_user`、`attempt_completion`、`discover_tools`、`use_skill`、`read_image` |
| Office（11） | `docx_read/create/edit`、`xlsx_read/create/edit`、`pptx_read/create`、`pdf_read/create/ocr`（斜杠表示同前缀的多个工具） |
| 本地媒体（4） | `media_probe`、`video_frames`、`media_audio`、`audio_transcribe` |
| 长篇内容（4） | `chapter_list`、`chapter_read`、`chapter_outline`、`consistency_check` |

能力管理接口由 [core/capabilities/tools.py](src/genesisai/core/capabilities/tools.py) 注册：

`create_tool`、`create_skill`、`create_mcp`、`inspect_capabilities`、`read_capability_artifact`、`revise_capability`、`freeze_capability`、`test_capability`、`try_capability`、`register_capability`。

LSP 配置有效时注册 `lsp_query`、`read_lsp_result`、`cancel_lsp_query`；当前查询操作为 definition、references、diagnostics 和 capabilities，不包含补全、重命名或格式化。MCP 的工具和资源接口在显式连接后发现。

## 四、建议阅读顺序

| 关注点 | 入口 | 重点 |
| --- | --- | --- |
| 契约与配置 | [shared/messages.py](src/genesisai/shared/messages.py)、[model/config.py](src/genesisai/model/config.py)、[agent/ports.py](src/genesisai/agent/ports.py) | 消息、模型及循环依赖的服务 |
| 主循环 | [agent/loop.py](src/genesisai/agent/loop.py)、[recovery.py](src/genesisai/agent/recovery.py) | 开始、取消、确认、恢复、真实进展 |
| 完成与验收 | [agent/completion.py](src/genesisai/agent/completion.py)、[core/development/policy.py](src/genesisai/core/development/policy.py)、[memory/tasks.py](src/genesisai/core/memory/tasks.py) | 修改版本、实际验证、未完成项 |
| 工具管线 | [tools/tool_runtime.py](src/genesisai/core/tools/tool_runtime.py)、[tools/executor.py](src/genesisai/core/tools/executor.py) | 组装、授权、账本与结果交付 |
| 后台任务 | [jobs/service.py](src/genesisai/core/jobs/service.py)、[jobs/write_coordination.py](src/genesisai/core/jobs/write_coordination.py) | 独立宿主、取消、未知状态与写入竞争 |
| 上下文与规则 | [prompt/context_budgeter.py](src/genesisai/core/prompt/context_budgeter.py)、[extensions/rules/](src/genesisai/core/extensions/rules/) | 完整原文、预算、来源及目录作用域 |
| 持久化与记忆 | [persistence/session.py](src/genesisai/core/persistence/session.py)、[memory/service.py](src/genesisai/core/memory/service.py) | SQLite 事务、来源失效、检索和分页 |
| 外挂生命周期 | [capabilities/service.py](src/genesisai/core/capabilities/service.py)、[capabilities/test_suite.py](src/genesisai/core/capabilities/test_suite.py) | 草稿、冻结、真实测试、用户验收 |
| 实际工具 | [kernel/editor/](src/genesisai/core/extensions/tools/kernel/editor/)、[kernel/run_commands/](src/genesisai/core/extensions/tools/kernel/run_commands/) | YAML、Spec、实现及标准观察 |
| 验证 | [tests/](tests/)、[app/evals/](src/genesisai/app/evals/) | 单元/集成回归、离线仿真和显式真实模型验收 |

## 五、修改文档时的核对点

- 工具名称、参数和执行方式以 `tool.yaml`、`spec.py`、实现及动态注册代码为准。
- 能力存在、当前依赖可用、真实模型验收通过是不同层次；测试报告必须保留日期、环境及跳过项。
- 默认配置是应用设置，不是供应商规格；未知模型的图片能力不能推定为支持。
- 命令授权与进程隔离不提供操作系统沙箱。Office 结构重开不等于视觉验收，抽样帧不等于完整视频理解。
- 历史设计与审计不作为现行使用说明；文档职责见[文档导航](docs/README.md)。
