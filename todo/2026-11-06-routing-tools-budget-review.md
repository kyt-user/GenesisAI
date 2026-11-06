# GenesisAI 路由工具与执行预算改进清单

日期：2026-11-06。目标：修复 GitHub 仓库阅读失败，并明确支持中大型项目维护所需的改造范围。

核心问题是任务分类、流程提示、工具选择和预算没有一致的决策依据。优先修复任务阻断，再完善持续执行与仓库检索，工具参数按实际负载调整。

## 等级和状态

- P0 紧急：数据损坏、权限失守或核心功能普遍不可用。本次没有确认此类问题。
- P1 高：已阻断正常任务，或直接限制持续开发任务，应优先处理。
- P2 中：影响调用成功率、仓库覆盖、执行效率或结果说明，安排专项改进。
- P3 低：进一步提升规模与效率，先评估收益。

“已确认缺陷”有日志、代码或本地复现支持；“能力改进”表示已确认实现限制，但尚未证明某个中大型项目任务因此失败。等级为实施优先级，不等同于安全漏洞等级。下表保留审计时的问题与参考方案，当前选型和完成情况见文末落实记录。

## 问题与修改范围

| 编号 | 等级 | 问题与状态 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- |
| R01 | P1 | **任务分类误判，已确认缺陷。** “仓库”或 `git` 子串在 URL 判断前命中 `local_files`；`github.com` 本身也会命中。两次用户输入都被归入本地任务。 | 显式识别 URL、路径和任务目标；本地与网络能力允许组合；关键词只作为初始线索。保留“只讨论”“只读”“禁止联网”等用户约束，执行中允许纠正分类。 | [budgets.py](E:/PROJECT/GenesisAI/src/genesisai/shared/budgets.py)、[task_protocols.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/task_protocols.py)、[loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)；分类与连续对话测试。 |
| R02 | P1 | **流程、工具和预算决策脱节，已确认缺陷。** 当前分别通过 profile、protocols、tool routes 决定行为；网页工具已加载，但本地档位网页预算仍为 0。运行时只为 `direct_answer` 自动切换联网档位。 | 统一任务上下文，分别表达意图、工作流程、所需能力、资源预算和权限。根据实际合法调用启用所需资源，受明确的总预算和权限约束；发现工具不等于获得授权，切换能力不能清零累计消耗。 | [budgets.py](E:/PROJECT/GenesisAI/src/genesisai/shared/budgets.py)、[research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py)、[selection.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/selection.py)、[tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py)、[composer.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/composer.py)、[ports.py](E:/PROJECT/GenesisAI/src/genesisai/agent/ports.py)；运行状态持久化与校验、工具治理测试。 |
| R03 | P1 | **局部阻塞导致整个任务收尾，已确认缺陷。** 本次 0 次网页预算触发 `force_answer`，下一次请求不再提供工具；执行循环另有连续失败阈值。 | 区分参数错误、资源不可用、权限拒绝、单能力额度耗尽和全局预算耗尽。可恢复问题进入修正或重规划；单能力不可用时保留仍有价值且已授权的步骤。只有全局停止条件或全部有效路径耗尽才收尾。 | [research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py)、[loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)、[recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)、[恢复提示](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/completion/recovery.prompt.yaml)；失败恢复与完成校验测试。 |
| R04 | P1 | **持续开发预算偏紧且多处叠加，能力改进。** 默认 20 轮、40 次工具调用、300 秒；profile 另有模型调用额度。单独增加 CLI 轮数可能仍被其他上限提前停止。 | 统一预算来源与可见状态，区分进度检查的软阈值和用户配置的硬上限。到软阈值整理上下文、核对进展；到硬上限保存检查点。为验证与收尾预留资源，不能通过换阶段或恢复自动绕过总上限。具体默认值通过代表性任务测定。 | [budgets.py](E:/PROJECT/GenesisAI/src/genesisai/shared/budgets.py)、[time_limits.py](E:/PROJECT/GenesisAI/src/genesisai/shared/time_limits.py)、[loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)、[research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py)、[cli.py](E:/PROJECT/GenesisAI/src/genesisai/app/cli.py)、[terminal_view.py](E:/PROJECT/GenesisAI/src/genesisai/app/terminal_view.py)；时间限制和会话恢复测试。 |
| R05 | P2 | **跨轮延续判断依赖短语，能力改进。** 当前开发延续主要依赖活动任务、24 字长度限制及固定短语集合；已有任务持久化，但仍需完善复杂追问的关联判断。 | 综合活动目标、计划进度、用户最新指令和待处理事项，区分继续、纠正、追问和新任务；复用有效结果与待办，已变化文件重新验证；明确状态恢复与预算恢复的边界。 | [task_protocols.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/task_protocols.py)、[tasks.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/tasks.py)、[loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)、[context_sources.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_sources.py)、[session.py](E:/PROJECT/GenesisAI/src/genesisai/core/persistence/session.py)；任务延续、纠正与恢复测试。 |
| R06 | P2 | **工具发现召回弱且加载范围固定，缺陷与能力改进。** 本次英文查询找到网页工具，中文相关查询为空；单次发现最多 4 个，动态发现列表只保留最近 8 个。 | 增加能力标签、中文别名和组合词匹配；分离候选发现与完整 Schema 加载。发现候选上限可先试验 8–12 个，活跃工具按当前步骤、依赖和上下文预算保留；同轮待执行工具不能被后续发现挤掉。 | [selection.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/selection.py)、[catalog.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/catalog.py)、[tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py)、[discover_tools](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/discover_tools)、[工具目录提示](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/tools/catalog_usage.prompt.yaml)；目录、发现与工具治理测试。 |
| R07 | P2 | **参数误用后的反馈和恢复可改进，已观察到调用错误。** 模型给 `discover_tools.limit` 传入 6，Schema 上限为 4；校验拒绝正确，但返回信息只有“数值超限”。 | 参数说明补足单位、范围和示例；错误返回字段、实际值、合法范围及修正方式。允许模型修正重试，把协议错误与执行失败分别统计。只扩充确有需求的参数，不取消类型和边界校验，不静默改写有副作用的参数。 | [base.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/base.py)、[executor.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/executor.py)、[recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)、相关工具的 `spec.py` 与 `tool.yaml`；Schema、参数边界和恢复测试。 |
| R08 | P2 | **代码结构索引覆盖不完整，已确认缺陷。** 索引仅取排序后前 100 个支持的文件；有截断标记，但索引分支没有续读游标；单文件超过 20 万字符也被截取。 | 为索引增加稳定分页、覆盖范围和跳过原因；明确哪些文件只被部分解析，支持按模块索引。避免把部分索引解释为整个项目结构。分页需处理文件新增、删除或顺序变化。 | [search_codebase 实现](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/implementation.py)、[参数定义](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/spec.py)、对应 `tool.yaml`；超过 100 个文件、大文件和分页变更测试。 |
| R09 | P2 | **搜索结果表达受限，能力改进。** 上下文最多前后 3 行，单页最多 100 条；已有搜索分页，可保留并完善。 | 上下文上限可先试验 20–40 行；增加文件名、计数、内容等输出模式；按返回字符或 Token 总量控制结果。提供明确分页与截断提示，减少反复查找和大段无关输出。 | [search_codebase](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase)、[工具输出处理](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py)；搜索过滤、分页、输出上限测试。 |
| R10 | P2 | **批量读取与编辑需要按总量管理，能力改进。** 读取已支持 50 个文件、`limit` 最大 20 万字符，但一批文件共用范围参数；编辑一批最多 20 个补丁。仅提高数量会扩大上下文和失败处理成本。 | 支持每个文件独立读取范围，设置整次调用的总输出预算；优先使用局部补丁。批量编辑如需扩容，应同步明确逐文件校验、部分成功或原子提交语义，并可准确查询结果。 | [read_files](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/read_files)、[editor](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/editor)、[tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py)、[context_budgeter.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_budgeter.py)；多文件读取、版本冲突、部分失败和输出裁剪测试。 |
| R11 | P2 | **长构建和长测试的执行流程需完善，能力改进。** 前台命令超时上限 600 秒；已具备后台执行、读取和停止作业能力，仍需核验与整体任务时限、恢复和完成判定的衔接。 | 建立启动、等待、增量读输出、完成验证流程。区分单次调用超时、作业时限和任务总预算；恢复后查询已有作业，避免重复启动；运行中或超时不能被描述为通过。 | [run_commands](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/run_commands)、[read_job](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/read_job)、[stop_job](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/stop_job)、[jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py)、[process.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/process.py)、[loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)；后台作业、取消、超时和恢复测试。 |
| R12 | P2 | **失败说明没有准确表达原因，已确认缺陷。** 本次实际是预算初值为 0、联网前被拒绝，最终回答却说“额度已用完”，并要求用户克隆、粘贴或换环境。 | 从结构化执行事实生成失败说明，区分未分配能力、实际额度耗尽、权限拒绝、网络失败和来源缺失；说明系统下一步或具体阻塞。能在现有授权内继续时先继续，不能继续时准确说明原因，不承诺未经验证的重试效果。 | [research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py)、[recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)、[loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)、[执行事实](E:/PROJECT/GenesisAI/src/genesisai/shared/execution.py)、[完成契约](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/completion)、终端错误展示；失败后追问和事实一致性测试。 |
| R13 | P3 | **进一步增强大仓库结构理解，能力建设。** 当前结构索引主要依赖 Python、JS、TS 的正则提取；跨模块影响定位与增量更新值得专项评估。 | 在 R08 的可靠覆盖基础上建立模块概览、增量更新和依赖关系定位；按语言和项目需求评估语法解析或语言服务接入。验证变更影响定位准确率，再决定扩大投入。 | [search_codebase](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase)、[project_index.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/project_index.py)、[项目上下文](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_sources.py)；新增跨模块检索与影响分析评测。 |

## 证据定位

原始会话：[session_7e0175ac37ba4e3e.jsonl](C:/Users/Administrator/.genesisai/logs/session_7e0175ac37ba4e3e.jsonl)。

| 位置 | 事实 | 对应项目 |
| --- | --- | --- |
| 日志 7 行 | 首次模型请求为 `local_files`，网页抓取预算和实际使用次数均为 0。 | R01、R02 |
| 日志 237 行 | `discover_tools.limit=6` 超过上限 4，返回参数校验错误。 | R07 |
| 日志 306、309 行 | 英文发现查询找到网页工具，中文查询返回空列表。 | R06 |
| 日志 364、366 行 | 抓取在执行前被预算拦截，随后进入回答阶段且工具数量为 0。 | R02、R03 |
| 日志 1145、1409 行 | 新追问再次进入 `local_files`；重新发现网页工具后，抓取预算仍为 0。 | R01、R02 |
| 日志 1733 行 | 最终说明使用“抓取额度已用完”，要求用户提供替代来源或换环境。 | R12 |

修复前代码的纯内存复现：两个原始用户输入及单独的 GitHub 链接均选中 `local_files`；同一个已登记 URL 在 `local_files` 下被 `before_execute` 拒绝，在 `direct_answer` 下会切换到 `web_quick` 并通过预算前置检查。该复现没有发起网络请求，不能据此判断 GitHub 或网页提供商的实际可用性。

## 验收要点

| 编号 | 最低验收条件 |
| --- | --- |
| R01 | GitHub URL、中文仓库阅读、纯本地修改、开发中查文档、只讨论及禁止联网等用例均正确处理；不使用特定域名白名单修补普遍误判。 |
| R02 | 模型可见的工具、任务约束和实际执行条件一致；本地与网络能力可组合；动态启用不能越权或重置累计消耗。 |
| R03 | 参数错误可修正，单网页失败不关闭所有工具；权限拒绝不触发绕过；真正达到全局上限时仍停止。 |
| R04 | 用户配置的硬上限始终生效；软阈值可继续有进展任务；暂停、恢复及阶段切换不重复计费或遗失消耗；能解释由哪一限制停止。 |
| R05 | 长句继续指令、简短追问、目标纠正和新任务可区分；恢复保留有效计划与结果，不自动重放结果不确定的副作用调用。 |
| R06 | 中文同义表达与英文表达均能找到目标工具；发现超过 8 个候选不破坏当前步骤；不把全部 MCP Schema 无条件塞入上下文。 |
| R07 | 类型、数值、枚举和必填字段错误都返回明确修正信息；放宽后 Schema、实现、描述和边界测试一致。 |
| R08 | 超过 100 个文件的仓库可完整分页覆盖，无重复、无遗漏；文件变化及单文件截断明确可见。 |
| R09 | 搜索模式和上下文范围按约定生效；结果总量有界且可续读；不会因扩大上下文而丢失分页线索。 |
| R10 | 多文件范围互不干扰；总输出有界；文件冲突和批量失败的实际修改范围可核验，不覆盖用户未提交修改。 |
| R11 | 作业启动后可持续读取输出、取消和恢复；任务结束时明确作业状态；不会重复执行或将运行中状态当作测试通过。 |
| R12 | “未开始联网”“访问失败”“额度耗尽”表述与账本一致；后续追问不把历史失败当作新的执行结果。 |
| R13 | 用代表性多模块项目衡量检索覆盖、变更影响定位、索引更新时间和上下文成本；未达到收益目标时不默认扩大架构。 |

## 实施顺序和边界

1. 第一批处理 R01、R02、R03、R12：修复本次会话暴露的任务阻断与说明问题，建立端到端回归。
2. 第二批处理 R04、R05、R11：统一预算与任务连续性，完善长作业流程。
3. 第三批处理 R06、R07、R08、R09、R10：完善工具发现、参数契约和大仓库读取覆盖。R07 的错误说明可随第一批提前实施。
4. 最后评估 R13：在已有流程可靠的前提下提升结构理解能力。

统一路由应遵守当前分层依赖：跨层状态契约放在 shared，策略实现放在 core，agent 经端口调用。替换旧分类或预算实现时同步清理调用方、测试和文档，不另加仅为兼容而存在的回退层。

现有 ENV 优先、终端密钥回退、会话恢复、错误处理继续保留。MCP 和 Skill 接入统一能力目录和流程选择，不产生额外权限。文件授权边界、版本校验、删除确认和真实验证要求不随参数扩容而削弱。

以上多个文件在本轮开始前已有未提交修改；本轮在现有修改上增量实现，没有回退其他改动，没有迁移或删除历史会话与用户数据。

相关待办：[上下文组装评估](E:/PROJECT/GenesisAI/todo/2026-11-05-context-assembly-review.md)涉及协议路由、工具目录和上下文预算；[外部会话审计](E:/PROJECT/GenesisAI/todo/2026-11-06-external-session-review.md)涉及命令执行、失败恢复和来源校验。实施前复核其已完成项，避免重复改造。

## 2026 年 11 月 6 日落实记录

| 编号 | 本轮状态 | 已落实的行为和选型 |
| --- | --- | --- |
| R01 | 已实现 | 起始流程判断移至 core 的任务路由；分类前剥离 URL，英文技术词使用词边界。仓库 URL 进入网页阅读，本地开发可同时启用 web；不增加特定 GitHub URL 的特殊执行分支。 |
| R02 | 已实现 | `TaskProtocolSelector.route` 统一生成流程、协议、能力集合、约束与执行意图。提示词和工具选择消费相同结果；实际工具执行再次检查声明的网络与副作用属性。能力切换不清零消耗或覆盖用户模型轮次上限。 |
| R03 | 已实现 | 网页额度耗尽作为局部限制返回，不移除全部工具；本地开发中的网页失败和网页无进展不强制终止整个任务。参数类错误保留修正机会，重复失败仍受限；网页进展判断同时计入本地来源与修改记录。 |
| R04 | 已实现 | 初始开发默认调整为 60 轮、180 次工具调用、1800 秒；新增 `--max-tools`。CLI 参数统一决定硬上限，每 20 轮保存进度检查点并提示核对计划。模型最后一轮返回的合法工具可正常审批和执行，恢复后仍使用原上限。内部模型重试和摘要请求不是新的交互轮次，仍受各自次数及剩余总时限约束。新默认值是可配置的起点，不是经真实大项目验证的最优值。 |
| R05 | 已实现基本延续规则 | 支持较长的继续执行语句，排除只讨论、取消和明确新任务；相关追问继承能力、约束和已登记 URL，即使此前尚未取得网页正文。追问附带新 URL 不会自行清除只读或禁网约束；只读讨论后的“继续”不会自动授权改写，明确执行指令才能解除相应限制。依然是确定性规则，不宣称完整自然语言意图识别。 |
| R06 | 已实现 | 发现上限 12、默认 6，增加中文能力别名和短片段召回；`activate=false` 只返回候选。发现记录不再固定截为 8 个，实际定义按约 24000 字符预算选择；基础工具、活动 Skill、最近使用、待执行工具及精确名称请求优先保留，最终仍受模型上下文上限检查。`available_next_round` 与实际选择一致，其他候选列为 `deferred`。 |
| R07 | 已实现 | 数值、长度、项数错误包含实际值与合法范围；类型和枚举错误提供明确修正信息。参数错误与普通执行失败分别累计，保留类型校验和重复错误停止机制。 |
| R08 | 已实现 | 结构索引支持文件分页、总文件数和绑定文件集合及元数据快照的游标；集合变化拒绝旧游标。目录过滤复用文件遍历规则，大文件截断、符号截断与解析失败可见。115 个文件的三页结果完整且无重复。 |
| R09 | 已实现 | 搜索上下文最大 40 行；新增 `output_mode=files/count`，支持输出字符预算。输出预算提前截断时按实际返回项数续页，不跳过尚未返回的命中。 |
| R10 | 已实现读取改进并保留编辑上限 | 多文件可设置独立范围，默认总正文 24000 字符并返回续读请求；普通文本不再在提取阶段固定截断为 20 万字符，仍保留 8 MiB 文件上限，Office/PDF 仍受提取上限约束。编辑器原有 20 个补丁、预检、版本冲突和部分失败报告继续保留，现有回归覆盖其行为，本轮未扩大批量修改范围。 |
| R11 | 已实现 | 后台作业支持最多 30 秒有界等待与增量输出；测试作业完成后解析完整已捕获日志，只有实际通过且属于当前任务、文件未在启动后变化时产生验证事实。待验证作业阻止重复自动启动测试；恢复时所有者不可用不会自动重放。命令的现有 600 秒上限保留，不把后台能力描述为无限运行。 |
| R12 | 已实现 | 预算拒绝明确说明未分配或已用完，并说明没有发起网络请求；提示词要求根据执行账本说明障碍。已加载能力、局部限制和进度检查点进入可信状态。自然语言输出仍需模型遵循事实，不把提示词改善视为对所有表述的形式化保证。 |
| R13 | 评估完成并落实 AST 复用 | 发现已有持久化增量项目索引和 Python AST 解析，因此直接复用已有解析能力，删除代码搜索中的重复 Python 正则解析。返回 `python_ast`、`regex` 或解析失败标记；没有新增另一套索引数据库、完整依赖图或语言服务。完整变更影响图和其他语言语义索引保留为后续专项。 |

工具约束依赖工具声明和任务路由，不替代操作系统级沙盒；尤其禁网声明不能被解释为对任意第三方程序内部网络行为的隔离保证。用户明确设置的运行时权限继续独立生效。

## 本轮验证

- 新增 [路由与预算回归](E:/PROJECT/GenesisAI/tests/test_routing_budget_repair.py) 和 [大仓库工具回归](E:/PROJECT/GenesisAI/tests/test_large_project_tools.py)，并更新旧分类入口的测试导入。保留既有产品断言，不通过放松审批或完成门禁来消除失败。
- 第一组回归 34 项通过，预算与既有运行时扩展回归 93 项通过；大仓库和记忆专项 23 项通过。
- 首次全量回归为 574 passed、1 failed、1 skipped。唯一失败暴露“最大 1 轮时丢弃待审批工具”的缺陷，已移除研究控制器中重复的模型预算终止逻辑。随后边界与工具专项 44 项通过。
- 130 个合成 Python 模块的增量索引评测：首次更新 130 个，未变化更新 0 个，单模块修改更新 1 个。专项运行耗时分别约 2438.9、241.8、50.9 毫秒；这些数字只描述本机合成数据，不代表真实中大型项目性能或成本。
- 当前仓库 `src/` 的只读评测使用独立状态目录：230 个 Python 文件全部进入持久化索引，结构检索分 8 页覆盖 230 个文件，无重复、遗漏或部分解析；308 个源码及资源文件首次索引约 4278.5 毫秒，未变化扫描更新 0 个、约 527.3 毫秒。`genesisai.shared.time_limits` 的 5 个已核对直接引用模块全部召回，无额外结果，约 10.8 毫秒、返回上下文 7685 字符。全部结构页合计 169800 字符，对应 Python 原文 730286 字符；单页含元数据约 2945–25537 字符。详见 [评测原始记录](E:/PROJECT/GenesisAI/temp/routing-index-1106-evaluation.json)。这只支持本仓库的直接引用定位，不证明传递依赖、动态导入或其他语言的语义影响分析能力；字符数不等同于模型 Token 或费用。
- 第二次全量回归为 579 passed、1 failed、1 skipped，唯一失败是 Windows 子进程未能在测试设置的 1 秒内进入执行阶段；该组隔离复跑 3 项通过。测试增加启动余量，并完整等待延迟写入窗口，仍要求超时终止、错误类型正确、重复调用不重放，以及本进程和子进程均不产生晚到写入。
- 最后的路由边界修正（相关追问附带新 URL 仍保留明确约束）通过 110 项路由、开发延续和既有网页追问回归，耗时 83.56 秒，见 [专项 JUnit 报告](E:/PROJECT/GenesisAI/temp/routing-followup-final-1106.xml)。这组验证覆盖全量测试启动后补入的最终路由修改和新回归用例。
- 最终全量离线回归 **582 passed、1 skipped**，耗时 433.91 秒，结果见 [JUnit 报告](E:/PROJECT/GenesisAI/temp/routing-tools-1106-results.xml)。唯一跳过项是宿主机不允许创建符号链接，目录联接另有用例覆盖。全量报告与最终路由专项报告合并去重后覆盖当前收集到的全部 **584 个用例（583 通过、1 跳过）**，没有缺失或过时用例，见 [覆盖核对](E:/PROJECT/GenesisAI/temp/routing-tests-1106-coverage.json)。两项分层依赖检查通过，CLI `--help` 可显示新参数，空白检查通过。
- 本轮没有调用真实模型 API 或真实 GitHub 网络服务；网页流程用受控网络替身验证执行、证据登记与最终交付，原始用户日志与历史数据未被修改。

### 验收证据对照

| 覆盖项 | 主要证据 |
| --- | --- |
| R01、R02、R12 | `test_routing_budget_repair.py` 的 URL 分类、混合能力、约束执行、受控网络取正文及来源登记用例；`test_external_session_regressions.py` 的失败账本与后续追问回归。 |
| R03、R07 | 路由回归中的零预算局部拒绝与参数实际值/范围；`test_acceptance.py` 的参数错误、连续失败、审批拒绝和全局上限；`test_coding_tool_expansion.py` 的嵌套 Schema 校验。 |
| R04 | `test_large_project_tools.py` 的 25 轮实际循环和第 20 轮检查点；`test_time_limits_sessions.py` 的显式时限、剩余时间和超时处理；`test_acceptance.py::test_A08_deadline_and_persisted_limits` 的 1 轮审批恢复边界。 |
| R05 | 路由回归中的长句继续、只读/禁网继承、带新 URL 的相关追问、新任务及持久化恢复；`test_development_continuation.py` 和 `test_coding_session_regressions.py` 的有效任务延续、无关提问与文件变化重新验证。 |
| R06 | 路由回归中的中文召回、候选模式、80 个 MCP 工具的定义预算和待执行工具保护；`test_tool_governance.py` 的中途发现与未提供工具拒绝；既有 Skill、MCP 产品验收。 |
| R08、R09 | 大仓库回归中的 115 文件三页覆盖、游标失效、目录排除、大文件截断、AST 误报检查、搜索计数/文件模式/40 行上下文及输出预算续页；当前仓库 230 文件只读评测。 |
| R10 | 大仓库回归中的独立读取范围、25 万字符文本尾部和超长行续读；`test_coding_tool_expansion.py` 的批量预检、过期审批及首个写入后的并发冲突报告。 |
| R11 | 大仓库回归中的后台测试等待、真实通过摘要、过期或异任务验证拒绝及所有者失联不重放；`test_coding_tool_expansion.py` 的停止审批、超时与恢复；进程治理测试检查超时后的进程树不会晚到写入。 |
| R13 | 130 模块增量更新计数与时长，以及当前仓库的覆盖、直接引用定位和字符量评测；明确不将单样本结果扩展为通用语义影响分析结论。 |
| 保持正式功能 | `test_api_key_priority.py` 验证 ENV 优先及终端回退；`test_storage_schema.py`、`test_tool_runtime.py` 验证会话恢复、未知执行不重放和权限边界；Import Linter 验证分层依赖。 |
