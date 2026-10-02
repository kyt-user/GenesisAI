# GenesisAI 升级路线

日期：2026-09-10
状态：P0、P1、P1.1 已形成当前基线；P2～P6 任务集与阶段验收标准已编写；P7 作为独立产品仿真总验收；代码实施尚未开始
适用范围：GenesisAI 从“网络搜索与本地资料整理 CLI”升级为“面向程序员的本地单 Agent CLI”

## 1. 文档目的

本文定义 GenesisAI 下一轮升级的产品边界、总体架构、核心模块、实施顺序和验收标准。后续代码改造应以本文为主线；具体阶段开始前，可以再拆分为任务清单和测试清单。

本轮升级不修改 `Project/xiaoerAI/`。xiaoerAI 继续作为只读参考源，只迁移适合单 Agent 架构的实现思想或代码，并在 GenesisAI 内重新适配。

本文具体参考 [xiaoerAI 升级路线](../../xiaoerAI/docs/升级路线.md) 中已经形成闭环的可观测性、评测体系、模型治理、Search 深化、任务恢复和证据验证思路。GenesisAI 只迁移这些工程原则，不迁移门童、掌柜、Cooker、DAG 或多 Agent 消息协议；所有模型决策仍由同一个 Agent Runner 完成。

## 2. 产品定位

GenesisAI 第一阶段产品定位为本地优先的 Python CLI 开发助手。用户通过自然语言提出任务，由一个 Agent 在受控权限内理解项目、选择工具、修改文件、运行验证、搜索网络并交付可审查的结果。

主要用户是需要在终端中完成以下工作的程序员：

- 分析代码仓库和定位实现；
- 创建、读取、搜索、修改和整理文件；
- 修复缺陷、补充功能、重构和处理测试失败；
- 执行构建、测试、格式检查等开发命令；
- 查看 Git 状态和变更差异；
- 搜索公开网络资料并保留来源；
- 读取和整理常见办公文件；
- 保存项目约定、架构决策、已验证命令和任务状态；
- 通过 Skills 复用稳定的任务流程。

GenesisAI 保持单 Agent 设计。这里的单 Agent 指一个主 Agent 使用 ReAct 循环完成思考、选择工具、执行、观察、验证和回答，不引入 xiaoerAI 的多角色分派、独立研究 Agent 或 Agent 间通信。

## 3. 本轮边界

### 3.1 纳入范围

1. Python CLI 交互和非交互任务执行。
2. OpenAI 兼容模型协议。
3. Tool Catalog、工具发现和按需加载。
4. 代码仓库搜索、读取、补丁修改、受控命令执行和 Git 查看。
5. JSON 与 Markdown 组成的轻量 Memory。
6. 内置、用户级和项目级 Skills。
7. 常见代码、配置、文本和办公文件读取。
8. 权限分级、确认、取消、恢复、审计和结果验证。
9. 网络搜索、网页读取和来源登记。

### 3.2 暂缓范围

- 多 Agent 编排、角色协作、DAG 和后台任务调度；
- SQLite、服务端数据库和向量数据库；
- 将整个项目复制或转换成 Markdown 知识库；
- Embedding、向量检索、重排器和完整 RAG 管线；
- 原生 Anthropic、Gemini 等非 OpenAI 兼容协议；
- 多模型自动路由和隐式模型降级；
- Web UI、云端账号体系和团队管理；
- 无限制 Shell、工作区外任意访问和静默高风险操作；
- 扫描版文档 OCR、复杂 PDF 编辑和完整 Office 排版还原。

## 4. 当前基线与升级目标

当前 GenesisAI 已具备：

- Python CLI 和单 Agent ReAct 循环；
- DeepSeek 真实模型调用及 OpenAI 兼容客户端基础；
- 文件列举、搜索、读取、创建和复制；
- 网络查询、网页正文抓取和来源登记；
- TXT、Markdown、CSV、JSON、DOCX、XLSX 和文本 PDF 提取；
- 会话 JSON 持久化、确认、取消、预算和安全检查；
- 本地资料与网络信息融合生成 Markdown 报告。

这里的“具备”只表示代码路径和确定性测试存在，不等于真实模型已经达到产品可用标准。2026-09-10 的 DeepSeek CLI 验收暴露了以下事实：

| 场景 | 实际结果 |
| --- | --- |
| 用户任务 | 查询当天苹果发布会产品，并在同一 Session 追问 |
| 模型与工具开销 | 首轮 13 次模型调用、23 次工具调用，累计 103009 Token |
| 搜索行为 | 重复抓取首页和搜索结果页，出现 403、超时、RSS 不支持和无效工具参数 |
| 最终状态 | `finish_reason=length`，没有向用户交付答案 |
| 连续追问 | 消息和 Active Tool 已延续，但模型继续抓取，没有利用已有证据收敛回答 |
| 验收结论 | P1 机制测试通过，真实模型产品验收失败，P1 不得标记最终完成 |

这次失败说明：脚本化 FakeModel 只能验证协议和状态机，不能替代真实模型对 Prompt、工具描述、任务预算、停止条件和失败恢复的端到端验证。

当前实现与目标之间的主要差距：

- 文件工具偏向资料整理，缺少面向代码开发的精确修改能力；
- 没有受控 Shell、构建、测试和 Git 工具；
- P1 已完成工具目录发现与按需加载；代码开发所需的 Patch、Shell、测试和 Git 工具留待 P2；
- 没有独立 Memory 模块，现有 JSON 主要是会话历史；
- 没有 Skills 注册、匹配和执行机制；
- P1 已将工具职责拆为 Registry、Catalog、Loader、Permission、Ledger、Executor 和 Runtime；
- Prompt 仍是单文件通用说明，没有按任务类型组合的协议、明确的搜索停止条件和异常收敛规则；
- Tool Runtime 只有总轮次和总工具数上限，缺少按任务类型划分的查询、抓取、重试、来源和 Token 预算；
- 工具结果原文不断进入模型上下文，缺少 Evidence Bundle、结果去重、片段选择和滚动任务摘要；
- `finish_reason=length`、部分回答、错误工具参数和多次网络失败没有形成可交付的恢复路径；
- 缺少可复现的真实模型评测集、调用指标和“必须产生最终答案”的发布门禁；
- Office 文件以读取为主，创建和局部修改能力尚未分级；
- 当前仍保留 Ollama、百炼等适配代码，与“首版只支持 OpenAI 兼容协议”的产品范围需要统一。

## 5. 总体架构

```text
CLI
 │
 ▼
Application / Task Service
 │
 ├── Workspace Context
 ├── Session State
 ├── Memory Retrieval
 ├── Skill Selection
 ├── Task Profile / Budget
 └── Tool Selection
 │
 ▼
Single Agent Runner
 │
 ├── Prompt Composer / Task Protocol
 ├── Context Builder / Budgeter
 ├── ReAct Loop
 ├── Model Client
 ├── Evidence / Progress State
 └── Completion & Recovery Validator
 │
 ▼
Tool Runtime
 │
 ├── Tool Catalog / Registry / Loader
 ├── Permission Policy
 ├── Executor
 └── Audit Ledger
 │
 ├── Filesystem Tools
 ├── Development Tools
 ├── Git Tools
 ├── Web Tools
 ├── Document Tools
 └── Memory Tools

Observability / Evaluation
 ├── Session、Run、Model Call、Tool Call Trace
 ├── Token、耗时、错误、重试和确认事件
 └── FakeModel 回归 + 真实模型任务集
```

模块之间遵循以下原则：

- CLI 只负责输入、显示和用户确认，不直接绕过执行层操作资源。
- Runner 是唯一模型决策循环，Tools 和 Skills 内不建立第二套 Agent 循环。
- Workspace 中的真实文件是事实来源，Memory 只保存经过筛选的长期知识。
- Tool Catalog 决定哪些工具可以被发现，Permission Policy 决定某次调用是否允许执行。
- 模型正常返回不等于任务完成；Completion Validator 必须结合工具状态、测试结果和产物状态判断。
- Prompt 负责向模型表达任务协议，Runtime 负责确定性执行预算、权限、去重和终止条件；安全与资源限制不能只依赖 Prompt。
- 同一个 Agent 可以使用不同任务协议，但不能在 Skill、Tool、Prompt 或评测代码中建立隐藏的第二模型循环。
- 每个 Run 必须能够回答“执行到了哪里、消耗了多少、为什么继续、为什么停止、交付了什么”。

## 6. 核心执行逻辑

一次任务按照以下流程执行：

1. CLI 接收用户请求、项目路径、权限模式和可选模型配置。
2. Application 建立或恢复会话，确定工作区边界。
3. Application 建立 Task Profile，区分普通问答、网络核实、资料整理、代码修改等任务，并选择对应协议和确定性预算。任务类型不是权限判断，模型不能借此扩大能力。
4. Prompt Composer 只组合当前任务需要的身份、政策、任务协议、工具规则、Skill 和完成契约，不把所有 Prompt 文件整体发送。
5. Context Builder 按预算加载当前用户输入、必要会话历史、相关 Memory、任务进度、Evidence 引用和 Active Tools；大块原文保存在本地，只注入当前需要的片段。
6. Tool Selector 根据任务和 Skill 预选少量工具；无法确定时只提供目录查询能力。
7. Runner 调用同一个模型。模型可以直接回答、调用已加载工具，或者请求加载新工具。
8. Tool Runtime 校验工具名称、参数、路径、权限、超时、去重键和分类预算。Prompt 提出的限制必须由这里或专用控制器执行，不能仅靠模型自觉。
9. 需要确认的调用冻结参数并等待用户；网络确认应允许用户选择仅本次调用或当前 Session 后续同类调用。
10. 工具结果先形成结构化 Observation；网络正文进入 Source Store，并抽取有限 Evidence，而不是每轮重复携带整页正文。
11. Progress State 记录已知事实、证据缺口、失败来源、已用预算和下一步理由。重复 URL、重复内容和已经失败的同一请求不得无边界重试。
12. 每轮结束后 Completion Validator 先判断是否已经满足用户问题、是否达到停止条件、是否仍有可行动的证据缺口，再决定继续调用模型或工具。
13. 文件发生修改后，Agent 查看 diff，并运行与改动相关的验证；研究任务达到证据标准后立即进入回答阶段。
14. 模型输出截断、参数错误或 Provider 暂时失败时进入有次数上限的恢复流程；已有证据足以回答时必须交付带限制说明的结果。
15. CLI 展示最终结果、来源、变更文件、验证命令、失败限制、产物路径和实际 Token/工具用量。
16. 系统保存 Session 与 Run 状态；符合写入规则的内容才进入项目 Memory。

核心状态转换固定为：

```text
用户输入
→ 任务协议与预算
→ 有界上下文
→ 模型决策
→ 工具执行 / Observation
→ Progress 与 Evidence 更新
→ 完成或恢复判断
   ├─ 已满足：生成最终答案
   ├─ 有明确缺口且预算充足：继续
   └─ 达到预算或无法继续：基于已有证据交付受限答案
```

## 7. 模型与配置

### 7.1 协议范围

第一阶段只维护一个 `OpenAICompatibleClient`。DeepSeek 等厂商通过 Provider 预设补充以下差异：

- 默认 API Base URL；
- API Key 环境变量名称；
- 推理字段和响应格式差异；
- Tool Calling、流式响应和 JSON 输出能力；
- 超时、重试和错误归类建议值。

不进行跨 Provider 自动切换。配置错误或模型不可用时，应直接报告实际错误。

### 7.2 用户配置

`config/model.yaml` 只保留用户通常需要修改的通用字段：

```yaml
provider: deepseek
model: deepseek-v4-flash

generation:
  temperature: 0.6
  max_tokens: 2048
```

推荐配置优先级：

```text
命令行参数
  > 项目 .genesis/config.yaml
  > 用户 ~/.genesisai/config.yaml
  > GenesisAI 默认值
```

模型密钥由进程环境变量或 `.env` 提供，不写入 YAML、Memory、Trace 和会话摘要。现阶段继续兼容工作区根目录 `.env`，并允许项目局部 `.env` 补充尚未设置的变量。

`generation.max_tokens` 只控制单次模型输出，不能充当整个 Run 的 Token 预算。总模型调用次数、累计 Token、工具调用次数和分类预算由 Agent Runtime 统一限制。默认参数必须通过真实 DeepSeek 任务集验证；不能只根据单轮聊天效果确定温度和输出长度。

## 8. Tool Catalog 与按需加载

### 8.1 设计目标

GenesisAI 不在每次模型请求中发送所有工具的完整定义。工具先注册到目录，由 Agent 根据当前任务查询和加载实际需要的工具。

受 OpenAI 兼容 Tool Calling 协议限制，模型只能直接调用当前请求中已经声明的工具。因此按需加载采用两阶段流程：

```text
模型获得核心目录工具
  → 查询候选工具
  → Runner 加载候选工具完整 Schema
  → 下一次模型调用使用具体工具
```

### 8.2 工具分层

| 层级 | 含义 | 生命周期 |
| --- | --- | --- |
| Core | 工具搜索、查看、加载及任务控制 | 始终可用 |
| Active | 当前任务已经加载的具体工具 | 当前任务内缓存 |
| Available | 已注册但尚未发送给模型的工具 | 需要时加载 |
| Disabled | 被配置、平台或依赖禁用的工具 | 不可调用 |

始终可见的 Core 工具固定为三个：

- `tool_search`：按名称、分类和描述搜索目录；
- `tool_describe`：查看候选工具的用途、参数概览和风险；
- `tool_load`：请求 Runner 在下一轮加载具体工具。

P1 不按用户输入自动猜测和预加载业务工具。Agent 必须显式调用 `tool_load`，加载结果从下一轮模型请求开始生效。

### 8.3 工具目录结构

```text
src/genesisai/tools/
├── registry.py
├── catalog.py
├── loader.py
├── executor.py
├── permissions.py
├── ledger.py
├── runtime.py
├── core/
├── filesystem/
├── development/
├── git/
├── web/
├── documents/
└── memory/
```

每个具体工具拥有独立目录和 `tool.yaml`：

```text
filesystem/file_read/
├── tool.yaml
├── spec.py
└── implementation.py
```

每份 `tool.yaml` 必须且只能声明四个字段：

- `name`：与目录一致的唯一工具名；
- `enabled`：是否允许加载；
- `category`：工具分类；
- `description`：与真实能力一致的简短说明。

工具不设置版本字段，也不设置顶层 `catalog.yaml`。参数 Schema、权限、路径、网络、副作用、超时和平台限制由同目录 `spec.py` 固定；Python 实现入口由目录约定推导，不允许 YAML 指向任意模块。Registry 启动时扫描 `tool.yaml` 和 Spec，严格校验后建立内存目录。

### 8.4 预选和回退

P1 中 Agent 使用 `tool_search`、`tool_describe` 和 `tool_load` 完成显式发现，Active Tools 上限固定为 8。P4 接入 Skills 时可以在不绕过 Catalog 和 Loader 的前提下增加 Skill 推荐，具体规则在 P4 文档确定。

工具被加载不代表调用自动获准。每一次具体调用仍必须通过权限检查。

## 9. Tools 能力规划

### 9.1 文件与代码

- `file_list`：受控列举、分页和过滤；
- `file_search`：文件名和文本搜索；
- `file_read`：按范围读取；P2 再增加代码行号；
- `file_create`：在输出目录创建新文件并禁止覆盖；
- `file_write`：明确授权后的整体写入，后续阶段实现；
- `apply_patch`：小范围、可审查的代码修改；
- `file_copy`：复制资料和生成副本；
- `file_move`：后续在明确确认下提供；
- `file_delete`：不作为首批默认工具。

文件修改必须保留原有编码、BOM、换行符和权限。写入前记录内容哈希，执行前再次核对，防止覆盖用户或其他进程的并发修改；写入后生成 diff 或等价变更摘要。

### 9.2 开发命令

- `shell_run`：执行受控命令；
- `test_run`：运行项目测试并结构化提取结果；
- `build_run`：运行已识别的构建命令；
- `lint_run`：运行格式或静态检查命令；
- `process_cancel`：终止由当前任务启动的长时间进程。

Shell 必须指定工作目录、超时和输出上限，默认限定在项目内。执行器应优先使用参数数组，避免不必要的命令字符串拼接。命令的退出码、标准输出、标准错误、耗时和截断状态必须真实记录。

### 9.3 Git

- `git_status`：查看工作区状态；
- `git_diff`：查看未暂存、已暂存或指定范围变更；
- `git_log`：读取有限历史；
- `git_show`：读取指定提交；
- `git_branch`：只读分支信息；
- `git_commit`：后续作为需要明确授权的写操作。

首批 Git 工具以只读查看为主。切换分支、重置、清理、推送和强制操作不进入默认自动执行范围。

### 9.4 网络

- `search_query`：查询公开搜索服务；
- `search_fetch`：抓取并提取公开页面；
- `web_download`：后续按文件类型和路径策略提供。

搜索结果必须登记标题、最终 URL、查询时间、访问时间和提取状态。网页内容默认只进入当前上下文；只有稳定结论或用户明确要求保存的内容才进入 Memory。网页和下载内容作为不可信资料处理，不能提升工具权限。

参考 xiaoerAI 的 Search 深化设计，GenesisAI 的单 Agent 网络研究必须采用受预算和证据契约约束的固定链路：

```text
问题与时效判断
→ search_query 生成候选
→ URL 规范化、域名与内容去重
→ search_fetch 读取少量候选正文
→ Evidence Bundle 更新
→ 缺口与冲突判断
→ 补检或最终回答
```

第一版规则：

- `search_fetch` 只接受用户明确给出的 URL，或本 Run 中 `search_query` 返回并登记的候选 URL；禁止模型自行拼接 Bing、DuckDuckGo 等搜索结果页 URL 代替 `search_query`；
- 同一规范化 URL 最多抓取一次，同一内容哈希或 SimHash 只保留一份，同一域名默认最多选择两份有效来源；
- 403、404、不支持格式和正文为空属于该候选失败，不对同一 URL 重试；超时和 5xx 最多重试一次；
- 官方来源能够直接回答时优先使用官方来源；重要事实需要至少一个权威来源，存在争议时再增加独立来源；
- 达到证据标准后立即停止。预算耗尽时基于已读证据回答，并明确哪些信息尚未核实，不允许为了追求“更多来源”无限抓取；
- 搜索候选不能直接作为事实证据，只有实际读取并通过来源校验的正文才能进入 Evidence Bundle；
- 引用必须绑定已登记的 `source_ref`，最终答案不能引用模型自行编造或未读取的 URL。

默认研究预算按任务复杂度分级，数值属于 Runtime 策略，不进入工具 YAML：

| 类型 | 适用场景 | 查询上限 | 正文抓取上限 | 有效来源目标 | 模型轮次上限 |
| --- | --- | ---: | ---: | ---: | ---: |
| 快速核实 | “今天是否发布”“某版本是否存在” | 2 | 4 | 1～4 | 8 |
| 常规研究 | 比较产品、整理专题 | 4 | 8 | 2～8 | 12 |
| 深度研究 | 用户明确要求全面调研 | 8 | 12 | 4～12 | 20 |

默认采用快速核实或常规研究；只有用户明确提出深度调研时才能使用深度预算。一次任务还必须有累计 Token 和时间上限，任何分类预算都不能突破总预算。

Evidence Bundle 至少记录：事实或结论、`source_ref`、对应正文片段、发布时间或访问时间、来源类型、是否存在冲突、提取状态和内容哈希。模型负责提出事实，确定性 Verifier 负责确认引用存在、来源实际读取且引用关系合法。

### 9.5 办公文件

首轮目标按能力分级：

| 格式 | 读取 | 生成 | 修改 |
| --- | --- | --- | --- |
| TXT/Markdown/JSON/CSV | 完整支持 | 完整支持 | 完整支持 |
| DOCX | 支持正文、标题、表格 | 基础生成 | 基础段落与表格修改 |
| XLSX | 支持工作表、单元格和公式结果 | 基础生成 | 单元格和工作表级修改 |
| PPTX | 支持文本和页面结构 | 后续 | 后续 |
| PDF | 支持文本型 PDF | 通过其他格式导出，后续完善 | 暂不支持 |

办公文件按需解析，原始文件仍是事实来源。解析结果属于当前工具输出，不自动转换成永久 Markdown 文件。值得长期保存的结论可以经过筛选后写入 Memory。

## 10. Memory：JSON 与 Markdown 替代数据库

### 10.1 定位

第一阶段以本地 JSON 和 Markdown 构建轻量 Memory，承担数据库在当前产品中的结构化保存、检索、恢复和人工审查作用。

Memory 不等于项目镜像，也不等于把所有文件转换成 Markdown。Agent 需要代码或文档原文时，应通过文件工具读取工作区当前内容。

JSON 负责机器可读的结构化数据：

- Schema 版本；
- 关键目录和关键文件索引；
- Memory 文档清单；
- 来源路径、内容哈希和更新时间；
- 任务、会话及写入状态；
- 标签、优先级、可信度和失效标记。

Markdown 负责模型和用户可直接阅读的长期内容：

- 项目概况；
- 架构说明；
- 编码约定；
- 构建和测试命令；
- 已确认决策；
- 已解决问题；
- 必要的原文片段及来源。

### 10.2 推荐目录

```text
<project>/.genesis/
├── config.yaml
├── memory/
│   ├── index.json
│   ├── files.json
│   ├── project.md
│   ├── architecture.md
│   ├── conventions.md
│   ├── commands.md
│   ├── decisions.md
│   ├── problems.md
│   └── tasks/
└── skills/

~/.genesisai/
├── config.yaml
├── sessions/
├── traces/
├── skills/
└── memory/
```

项目知识放在 `<project>/.genesis/memory/`，便于审查和按需跟随项目；会话全文、运行 Trace 和用户级偏好放在 `~/.genesisai/`，避免污染项目仓库。项目可以通过 `.gitignore` 决定哪些 `.genesis` 内容提交。

### 10.3 写入规则

允许自动保存：

- 会话和任务运行状态；
- 工具调用账本；
- 已经实际验证通过的构建或测试命令；
- 用户明确要求记住的信息。

需要形成候选并确认后保存：

- 架构结论；
- 编码规范；
- 长期项目约束；
- 来自网络的稳定结论；
- 影响后续任务的用户偏好。

不得作为长期 Memory 自动保存：

- 模型未验证的猜测；
- 临时错误输出；
- 密钥、Token 和敏感环境变量；
- 无明确用途的大段网页正文；
- 整个项目文件的 Markdown 副本；
- 模型私有推理文本。

每条长期记忆应记录来源、创建时间、更新时间、相关文件路径、内容哈希和可信状态。来源文件发生变化时，相关记忆标为 `stale`，在重新验证前只能作为历史参考。

### 10.4 检索方式

首轮不引入向量库。Memory 检索使用：

- JSON 字段、标签、类别和路径过滤；
- Markdown 标题和关键词搜索；
- 当前任务、Skill、文件路径和 Git 状态加权；
- 有界 Top-K 结果和上下文长度控制。

工作区检索与 Memory 检索保持分离：工作区工具负责查找当前真实文件，Memory 工具负责查找已经沉淀的项目知识。

### 10.5 数据可靠性

JSON 写入采用临时文件、刷新和原子替换；数据包含 `schema_version`。同一项目只允许一个写入者，或使用文件锁避免并发覆盖。保存前保留最近一次可恢复版本，解析失败时不得用空数据覆盖原文件。

## 11. Skills

### 11.1 职责

Skill 描述一类任务的标准流程、约束、完成条件和推荐 Tools。它影响 Agent 如何执行任务，但不直接获得额外权限。

### 11.2 目录和优先级

```text
src/genesisai/skills/       # 内置
~/.genesisai/skills/        # 用户级
<project>/.genesis/skills/  # 项目级
```

同名 Skill 的优先级为：项目级高于用户级，用户级高于内置。覆盖关系必须在 CLI 中可查看，加载失败不能静默忽略。

### 11.3 Skill 结构

```text
skills/test-fix/
├── SKILL.md
├── skill.yaml
├── templates/
├── examples/
└── scripts/
```

`skill.yaml` 只声明目录检索和确定性加载需要的名称、简介、触发条件、推荐工具、可选工具、平台要求和入口文档，不设置版本字段。`SKILL.md` 描述执行步骤、停止条件、验证方法和输出要求；内容哈希用于检测目录与实例是否一致。

首批内置 Skills：

- `repository-overview`：识别技术栈、入口、构建和测试方式；
- `bug-fix`：复现、定位、最小修改、回归验证；
- `test-fix`：分析失败测试并运行针对性复测；
- `code-review`：检查正确性、安全、兼容性和测试缺口；
- `refactor`：保持行为的结构调整；
- `dependency-upgrade`：查阅官方资料、升级和验证；
- `web-research`：检索、抓取、比较和引用；
- `document-organize`：读取和整理本地资料；
- `project-memory`：生成和维护经过验证的项目记忆。

Skill 中的脚本仍通过 Tool Runtime 执行，不能绕过权限、路径和命令策略。

## 12. Prompt、任务协议与上下文工程

Prompt 是 Agent 产品行为的一部分，必须像代码一样拆分、测试和验收。单纯扩写一个 `system.md` 会造成规则冲突、上下文浪费和模型注意力下降。Prompt 只能指导模型；权限、预算、去重、确认和终止必须由代码确定性执行。

### 12.1 推荐目录

```text
prompts/
├── system/
│   ├── identity.md
│   └── policies.md
├── protocols/
│   ├── direct_answer.md
│   ├── web_research.md
│   ├── local_files.md
│   └── code_change.md
├── tools/
│   ├── catalog_usage.md
│   └── evidence_usage.md
├── context/
│   ├── session.md
│   └── memory.md
└── completion/
    ├── final_answer.md
    └── recovery.md
```

Prompt 文件使用 Markdown，组合关系由受测试的代码映射维护，不再增加一份与代码职责重复的宽松配置。每次请求只装载身份、政策和一个主要任务协议，再补充实际存在的 Skill、Memory 和工具说明。

### 12.2 Prompt Composer

Context Builder 调用 Prompt Composer，按固定顺序组装：

1. 身份、产品边界和单 Agent 约束；
2. 不可被用户、网页或本地文件覆盖的安全政策；
3. 当前 Task Profile、完成目标和确定性预算；
4. 当前任务协议；
5. 当前 Skill；
6. 相关 Memory 和工作区状态；
7. 有界会话历史、Progress State 与 Evidence Bundle；
8. 当前请求实际提供的 Active Tools；
9. 最终回答和失败说明契约。

Composer 必须输出可观测的组成清单和各部分字符或 Token 估算，但 Trace 不保存密钥、完整 Prompt、用户文件正文、网页正文或模型私有推理。

### 12.3 任务协议

| 协议 | 默认行为 | 完成条件 |
| --- | --- | --- |
| `direct_answer` | 已有知识或当前上下文足够时直接回答，不调用工具 | 给出清晰答案，或明确说明缺少实时依据 |
| `web_research` | 先查询候选，再读取少量正文，维护 Evidence Bundle | 核心问题有足够来源，或预算耗尽后给出受限结论 |
| `local_files` | 先定位文件，再按需分段读取，不复制整个项目进上下文 | 用户要求的文件分析或整理结果已经交付 |
| `code_change` | 搜索、读取、最小修改、验证、检查 diff | 修改真实存在、验证已运行、结果与 diff 一致 |

Task Profile 只能选择协议和预算，不直接授予 Tool 或权限。模型可以建议切换协议，Runtime 校验后才能生效；普通问题不能因为模型想“深入研究”就自动升级为深度预算。

### 12.4 网络研究 Prompt 契约

`web_research` 必须明确告诉模型：

- 使用 `search_query` 找候选，不能手工拼接搜索引擎结果页交给 `search_fetch`；
- 每轮开始前说明尚缺哪条证据，已有证据足够时立即回答；
- 优先读取官方或直接来源，不为堆积来源重复抓取同一首页；
- 单个候选失败后选择其他候选，不反复请求同一 URL；
- 搜索结果摘要不是正文证据，最终引用只来自已读取来源；
- 到达查询、抓取、轮次、Token 或时间预算时停止工具调用，基于现有证据回答；
- 已经存在可靠 Evidence 时，用户追问“结果呢”“它的优势是什么”应优先总结，不得默认重新搜索。

这些规则同时由 Research Controller 检查。模型请求违反 URL 来源、去重、重试或预算规则时，Tool Runtime 返回稳定错误和剩余预算，不执行违规请求。

### 12.5 Context Budgeter

模型上下文上限由 Provider 预设提供，Context Budgeter 为最终回答和一次恢复调用预留至少 25%，其余预算分别分配给系统与工具定义、会话历史、Memory、文件片段、网页 Evidence 和当前 Run 的 Observation。任何单项都不得无限挤占其他部分。

必须保留：

- 当前用户输入；
- 上一轮最终回答及其关键来源；
- 当前未完成目标、用户约束和权限边界；
- 待确认调用、已修改文件、验证结果和失败状态；
- 当前任务直接相关的 Evidence 与引用。

应当压缩或引用：

- 已经提取为 Evidence 的网页全文；
- 重复的搜索结果、工具目录结果和工具 Schema；
- 成功完成且与当前追问无关的旧 Observation；
- 长测试输出和构建日志；
- 已保存到本地 Source Store 或 Artifact Store 的大块内容。

工具结果先保存到本地，再生成包含 `result_ref`、摘要、截断状态和可继续读取范围的 Observation。摘要不能取代事实来源；需要引用原文时按 `source_ref` 重新读取有限片段。

长 Session 使用滚动摘要，但最近一轮用户输入、最终回答、有效来源和未完成操作必须保持原始结构。压缩失败时明确要求 `/compact` 或 `/new`，不能悄悄丢失追问所依赖的内容。

### 12.6 完成判断与失败收敛

Completion Validator 不询问第二个 Agent，而是根据当前 Task Profile、Evidence、工具账本和模型输出执行确定性检查：

- 普通问答已有回答时，不允许继续无目的调用工具；
- 快速核实获得官方来源或足够独立证据后，下一步必须进入最终回答；
- 达到任一硬预算后禁止新增工具调用，进入受限答案生成；
- 连续出现相同参数错误、相同 URL 或相同失败类型时提前终止该策略；
- 有可用 Evidence 时，网络局部失败不能让整项任务无回答结束；
- 最终答案必须回答用户问题，并区分已证实事实、合理推断和未核实内容。

恢复规则：

| 异常 | 第一处理 | 最终处理 |
| --- | --- | --- |
| 工具参数 Schema 错误 | 返回缺失或无效字段，只允许一次修正 | 再次同类错误则停止该工具策略并回答或报告 |
| 429、超时、5xx | 同 Provider 或同候选最多重试一次 | 换候选或基于已有证据回答 |
| 403、404、不支持格式、空正文 | 标记候选不可用 | 不重试同一 URL |
| `finish_reason=length` | 保存部分输出，压缩上下文后进行一次禁止新增工具的回答恢复 | 仍截断时交付可读部分并标记未完整，不能丢弃全部结果 |
| 空回答或非法 Tool Call | 一次格式修复 | 失败后返回明确错误和已有进度 |
| 达到总预算 | 禁止新增模型与工具探索 | 输出已有结论、证据和未完成项 |

### 12.7 Prompt 变更治理

- Prompt 文件必须使用中文注释或说明，任务协议中的工具名与正式 Registry 一致；
- Prompt 变更必须运行固定 FakeModel 协议测试、真实模型行为测试和 Prompt Injection 回归；
- 测试应比较完成率、调用次数、Token、耗时、引用合法率和失败收敛，不以“模型返回了文本”作为通过；
- 生产 Prompt 不保存 API Key、用户秘密或测试答案，不用针对单个用例的关键词硬编码结论；
- Prompt 内容哈希可以进入 Trace，用于复现实验，但不为 Prompt YAML 或文件增加版本字段。

## 13. 可观测性、评测与模型治理

### 13.1 可观测性

参考 xiaoerAI 的 Trace 设计，GenesisAI 统一使用：

```text
session_id
run_id
model_call_id
tool_call_id
skill_id（启用 Skill 后）
```

每次 Run 记录开始和结束时间、Provider、Model、Prompt 组成清单、模型调用次数、输入/输出/推理 Token、工具名称和耗时、重试、确认状态、错误类型、Evidence 引用、产物和最终状态。CLI 后续提供 `/trace` 或 `/status` 查看摘要。Trace 只保存脱敏元数据和引用，不保存完整 Prompt、API Key、本地敏感正文或网页全文。

### 13.2 固定评测体系

评测至少包含：

- 无需工具的普通问答是否直接回答；
- 当天事件快速核实是否在预算内完成；
- 第一轮网络回答后的代词追问是否复用 Evidence；
- 本地文件多轮读取是否保持 Session 上下文；
- 工具参数错误、403、超时和不支持格式是否有界收敛；
- `finish_reason=length` 是否保留并恢复可交付答案；
- 网络、写入和远程模型授权是否独立；
- 后续代码任务的修改、测试和 diff 是否真实一致。

核心指标：

```text
任务最终交付率
一次完成率
平均与 P95 模型调用次数
平均与 P95 工具调用次数
累计 Token 和耗时
有效来源数与引用合法率
重复 URL / 重复内容率
错误恢复率
权限误放行率
产物验收通过率
```

评测分为三层：确定性单元测试、FakeModel 协议回归、真实 OpenAI 兼容模型端到端任务。FakeModel 通过不能替代真实模型验收。涉及用户付费 API 的自动运行必须由用户明确授权，但阶段最终验收不能在真实模型完全未验证时宣称“当前可用”。

P1.1 的真实 DeepSeek 最低门槛固定为：

1. “今天苹果是否开发布会、有哪些产品”能够给出最终答案；
2. 快速核实不超过 2 次 `search_query`、4 次 `search_fetch`、8 次模型调用和任务级 Token 上限；
3. 不抓取搜索结果页，不重复抓取相同 URL，不因单个候选失败无限重试；
4. 得到答案后直接追问“其中这个产品的优势是什么”能够复用上一轮回答与 Evidence；
5. 全程没有“当前请求未提供工具”、无回答的 `length` 失败或待确认死循环；
6. 默认 ask 与 Session 级网络允许两种流程都能完成，CLI 明确展示实际权限状态。

### 13.3 模型治理

- 统一 Provider 超时、错误类型和重试语义；
- 每个 Run 设置模型调用、累计 Token、耗时和费用上限；
- 同 Provider 可按错误类型重试一次，第一版不跨 Provider 静默降级；
- 记录模型是否支持 Tool Calling、流式输出、JSON 和 reasoning 字段；
- 根据固定评测结果确定温度、输出长度和 Provider 默认值；
- Provider 不可用时返回 `model_unavailable` 或具体稳定错误，不伪装成任务完成；
- 真实模型评测保存指标和脱敏 Trace，不保存测试账号密钥或完整用户资料。

## 14. 权限与安全

### 14.1 权限模式

| 模式 | 行为 |
| --- | --- |
| `plan` | 只分析和读取，不修改文件、不执行有副作用命令 |
| `ask` | 安全读取自动执行，修改和高风险操作逐次确认 |
| `auto` | 在明确工作区和允许规则内自动执行低风险修改 |

默认使用 `ask`。权限决策顺序采用：明确拒绝规则、需要确认规则、明确允许规则；没有匹配时按照工具风险等级处理。

### 14.2 必须独立控制的能力

- 工作区内读取；
- 工作区内写入；
- 删除和移动；
- Shell 执行；
- 网络访问；
- Git 写操作；
- 工作区外路径访问；
- 本地资料发送到远程模型。

用户输入和资料内容不能提升权限。工具调用确认必须绑定工具名称、规范化参数、资源状态和过期时间；参数或目标文件变化时重新确认。

公开网络请求的默认模式可以保持 `ask`，但确认面板必须同时提供“仅批准当前调用”和“允许当前 Session 后续公开网络调用”。Session 级允许不能授权本地资料外发、文件写入或其他能力，退出 Session 后不成为全局默认。

## 15. CLI 产品能力

保留当前 `/help`、`/status`、`/history`、`/resume`、`/approve`、`/reject`、`/cancel` 和 `/exit`，逐步增加：

- `/new`：创建新会话或任务；
- `/model`：查看当前模型和能力；
- `/permissions`：查看或切换权限模式；
- `/tools`：查看 Core、Active、Available 和 Disabled Tools；
- `/skills`：查看、选择和解释当前 Skill；
- `/memory`：查看、添加、更新、忘记和重新验证记忆；
- `/diff`：查看本任务修改；
- `/review`：在结束前执行变更审查；
- `/compact`：压缩长会话上下文；
- `/trace`：查看当前 Run 的模型、工具、Token、错误和恢复摘要；
- `/budget`：查看任务类型、分类预算、已消耗量和剩余量；
- `/artifacts`：查看生成产物。

CLI 应显示真实状态：当前任务、模型、权限模式、活动 Skill、已加载工具、运行命令、修改文件、验证结果、待确认操作和 Token 使用量。未知数据保持未知，不估算成确定值。

## 16. 建议项目结构

```text
Project/GenesisAI/
├── cli.py
├── pyproject.toml
├── config/
│   └── model.yaml
├── prompts/
├── src/genesisai/
│   ├── application.py
│   ├── runner.py
│   ├── context.py
│   ├── prompts.py
│   ├── progress.py
│   ├── evidence.py
│   ├── completion.py
│   ├── models/
│   ├── tools/
│   ├── skills/
│   ├── memory/
│   ├── workspace/
│   ├── sessions/
│   ├── artifacts/
│   ├── evaluation/
│   └── observability/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── acceptance/
│   ├── manual/
│   └── fixtures/
└── docs/
```

目录调整应渐进进行。先提取接口和职责，再迁移现有实现，避免一次性重写已经通过验收的路径安全、确认和恢复逻辑。

## 17. 分阶段实施路线

P2、P3、P4、P5、P6 保持独立阶段、任务集和验收标准，但在同一个开发周期内连续实施。每阶段完成后自动执行并回填阶段验收；失败先修复，不等待用户逐阶段确认。P6 通过后进入独立 P7，使用五类仿真数据集执行离线与真实 DeepSeek 总验收，最后由用户一次性决定 V1 CLI 产品基线是否完成。

### P0：冻结基线与清理范围（完成）

工作内容：

- 运行当前离线测试并保存基线；
- 标记现有功能与本轮范围；
- 清理未使用的多 Agent 搜索契约和重复抽象；
- 明确只支持 OpenAI 兼容协议后的 Provider 保留策略；
- 给持久化 JSON 增加 Schema 版本和迁移入口。

退出条件：现有文件整理和网络搜索场景保持通过，源码职责和升级迁移清单明确。

### P1：Tool Runtime 拆分（机制回归通过）

工作内容：

- 从当前 `ToolExecutor` 提取 Registry、Catalog、Loader、Permission、Ledger、Executor 和 Runtime；
- 为三个 Core 和七个业务工具建立严格四字段 `tool.yaml`，不设置工具版本和顶层 `catalog.yaml`；
- 实现工具目录扫描、校验、启用和禁用；
- 实现 Core、Active、Available、Disabled 四层工具集合；
- 改进当前七个工具，使声明、权限、参数、结果和实际行为与仓库定义一致。

原定退出条件：模型初始请求只携带三个 Core 工具；Agent 能查询目录、加载工具并完成原有任务。

补充整改要求：同一 Session 中的普通输入必须共享消息、来源与 Active Tools；只有显式 `/new` 才建立新 Session 并清空工具集合。网络与写入权限保持独立安全默认，同时支持当前 Session 内切换确认模式。

当前结论：Registry、Catalog、Loader、Permission、Ledger、Executor、Session/Run 和 Active Tools 的确定性测试已经通过；真实模型收敛问题已纳入并由 P1.1 整改。

### P1.1：Agent 可用性、Prompt 与真实模型收敛（已实施，纳入最终产品验收）

P1.1 是 P1 的产品可用性补充，代码、自动测试和真实 DeepSeek 记录已经完成。其最终体验结论并入 P7，不再单独阻塞 P2～P6 的连续实施。

- [P1.1 实施计划](temp/P1.1_Agent可用性与Prompt工程实施计划.md)
- [P1.1 验收标准](temp/P1.1_Agent可用性与Prompt工程验收标准.md)

工作内容：

- 将单体 `system.md` 拆为身份、政策、任务协议、工具规则、上下文和完成恢复 Prompt；
- 建立 Prompt Composer、Task Profile、Context Budgeter、Progress State、Evidence Bundle 和 Completion Validator 的清晰接口；
- 为快速核实、常规研究和深度研究设置独立查询、抓取、模型轮次、Token、时间与重试预算；
- 强制网络任务使用 `search_query → 候选登记 → search_fetch`，禁止抓取模型自行拼接的搜索引擎结果页；
- 实现 URL、域名、内容和失败请求去重，达到证据标准后立即停止；
- 将长网页正文和工具输出改为本地引用与有限 Observation，避免原文反复进入每轮上下文；
- 实现工具参数错误、网络失败、空回答和 `finish_reason=length` 的有界恢复；
- 在网络确认面板提供当前调用批准和当前 Session 同类允许两种选择；
- 建立脱敏 Trace、任务预算显示、固定评测集和真实 DeepSeek 端到端验收；
- 重新审查 Prompt 与工具描述，确保简单任务不会反复搜索目录或加载错误工具。

退出条件：

1. 第 13.2 节的真实 DeepSeek 最低门槛全部通过；
2. 固定 FakeModel 回归、故障注入和真实模型评测均通过；
3. 快速网络核实在预算内给出最终答案，不能以无答案的 `length`、工具循环或待确认循环结束；
4. 连续追问优先复用上一轮答案和 Evidence，需要新证据时才继续搜索；
5. Trace 能解释每次模型与工具调用的目的、消耗、错误和停止原因；
6. P1.1 的自动化与真实模型证据完成记录；最终 CLI 体验结论并入 P7 统一验收。

### P2：开发工具闭环

- [P2 任务集](temp/P2_开发工具闭环任务集.md)
- [P2 阶段验收标准](temp/P2_开发工具闭环阶段验收标准.md)

工作内容：

- 增加带行号的代码读取和快速文本搜索；
- 增加 `apply_patch`；
- 增加受控 `shell_run`、`test_run`；
- 增加只读 Git status、diff、log、show；
- 建立修改前哈希检查和修改后 diff；
- 扩展 CLI 的工具进度和变更展示。

退出条件：Agent 能定位一个测试失败，修改代码，运行针对性测试并展示 Git diff。

### P3：Memory 基础设施

- [P3 任务集](temp/P3_Memory基础设施任务集.md)
- [P3 阶段验收标准](temp/P3_Memory基础设施阶段验收标准.md)

工作内容：

- 建立项目级 `.genesis/memory/` 和用户级存储；
- 定义 JSON Schema、原子写入、锁、备份和迁移；
- 实现项目、架构、规范、命令、决策和问题 Markdown；
- 实现 `/memory` 查看、添加、忘记和重新验证；
- 实现来源哈希、失效标记和关键词检索；
- 从现有 SessionStore 迁移会话职责，但不复制全部项目文件。

退出条件：重新启动后能够检索项目约定和已验证命令；来源文件变化时旧记忆被标记待验证。

### P4：Skills

- [P4 任务集](temp/P4_Skills任务集.md)
- [P4 阶段验收标准](temp/P4_Skills阶段验收标准.md)

工作内容：

- 建立 Skill Registry、Loader 和优先级；
- 支持内置、用户级和项目级 Skill；
- 将 Skill 推荐工具接入 Tool Selector；
- 实现仓库分析、Bug 修复、测试修复、代码审查和网络调研等首批 Skill；
- 在 CLI 展示当前 Skill 的来源、内容哈希和工具依赖。

退出条件：同一个开发任务在指定 Skill 下按照规定步骤执行，Skill 无法绕过权限。

### P5：文件格式和办公能力

- [P5 任务集](temp/P5_文件格式与办公能力任务集.md)
- [P5 阶段验收标准](temp/P5_文件格式与办公能力阶段验收标准.md)

工作内容：

- 统一文本编码、换行和文件类型识别；
- 扩展常见代码和配置格式；
- 将 DOCX、XLSX、PPTX、PDF 拆分为独立按需工具；
- 增加基础 DOCX、XLSX 生成和局部修改；
- 保留格式限制、解析失败和产物验证信息。

退出条件：常见代码文件可安全修改，办公文件能力符合第 9.5 节矩阵，未支持的操作明确拒绝。

### P6：上下文、可靠性与产品化

- [P6 任务集](temp/P6_上下文可靠性与产品化任务集.md)
- [P6 阶段验收标准](temp/P6_上下文可靠性与产品化阶段验收标准.md)

工作内容：

- 扩展跨长 Session 的自动压缩、摘要重建和人工 `/compact`；
- 在 Memory、Skills、开发工具和 Office 工具加入后重新平衡上下文预算；
- 完善跨进程任务恢复、撤销信息和长期 Trace 管理；
- 增加安装升级、诊断命令、日志轮转和空间清理；
- 扩展真实模型、真实项目和跨平台验证矩阵。

退出条件：长任务可恢复，工具和 Memory 按需装载，失败状态可解释，安装与升级流程可复现。

### P7：产品仿真总验收

- [P7 任务集](temp/P7_产品仿真总验收任务集.md)
- [P7 总验收标准](temp/P7_产品仿真总验收标准.md)

工作内容：

- 建立代码修复、文件管理、网络研究、Memory/Skill 和 Office 五类无敏感信息的数据集；
- 通过隔离副本分别执行离线确定性仿真和真实 DeepSeek 仿真；
- 汇总模型、工具、Token、权限、文件、测试、Memory、来源、产物和恢复证据；
- 将缺陷回流到 P2～P6 修复，并重跑受影响案例和全量测试；
- 生成最终产品验收记录，交由用户一次性验收。

退出条件：五个离线案例和五个真实 DeepSeek 案例全部交付，安全与隐私必需项通过，全量测试 0 failed，用户明确确认 V1 CLI 产品基线完成。

## 18. 验收清单

### 18.1 单 Agent 与模型

- [ ] 一个 Runner 完成全部模型决策，没有隐藏的第二 Agent 循环。
- [ ] 所有支持模型通过同一个 OpenAI 兼容客户端调用。
- [ ] 模型配置错误不会隐式改用其他模型。
- [ ] 截断、空答、服务异常和预算耗尽不会显示为成功。

### 18.2 Tools

- [ ] 初始模型请求只携带 `tool_search`、`tool_describe`、`tool_load` 三个 Core 工具。
- [ ] Agent 可以搜索、查看并加载具体工具。
- [ ] 未加载工具不能被直接调用。
- [ ] 禁用、重复、Schema 错误和缺少依赖的工具有明确诊断。
- [ ] Tool 加载不绕过调用权限。

### 18.3 Prompt、研究与上下文

- [ ] Prompt 已按身份、政策、任务协议、工具、上下文和完成恢复拆分，并由 Composer 按需组合。
- [ ] 普通问答无需工具时直接回答，不经过 Tool Catalog 循环。
- [ ] 网络任务只能抓取用户 URL 或 `search_query` 登记的候选 URL。
- [ ] 快速、常规和深度研究的查询、抓取、轮次、Token、时间及重试预算由代码执行。
- [ ] 相同 URL、内容、失败请求和无新增信息的重复策略能够停止。
- [ ] 网页全文和长工具输出使用本地引用与有限 Observation，不在每轮上下文重复发送。
- [ ] 上一轮答案、关键 Evidence 和未完成操作在连续追问与压缩后保持可用。
- [ ] 达到证据标准或硬预算后一定进入最终回答或受限答案，不继续无目的探索。
- [ ] `finish_reason=length`、空回答和工具参数错误具有有界恢复，不丢弃全部已有结果。
- [ ] Prompt Injection 不能改变权限、预算、工具范围、完成条件和系统政策。

### 18.4 可观测性、评测与模型治理

- [ ] 每个 Session、Run、模型调用和工具调用具有稳定关联标识。
- [ ] Trace 能解释调用、耗时、Token、错误、重试、确认、Evidence、产物和最终状态。
- [ ] Trace 不保存 API Key、完整敏感正文、完整 Prompt 或模型私有推理。
- [ ] 评测同时包含确定性测试、FakeModel 协议回归和真实模型端到端任务。
- [ ] 评测报告任务交付率、调用次数、Token、耗时、重复率、引用合法率和恢复率。
- [ ] 真实模型未验证时不得把阶段标记为产品可用。
- [ ] Provider 重试有上限，第一版不跨 Provider 静默降级。
- [ ] 模型输出长度、温度和预算经过固定真实任务集验证。

### 18.5 开发任务

- [ ] 能识别仓库结构、入口、依赖、构建和测试命令。
- [ ] 能精确搜索和分段读取代码。
- [ ] 能用补丁完成小范围修改，并防止覆盖并发变更。
- [ ] 能运行针对性测试，记录退出码和完整状态。
- [ ] 能展示本任务产生的 Git diff。
- [ ] 不能把未运行测试描述为测试通过。

### 18.6 Memory

- [ ] Memory 使用 JSON 索引和 Markdown 内容，不依赖数据库。
- [ ] 不批量转换或复制项目文件为 Markdown。
- [ ] 项目文件仍通过工具实时读取。
- [ ] Memory 可以跨会话检索。
- [ ] 长期记忆包含来源和更新时间。
- [ ] 来源变化时相关记忆标记为待验证。
- [ ] 密钥、Token 和模型私有推理不会进入长期 Memory。
- [ ] JSON 异常或中断写入不会破坏已有数据。

### 18.7 Skills

- [ ] 内置、用户级和项目级 Skill 可以发现和加载。
- [ ] 同名 Skill 的优先级稳定且可查看。
- [ ] Skill 能推荐工具并影响任务流程。
- [ ] Skill 不能扩大文件、网络或命令权限。
- [ ] Skill 缺失、格式错误和依赖缺失有明确反馈。

### 18.8 权限和恢复

- [ ] `plan`、`ask` 和 `auto` 行为符合定义。
- [ ] 文件写入、Shell、网络、Git 写操作和外部路径分别控制。
- [ ] 确认绑定具体工具、参数和资源状态。
- [ ] 重启不会自动重放结果未知的副作用操作。
- [ ] 取消和超时能够留下可解释状态。
- [ ] 网络确认可以明确选择单次批准或当前 Session 同类允许，且不会扩大其他权限。

### 18.9 文件与网络

- [ ] 常见代码和配置文件保留编码、换行和权限。
- [ ] Office 文件按照能力矩阵读取或修改。
- [ ] 扫描 PDF、复杂排版等未支持场景明确说明限制。
- [ ] 网络结果保留最终 URL 和访问时间。
- [ ] 搜索候选、已读取正文和最终引用可以确定性关联。
- [ ] 网络研究符合分类预算、去重、失败重试和证据停止规则。
- [ ] 网页内容不能改变系统权限或充当用户授权。

## 19. 核心端到端验收场景

### 19.1 P1.1 真实网络与连续追问

在进入 P2 前必须完成：

```text
使用真实 DeepSeek 启动 CLI
→ 用户询问“今天苹果是否开了发布会、有哪些产品”
→ Agent 在快速核实预算内查询、读取和去重
→ Agent 交付带合法来源的最终答案
→ 用户直接追问其中一个产品的主要优势
→ Agent 复用上一轮答案和 Evidence，必要时只补充最少新证据
→ Agent 交付追问答案
```

该场景必须同时在默认 `ask` 和当前 Session 网络允许模式下验证。任何一次出现无答案的 `length` 失败、重复 URL 循环、抓取搜索结果页、持续等待确认或未提供工具错误，P1.1 均不得通过。

### 19.2 最终开发 CLI 场景

最终核心场景定义为：

> 用户进入一个真实代码仓库，要求 GenesisAI 分析测试失败、定位原因、修改代码、运行相关测试、检查 Git diff，并把经过验证的测试命令和项目约定保存到 Memory。

通过条件：

1. Agent 只加载完成任务需要的 Skill 和 Tools。
2. 文件搜索、读取和修改均在工作区边界内。
3. 修改前后内容可追踪，外部并发修改不会被静默覆盖。
4. 测试命令确实执行，并保留退出码和输出摘要。
5. 最终回答列出修改内容、验证结果和剩余风险。
6. Git diff 与汇报一致。
7. 重启后能从 JSON 与 Markdown Memory 中找到已验证命令。
8. Memory 没有保存整个仓库副本、密钥或模型推理文本。
9. xiaoerAI 文件没有被修改，也不是 GenesisAI 的运行时依赖。

## 20. 完成定义

本轮升级完成时，GenesisAI 应能作为一个可实际使用的单 Agent 开发 CLI：它能理解真实仓库，通过目录发现并按需加载 Tools，遵循 Skill 完成代码修改和验证，并使用 JSON 与 Markdown 保存轻量、可审查、可迁移的项目 Memory。

“任务完成”必须有实际证据：文件变更真实存在、相关验证实际运行、失败和限制如实说明、权限没有被绕过、长期 Memory 只记录经过允许且具备来源的信息。

任何阶段的“自动测试通过”和“用户可用”必须分别记录。协议测试、FakeModel 和离线测试证明实现机制没有退化；只有对应阶段规定的真实模型端到端场景也通过，才可以向用户报告该功能当前可用。
