# 长任务限制审计与逐项评审清单

日期：2026-11-06。范围：E:\PROJECT\GenesisAI 当前工作树，包含尚未提交的能力构建实现。基线提交：8312d27；不是只检查该提交的旧代码。

状态：**用户已批准大胆改造方向，当前正在分批实施与测试。** 表中“限制及当前值”保留审计时快照；实施进展与验证结果见 [实施记录](2026-11-06-long-task-implementation.md)，不能把快照当作当前代码行为。原有未提交改动保留。

## 统计口径

已核实 **149 项可逐项评审的限制策略，分 13 类**。这是当前清单的去重条目数，不是代码里数字或条件判断的出现次数，也不表示 149 个全部需要删除的硬限制。

- 一项表示一组可共同评审的同类策略；例如同一种图片预算在附件、MCP、上下文重复校验，只记一次。同一接口的一组尺寸字段可合为一项。
- 包含：可配置默认值、硬编码上限、分页/截断规则、停止与恢复门禁、权限/完整性保护、接入能力边界、提示词行为限制。表内说明具体性质和触发后果。
- 不把纯显示宽度、轮询间隔、普通缓存容量、哈希长度、测试专用常量计作任务限制。不把一个尚未实现的任意功能都算作限制；只纳入现有接口明确限定的能力边界。
- 数值指源码默认或参数允许范围，**不等于用户运行时一定采用默认值**。字节、字符、token分别说明；模型配置允许的最大数字不代表供应商实际支持量。
- “保留”也可列P1，表示其对任务完整性很重要，不表示它是P1缺陷。P0是已复现或直接阻断本次主链路；P1是长任务关键项；P2是常规改进/需场景判断；P3是低优先级或通常合理的边界。
- “删除”表示取消当前不必要的硬策略；“放宽”表示保留机制但扩大或可配置；“重构”表示换成进展、阶段、分页、事件或引用机制，单改数字不能解决；“保留”表示保留目标和边界。
- 表中“修改范围”列的是实现入口；后续任何替换都应同步清理调用方、测试与文档，遵循仓库不留历史兼容层的约定。

更新后的建议：**删除 42 项、放宽 11 项、重构 64 项、保留 32 项**。删除针对本行明确指定的硬策略，不表示同时去掉同组的类型校验、授权、完整性或实际资源保护。源码现状列保持审计事实，建议列为新的目标行为。

| 类别 | 范围 | 条目数 | 编号 |
| --- | --- | ---: | --- |
| A | 任务执行与停止 | 11 | A01–A11 |
| B | 错误恢复与回答门禁 | 10 | B01–B10 |
| C | 模型输出、上下文与图片 | 15 | C01–C15 |
| D | 路由、工具发现与交互参数 | 10 | D01–D10 |
| E | 工具执行、文件读写与代码搜索 | 15 | E01–E15 |
| F | 项目索引与长期记忆 | 9 | F01–F09 |
| G | 联网检索与网页采集 | 13 | G01–G13 |
| H | 终端命令与后台作业 | 9 | H01–H09 |
| I | 外挂能力创建、测试与注册 | 20 | I01–I20 |
| J | MCP、Skill、规则与语言服务 | 11 | J01–J11 |
| K | 权限、数据完整性与授权边界 | 8 | K01–K08 |
| L | 办公文档、媒体与长篇内容 | 11 | L01–L11 |
| M | 提示词中的额外行为限制 | 7 | M01–M07 |
| 合计 | | **149** | |

## 更大幅度的改造方向

目标：用户给出目标与授权后，系统在范围内持续推进中大型项目任务。模型请求结束、阶段检查点、前台等待结束均不等于任务结束。

1. **任务持续存在**。撤掉默认30分钟、60轮、180工具、连续3失败的业务硬停。任务目标、约束、验收、待办、产物、作业和未知副作用独立持久化。完成、取消、用户显式预算耗尽、必需输入/授权缺失或无可行替代方案的外部阻塞，才形成明确停止状态。
2. **工具始终可发现**。路由只推荐工具和流程；短句不重置目标，回顾不关闭执行，恢复不关闭工具。按模型上下文加载schema，完整授权目录可检索和精确加载。每次真实执行仍校验schema、调用身份及权限。
3. **大内容可续读、可引用、可增量修改**。取消文件、页数、行数及日志的不可恢复前缀截断；单批有预算，完整资源有引用和覆盖记录。能力改为草稿清单＋文件/资产引用＋增量修订＋冻结版本，清理完整JSON包旧接口。
4. **长作业共用执行机制**。前台短等后返回job_id，作业继续受监管。终端、MCP、外挂Tool、Skill测试统一进度、取消、超时、资源配额和结果持久化。独立任务按资源调度并发，冲突写入有序执行。
5. **授权按任务范围持续有效**。只读/禁网等约束有来源和作用域，范围内不反复确认。任务授权与具体能力版本验收分别管理；用户满意后注册的要求不变。
6. **提示词允许完整工程工作**。授权Git写入、合理测试修订、代码生成器和项目工具链正常使用；改造规模服从目标，验证匹配风险。同步清理与新行为冲突的提示、调用方、测试、文档。

防循环按“同一动作＋同一输入＋同一错误/无状态变化”处理：先换方法、做检查点，确实无法推进时报告具体阻塞。模型声称有进展、进程仍存活或持续打印日志，都不能单独证明业务有进展。

资源保护保留：模型/协议实际容量、连接/首响应/空闲超时、单次解码/内存及解压边界、网络地址校验、显式存储配额。单批处理容量不等于整份资源永久不可处理。存储不足应明确处理，未经用户确认不删除既有日志、会话或文件。

授权与完整性保留：用户约束、删除确认、覆盖冲突、凭据隔离、真实测试与验收、注册版本/报告绑定、未知副作用对账。ENV优先、终端密钥回退、会话恢复及错误处理继续是正式功能。

### 实施切分与验收

| 批次 | 形成的完整能力 | 必须证明 |
| --- | --- | --- |
| 1 | 任务状态、停止策略、错误恢复、真实完成判定 | 超过旧轮次/次数仍推进；短句续接、截断、临时失败不丢目标；未完成不会标completed |
| 2 | 引用/增量能力构建、模型输出预算 | 大实现无需一次输出完整JSON；失败可修订重测；验收精确绑定注册版本 |
| 3 | 统一长作业、MCP远程接入与生效配置 | 超过10分钟可继续且可取消；跨宿主恢复可对账；测试和正式执行配置一致 |
| 4 | 文件/索引/文档/日志全量可达、动态目录与Skill | 超过旧1000/5000文件等阈值仍可读到尾部；工具不会被关键词路由锁死 |
| 5 | 任务范围授权与提示词清理 | 范围内不反复确认；明确禁令仍有效；授权Git、测试更新及工具链写入不被误拦 |

新机制替换时同步删除旧接口、旧行为开关、调用方与失效测试/文档，不增加兼容层或历史格式迁移。每批验证完整链路，再替换依赖机制，避免先撤掉上限却仍一次把全部内容装入内存。保留必要的确定性回归，并用真实模型验证长工具参数和多轮续接。

以上为已批准的实施方案。表中旧限制是审计基线；最终逐项核销须以实现、测试和实施记录为准。


## 建议先评判的主链路

1. **C01 → B03/B07 → I01**：输出2048、工具参数截断恢复、恢复禁用工具、一次提交完整能力包共同形成死循环。必须一起考虑，不能只把输出调大。
2. **D01/D02 → J01 → X03/X05**：任务续接不能丢意图；接远程MCP不应总被迫造桥；实际验收状态和正式调用配置必须一致。
3. **A01–A04 → E01 → H01 → I06/I07/I10**：梳理任务、模型、工具、作业、测试五层时间预算，避免上层允许、下层提前终止。
4. **E08/F01/F02 → C03–C06 → H03/H04**：中大型项目的可见范围、记忆、完整日志，决定任务能否持续推进。
5. **M01–M07**：清理提示词中的绝对禁令和相互矛盾要求，使已授权开发、测试修订、Git提交能正常执行。

## 容易误判的地方

- 任务目前是默认 **60轮/180工具/1800秒**。PROFILES里的3/8/12/20/25模型调用数会被主Runner的max_rounds覆盖，不能另算成主任务的有效硬上限；其中搜索/抓取额度仍生效。
- 每20轮progress checkpoint只提示检查进展，**不会在20轮停止**。
- 连续失败默认3次，成功一次会清零；不是整个长任务累计失败3次就停止。
- read_job等待最多30秒是一次等待时长，**不是后台作业寿命**。后台仍受run_commands最多600秒的另一条限制。
- list_files前1000文件是总扫描截断；search_codebase内容搜索使用cap=None，不共用该1000上限；chapter_list也没有该上限。
- 12000字符工具预览、16000字符memory_read分页、200000字符原文存储裁剪是不同层。只有保留了完整原文，分页才能补回尾部。
- auto模式能放开许多普通确认/宿主路径，不能绕过能力注册的用户验收。命令及测试进程不构成OS级文件/网络沙箱，不能把文件工具的访问限制误称为全局隔离。
- 40万输入上下文不能弥补2048输出、单个必需上下文块超额、或完整工具参数必须一次发送的问题。

## A．任务执行与停止（11项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| A01 | **任务累计时长**：默认 1800 秒；CLI --seconds 可改 | 到期停止并保存进度，长构建/调研会中断 | P1／删除 | 撤掉默认1800秒任务寿命；任务跨请求/阶段持续存在，只有用户显式总时间/成本预算作为任务级资源停止条件 | [shared/time_limits.py](E:/PROJECT/GenesisAI/src/genesisai/shared/time_limits.py)<br>[agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)<br>[app/cli.py](E:/PROJECT/GenesisAI/src/genesisai/app/cli.py) |
| A02 | **模型轮次**：默认 60 轮；CLI --max-rounds 可改 | 硬停止；搜索、修复、验证共享额度 | P1／删除 | 撤掉默认60轮终止任务；轮次仅用于计量、诊断和检查点，尊重用户显式预算 | [shared/time_limits.py](E:/PROJECT/GenesisAI/src/genesisai/shared/time_limits.py)<br>[agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| A03 | **工具调用次数**：默认 180 次；CLI --max-tools 可改 | 仓库扫描及多轮测试可提前耗尽 | P1／删除 | 撤掉默认180次工具终止任务；依据任务进展继续执行，等待状态不挤占业务额度 | [shared/time_limits.py](E:/PROJECT/GenesisAI/src/genesisai/shared/time_limits.py)<br>[agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| A04 | **单次模型请求超时**：默认 60 秒；CLI --request-timeout 可改，仍受任务剩余时间约束 | 深推理或长工具参数请求可能超时 | P1／放宽 | 区分连接、首 token、流空闲与整体生成超时 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)<br>[shared/time_limits.py](E:/PROJECT/GenesisAI/src/genesisai/shared/time_limits.py) |
| A05 | **连续工具失败停止**：默认 3 次；Runner 参数可改，CLI 未暴露；成功一次会清零 | 不同原因的连续失败也能耗尽同一预算 | P1／删除 | 撤掉连续3次失败终止整个任务；按动作和错误指纹诊断、换方法，无可行路径时记录具体阻塞原因 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| A06 | **可修正参数错误宽限**：同一工具的 validation_error 等前 2 次不计失败；之后计入 | 参数探索仍可能触发总失败停止 | P2／删除 | 撤掉同一工具参数错误累计为全局失败的策略；给出可修正反馈，检测重复无效动作 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| A07 | **预留最后一轮回答**：剩余模型调用约 1 次时强制转回答 | 剩余步骤即使很小也不能执行 | P2／删除 | 撤掉按剩余轮次强制最终回答；检查点独立保存，真实验收或明确停止条件才结束任务 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| A08 | **自动验证次数**：最多注入 3 轮自动验证；不等于手动测试只能 3 次 | 长修复链的自动验证流程提早耗尽 | P1／删除 | 撤掉最多3轮自动验证；实质变更后运行对应验证，无新变更或证据时避免重复测试 | [core/development/policy.py](E:/PROJECT/GenesisAI/src/genesisai/core/development/policy.py) |
| A09 | **开发任务必须产生变更的恢复门禁**：模型连续两次未写入且未验证成功，判 partial | 合法的无需修改结论可能被迫进入 editor | P1／删除 | 撤掉必须写入及两次未写即partial的规则；验收允许有证据的无需修改，也接受正规工具链产生的变更 | [core/development/policy.py](E:/PROJECT/GenesisAI/src/genesisai/core/development/policy.py) |
| A10 | **会话单任务与串行执行**：CLI 单 worker；同一批工具顺序执行；提示明确单 Agent | 独立读取、测试无法并行，拉长任务耗时 | P2／重构 | 保持有冲突的写入串行；支持独立只读/后台作业并发，多 Agent 另行决定 | [app/run_queue.py](E:/PROJECT/GenesisAI/src/genesisai/app/run_queue.py)<br>[agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)<br>[core/prompt/templates/system/identity.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/system/identity.prompt.yaml) |
| A11 | **活动/待确认/未知执行状态阻止新运行**：运行中或有未解决执行状态时不能直接开始另一 run | 需要恢复或终结旧运行后才能继续 | P2／保留 | 保留防重入；提供明确暂停、续跑、隔离任务入口 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |

## B．错误恢复与回答门禁（10项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| B01 | **供应商临时错误重试**：连接/超时/限流/内部错误通常只重试 1 次；与溢出恢复共享 attempts | 短暂故障能终止较长任务 | P1／重构 | 可配置退避/Retry-After/总重试时长；不同恢复原因分开计数 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| B02 | **上下文溢出恢复**：压缩后仅再试 1 次，再溢出失败 | 估算偏差较大时过早终止 | P2／重构 | 逐级缩减可选上下文并校准，保留明确最终失败边界 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)<br>[core/prompt/context_budgeter.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_budgeter.py) |
| B03 | **输出截断恢复**：finish_reason=length 首次进入 recover；第二次终止 | 工具参数截断后也走短回答恢复，无法继续创建 | P0／重构 | 分开回答截断与工具参数截断；后者增加预算、缩小批次或改引用提交 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)<br>[agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| B04 | **空回答恢复**：一次恢复；再次空回答失败 | 推理占满输出等情况可能连续触发 | P2／重构 | 识别输出预算、供应商错误与真实空输出，再选择重试方案 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| B05 | **工具协议被写成普通文本**：一次自然语言恢复；再次则 partial/failed | 把可修正的调用错误变成结束任务 | P1／重构 | 维持不可执行文本规则，但恢复时允许模型重新发合法工具调用 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| B06 | **无效工具调用恢复次数**：空/重复 ID、未下发工具等通常只给 1 次纠正 | 工具发现或路由错误难以恢复 | P1／删除 | 撤掉无效调用仅一次纠正的全局门禁；修复目录/schema/ID后继续目标，相同无效动作由进展检测处理 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| B07 | **回答/恢复阶段停发工具**：final 或 recover 时 tools=[]；只有少数补读例外 | 全局锁死执行，是本次 session 的核心阻断之一 | P0／删除 | 撤掉回答/恢复阶段一刀切停发工具；发现还需行动可回到执行，真实完成后才交付，合法调用和权限校验仍生效 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)<br>[agent/completion.py](E:/PROJECT/GenesisAI/src/genesisai/agent/completion.py) |
| B08 | **来源引用纠错与补读**：引用错误仅 1 次恢复；过期文件最多补读 3 个路径、1 批 | 大批量修改后无法一次恢复所有失效引用 | P1／删除 | 撤掉最多3路径和1批补读；失效来源按集合分页重读，保留真实性校验 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| B09 | **价格声明证据门禁**：数字须受已读取来源支持；仅 1 次纠正/补抓，候选最多 40 | 换算、组合价格或多来源比较可能被过早收口 | P2／重构 | 保留来源要求；支持明确计算过程，补证据按进展处理 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| B10 | **未知执行结果不自动重放**：恢复时已启动但结果未知的副作用工具不能自动再执行 | 可能等待核对，但可防重复修改/重复外部操作 | P1／保留 | 先查实际副作用或幂等凭据，再决定恢复；不能用放宽重试代替 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)<br>[core/tools/executor.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/executor.py) |

## C．模型输出、上下文与图片（15项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| C01 | **模型输出预算**：默认 max_tokens=2048；配置接受 1–384000；实际请求还与输出预留取 min | 思考与长 JSON 竞争输出空间；本次 4 次创建都被截断 | P0／放宽 | 按模型能力与任务类型配置；先联动工具提交方式、超时和恢复，不盲填最大值 | [model/default.yaml](E:/PROJECT/GenesisAI/src/genesisai/model/default.yaml)<br>[model/config.py](E:/PROJECT/GenesisAI/src/genesisai/model/config.py)<br>[agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| C02 | **输入上下文总预算**：默认 input_max/context_window 均 400000；预留输出 4096、安全余量 1024，默认有效 394880 | 超过时压缩/拒绝；大窗口并不覆盖其他局部限制 | P2／保留 | 与真实模型容量对齐；输出配置提高时预留会同步提高，不是独立固定 4096 上限 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py)<br>[core/prompt/context_budgeter.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_budgeter.py) |
| C03 | **历史与最近消息保留预算**：默认 history_tokens=32000、preserve_recent_tokens=20000，可配置 | 长任务早期决策被压缩，需要可检索任务记忆 | P1／重构 | 按任务状态保留关键约束/验收/未完成项；其余按预算压缩 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py)<br>[core/prompt/compaction.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/compaction.py) |
| C04 | **摘要模型预算**：默认摘要 2048 tokens；摘要文本另裁到 6000 字符，兜底片段 600 字符 | 多阶段任务细节可能在摘要中丢失 | P1／重构 | 结构化任务账本＋引用；摘要长度按任务复杂度配置，避免无提示二次截断 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py)<br>[core/prompt/compaction.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/compaction.py) |
| C05 | **必需上下文块各自设限**：ContextBlock 默认 6000 tokens；runtime 12000、每 Skill 8000；超额 required 直接失败 | 即使总上下文有余量，也可能无法启动下一轮 | P1／删除 | 撤掉必需上下文块独立小预算超额即报错的规则；按模型总预算统一分配、分层读取，约束保留完整来源 | [core/prompt/providers.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/providers.py)<br>[core/prompt/context_sources.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_sources.py) |
| C06 | **可选项目目录上下文裁剪**：notes 16 条、files 30 项、每文件 symbols 12 个；整个块 8000 tokens，超额整块不注入 | 中大型项目看见的目录少，超额时甚至全部省略 | P1／重构 | 按任务相关度检索，分页；逐项裁剪而不是整块丢弃 | [core/prompt/context_sources.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_sources.py)<br>[core/prompt/providers.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/providers.py) |
| C07 | **历史工具结果压缩**：较长工具结果约 4000 字符以上摘要化；字段缩短到 400/800 字符，列表前 12 项 | 先前工具结果细节可能不再直接可见 | P2／保留 | 保留按需压缩，但必须附可重读引用、截断范围和关键失败信息 | [core/prompt/compaction.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/compaction.py) |
| C08 | **压缩触发与估算校准**：约有效预算 90% 触发；估算校准 factor 范围 1–4 | 估算失真可能过早压缩或仍超限 | P2／保留 | 保留余量；用实际 usage 校准并监测，不直接删除安全边界 | [core/prompt/context_budgeter.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_budgeter.py) |
| C09 | **用户单次文本长度预检**：超过 3 × 有效输入 token 限额的字符数时拒绝 | 超长粘贴被挡住；字符与 token 估算不精确 | P3／删除 | 撤掉字符数超过3倍token额度的经验拒绝；按模型实际预算估算，超长输入保存引用并分段处理 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py) |
| C10 | **图片数量**：默认每请求最多 4 张，可配置；附件/MCP/上下文共用限制 | 多截图调试需要分批 | P2／删除 | 撤掉固定4张图片上限；按模型能力、视觉token、传输及解码预算分批加载 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py)<br>[app/attachments.py](E:/PROJECT/GenesisAI/src/genesisai/app/attachments.py)<br>[core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |
| C11 | **单张图片字节数**：默认 10 MiB，可配置 | 大截图/照片拒绝 | P3／保留 | 优先压缩或缩放；仅有实际需求再提高 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py) |
| C12 | **单张图片像素数**：默认 3200 万像素，可配置 | 超大图拒绝 | P3／保留 | 保留解码资源保护，提供分块读取 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py) |
| C13 | **图片长边**：默认缩到 1536，可配置 | 细小文字/大图细节损失 | P2／放宽 | 支持原图区域裁剪或多尺度查看，按任务调分辨率 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py) |
| C14 | **单请求图片总字节数**：默认 12 MiB，可配置 | 数张各自合规的图片也可能合计超额 | P3／保留 | 与模型及传输容量统一配置，支持分批 | [shared/context_config.py](E:/PROJECT/GenesisAI/src/genesisai/shared/context_config.py) |
| C15 | **历史图片只留引用/受当前预算重投影**：压缩/历史投影后需 read_image 重新加载 | 模型不能仅凭历史引用继续做视觉判断 | P2／保留 | 保留显式重读，任务账本记录待复查图片区域 | [core/prompt/compaction.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/compaction.py)<br>[core/prompt/context_sources.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_sources.py) |

## D．路由、工具发现与交互参数（10项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| D01 | **关键词路由与继续意图判定**：profile/protocol/续接依赖正则和有限短语 | 短句承接会丢失能力构建上下文 | P1／重构 | 持久化任务意图与阶段；文本路由只作提示，不重置已有目标 | [core/prompt/task_protocols.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/task_protocols.py) |
| D02 | **只读/禁网约束及继承**：按关键词识别，再过滤工具；续接中继承且需特定表达解除 | 可能把讨论阶段的限制错误带到执行阶段 | P1／重构 | 保留用户明确约束，改成有来源/作用域/期限的结构化约束与显式状态转换 | [core/prompt/task_protocols.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/task_protocols.py)<br>[core/tools/selection.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/selection.py) |
| D03 | **历史回顾专用工具集合**：history_lookup 时仅提供标记为历史查询的工具 | 混合‘回顾并继续’请求可能少工具 | P2／删除 | 撤掉历史回顾时关闭业务工具的封闭集合；回顾、查证、继续执行可在同一任务发生 | [core/tools/selection.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/selection.py) |
| D04 | **工具定义字符预算**：非受保护工具共享 24000 字符；基础/活动 Skill/待执行等可突破 | 复杂 MCP schema 会挤掉其他工具 | P1／重构 | 按 token 预算与任务阶段选工具，精简描述，按需装载完整 schema | [core/tools/selection.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/selection.py) |
| D05 | **近期工具保留窗口**：仅最近 6 次工具活动受保护 | 长链任务早期使用过的必要工具可能掉出目录 | P2／删除 | 撤掉最近6次活动决定工具保留的策略；任务阶段声明需求，必要工具不因窗口滚动而消失 | [core/tools/selection.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/selection.py) |
| D06 | **工具发现参数与结果上限**：query≤200 字符；默认返回 6、最大 12 | 复杂需求/大量外部工具需要多次查询 | P2／放宽 | 支持分页、按 provider/category 筛选及精确名称加载 | [core/extensions/tools/kernel/discover_tools/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/discover_tools/spec.py)<br>[core/tools/tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py) |
| D07 | **调用必须属于本轮下发目录**：无效 ID 或存在未提供工具时整批拒绝 | 一个错名阻塞同批合法工具 | P2／重构 | 保留合法目录校验；返回可恢复的发现路径，明确批次原子性 | [agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| D08 | **计划规模**：steps≤30、acceptance≤30、non_goals≤20；每项≤500 字符 | 项目级长计划塞不进单一平面数组 | P2／重构 | 支持阶段/子任务与增量更新，当前阶段保持简短 | [core/extensions/tools/kernel/plan/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/plan/spec.py) |
| D09 | **询问用户的参数**：question≤1500；最多 6 个选项，每项≤250 字符 | 一般足够；复杂评审需引用文档 | P3／保留 | 保持问题简短，完整材料通过文件/预览呈现 | [core/extensions/tools/kernel/ask_user/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/ask_user/spec.py) |
| D10 | **完成结果参数**：attempt_completion.result≤20000、command≤2000 字符 | 大型报告不适合直接塞返回字段 | P3／保留 | 返回摘要与产物引用；正文保存文件 | [core/extensions/tools/kernel/attempt_completion/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/attempt_completion/spec.py) |

## E．工具执行、文件读写与代码搜索（15项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| E01 | **通用工具执行超时**：ToolSpec 默认 30 秒；prepare/execute/observe 共用时限，并受任务剩余时间约束 | 大扫描或慢 MCP 可提前超时；副作用结果可能变 unknown | P1／重构 | 按工具声明分配、支持进度/后台执行和可取消续期 | [core/tools/base.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/base.py)<br>[core/tools/executor.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/executor.py) |
| E02 | **通用工具返回正文**：默认 12000 字符；editor 4000、memory/run_commands 24000、plan 8000、能力生命周期 16000 | 工具自己的大返回仍可能被外层再次裁剪；通常可经引用重读 | P1／重构 | 统一内外层预算，返回摘要＋可分页原文引用；避免矛盾参数 | [core/tools/base.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/base.py)<br>[core/tools/executor.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/executor.py)<br>[core/tools/tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py) |
| E03 | **本地读取/编辑文件大小**：公共路径上限 8 MiB | 大型日志、源码生成文件无法通过这些工具读取/编辑 | P1／重构 | 取消8MiB文件整体拒绝，改范围读取、流式搜索及定点编辑；控制单次内存而非文件整体可处理性 | [core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py)<br>[core/extensions/tools/kernel/editor/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/editor/implementation.py) |
| E04 | **文本类型/扩展名白名单**：读取与 editor 创建各有列表；未知后缀/二进制拒绝 | 新语言、Dockerfile 等扩展名外的文件可能被挡 | P1／删除 | 撤掉未知扩展名一律拒绝；按编码和内容识别文本，允许显式类型，二进制用对应处理器 | [shared/filetypes.py](E:/PROJECT/GenesisAI/src/genesisai/shared/filetypes.py)<br>[core/extensions/tools/kernel/editor/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/editor/implementation.py) |
| E05 | **通用文档提取正文上限**：extract 最多 200000 字符；不是纯文本 read_files 分页的总上限 | 大文档后半部分无法通过提取结果的 offset 找回 | P1／删除 | 撤掉仅保存前200000字符的总量截断；完整受控源可按范围读取，尾部始终有可达引用 | [core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py) |
| E06 | **文件读取批量与正文**：每次≤50 文件；默认每文件 8000、总 24000 字符；可提到每文件/总量 200000 | 限制单次吞吐，可用 remaining_requests 续读 | P2／保留 | 保留有界批量与续读；让外层结果预算一致，按模型余量调默认值 | [core/extensions/tools/kernel/read_files/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/read_files/spec.py)<br>[core/extensions/tools/kernel/read_files/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/read_files/implementation.py) |
| E07 | **分页/路径参数数值边界**：常见路径≤2000；read_files 单路径行号≤100万、offset≤1亿；list/search offset≤20万 | 巨大日志/目录边界人为且不同入口不一致 | P2／重构 | 取消人为offset/行号最大值，按真实范围验证；路径遵守平台及授权规则，统一单文件和批量参数 | [core/extensions/tools/kernel/read_files/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/read_files/spec.py)<br>[core/extensions/tools/kernel/list_files/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/list_files/spec.py)<br>[core/extensions/tools/kernel/search_codebase/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/spec.py) |
| E08 | **目录总扫描上限**：list_files 只扫描前 1000 个文件，返回每页 100；后续 offset 也突破不了扫描上限 | 中大型仓库存在永远看不到的文件 | P1／删除 | 删除前 1000 总量硬截断，保留每页预算并返回可续扫游标 | [core/extensions/tools/kernel/list_files/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/list_files/implementation.py) |
| E09 | **扫描目录排除与链接不遍历**：walk 默认跳过 .git/.venv/node_modules/__pycache__，不跟随目录符号链接/junction | 查依赖实现、链接源码时默认不可达 | P2／重构 | 保留默认排除与防环；允许指定目录定向扫描及显式 include | [core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py) |
| E10 | **编辑请求正文与批量补丁**：content/old_text/new_text/patch≤100000 字符；一批≤20 个补丁；source_refs≤100 | 大型生成/多文件重构需拆分多次调用 | P2／重构 | 改为小内容内联、文件/产物引用及增量补丁；批次按资源拆分，取消固定20项和整段源码必须进入参数的假设 | [core/extensions/tools/kernel/editor/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/editor/spec.py) |
| E11 | **搜索页大小与上下文**：limit≤100，默认内容 30/index 100；context_lines≤40；max_output_chars≤100000，默认24000 | 一般可分页；全仓批量处理需要多轮 | P2／保留 | 保留单页预算与游标，提供计数/文件列表结果，避免盲增正文 | [core/extensions/tools/kernel/search_codebase/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/spec.py)<br>[core/extensions/tools/kernel/search_codebase/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/implementation.py) |
| E12 | **搜索表达式与过滤参数**：query≤2000；glob≤200；exclude≤50；depth≤1000；cursor≤512 | 多数合理，复杂排除配置可能超出 | P3／保留 | 支持引用项目配置以承载复杂过滤；统一文档说明 | [core/extensions/tools/kernel/search_codebase/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/spec.py) |
| E13 | **代码索引模式单文件截断**：search_codebase(mode=index) 仅前 200000 字符；汇总 TODO≤50、单条120字符 | 后半部符号/TODO 缺失；不等于普通内容搜索只查前20万字 | P1／删除 | 撤掉仅索引前200000字符及TODO前50个的不可续读规则；完整分片索引，结果游标分页 | [core/extensions/tools/kernel/search_codebase/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/implementation.py) |
| E14 | **搜索结果局部裁剪**：before/after 每行仅 240 字符；skipped 最多50项 | 长行及大量跳过原因难以诊断 | P2／重构 | 保留短预览，返回完整行/跳过清单引用，注明省略数量 | [core/extensions/tools/kernel/search_codebase/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/search_codebase/implementation.py) |
| E15 | **编辑冲突、覆盖与来源约束**：创建不覆盖已有文件；补丁校验前态/匹配；源码不能附报告来源；带来源报告限 MD/TXT | 防覆盖值得保留，报告格式约束会增加操作成本 | P2／重构 | 保留并发冲突与不可误覆盖；来源改元数据，取消格式耦合 | [core/extensions/tools/kernel/editor/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/editor/implementation.py)<br>[shared/changes.py](E:/PROJECT/GenesisAI/src/genesisai/shared/changes.py) |

## F．项目索引与长期记忆（9项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| F01 | **项目索引文件数**：refresh 默认 max_files=5000；不是前台所有搜索的共同上限 | 大仓库索引不完整，重复全量刷新不能自然覆盖余下文件 | P1／删除 | 撤掉项目仅覆盖前5000文件的总量上限；5000如保留只作单批大小，持久游标持续推进 | [core/memory/project_index.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/project_index.py) |
| F02 | **索引单文件大小与文本体积**：跳过>1 MiB文件；检索正文只收前128000字符 | 大文件的后半内容无法通过索引召回 | P1／重构 | 取消大于1MiB源码一律跳过及128000字符不可续读前缀；按符号/行分片覆盖受授权项目 | [core/memory/project_index.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/project_index.py)<br>[core/memory/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/service.py) |
| F03 | **索引隐私/范围过滤**：跳过 Git ignore、敏感文件、链接和未授权代码根 | 索引不等于整个磁盘；默认保护必要 | P2／保留 | 允许显式设置额外代码根/非敏感 include；不自动索引密钥 | [core/memory/project_index.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/project_index.py) |
| F04 | **索引 Git 读取时限**：Git ls-files/check-ignore 各10秒 | 大型仓库或慢盘可能无法建立索引 | P2／放宽 | 按仓库规模配置并缓存；长扫描分阶段可取消 | [core/memory/project_index.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/project_index.py) |
| F05 | **记忆检索查询与结果**：query≤500字符；只取前12检索词；默认8/最多20条结果 | 复杂历史问题召回不足且无大批量结果入口 | P2／重构 | 支持分页/语义子查询，明确查询被裁剪的部分 | [core/memory/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/service.py)<br>[core/extensions/tools/kernel/memory_search/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/memory_search/spec.py) |
| F06 | **记忆正文读取页大小**：默认6000、最大16000字符，支持offset | 长文件/工具溢出结果需频繁翻页 | P2／放宽 | 按模型上下文动态调页大小；保留全文引用和游标 | [core/memory/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/service.py) |
| F07 | **记忆写入体积与证据**：title≤200、body≤32000、sources 1–20；更新须正确revision与可验证来源 | 大型设计文档不宜整篇写成单条记忆 | P2／重构 | 保留版本/来源；正文允许引用文档并拆分事实条目 | [core/memory/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/service.py)<br>[core/extensions/tools/kernel/memory_write/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/memory_write/spec.py) |
| F08 | **任务交接摘要**：任务标题200、交接回答4000字符 | 阶段交接可能丢失未完成项 | P1／重构 | 交接存结构化待办/约束/验证/引用，摘要仅作入口 | [core/memory/tasks.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/tasks.py) |
| F09 | **记忆维护调度量**：每次 prepare/finish 处理最多2项维护任务；维护默认1项 | 大规模索引更新可长期积压 | P2／放宽 | 独立后台维护与队列状态；按空闲资源处理，不阻塞每轮模型 | [core/tools/tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py)<br>[core/memory/maintenance.py](E:/PROJECT/GenesisAI/src/genesisai/core/memory/maintenance.py) |

## G．联网检索与网页采集（13项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| G01 | **检索/抓取档位预算**：direct=0/0；quick=2/4；normal/local=4/8；deep=8/12；creative=2/4（搜索/抓取） | 长调研/跨文档开发的网页额度偏小 | P1／删除 | 撤掉quick/normal/deep固定搜索/抓取次数；统一消耗用户任务预算，按证据缺口决定下一步 | [shared/budgets.py](E:/PROJECT/GenesisAI/src/genesisai/shared/budgets.py)<br>[core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| G02 | **无新增证据停止**：研究流程连续3轮进展标记不变会禁用web能力；部分档位强制回答 | 推理/规划期间未抓新网页也可能被当停滞 | P1／删除 | 撤掉连续3轮无新网页就禁网/强答；判断实际子目标进展，分析既有资料也可以有进展 | [core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| G03 | **同一URL失败重试**：可重试候选实际尝试最多2次；403/404等不可重试 | 短暂站点故障后无法继续补取 | P2／重构 | 按HTTP状态、退避与总时间控制；不重复请求永久失败资源 | [core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| G04 | **URL必须预先登记**：只抓用户输入/搜索/受认可链接等候选；批准不能替代登记 | 合理拼接的文档子页也可能被拒绝 | P1／删除 | 撤掉候选登记作为URL访问前置权限；通过网络授权和地址安全校验即可读取，登记仅用于溯源 | [core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| G05 | **搜索结果页禁止抓取**：禁止直接抓指定搜索引擎结果页 | 限制备用检索入口 | P2／删除 | 撤掉按搜索引擎域名一刀切禁止抓取；优先检索接口，也允许读取可访问页面，遵守实际访问边界 | [core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| G06 | **网页客户端网络边界**：仅HTTP(S)公网；拒绝私网/保留地址/混合DNS；固定解析IP | 无法读本地服务、企业内网文档 | P2／重构 | 保留公网工具的SSRF保护；另设可授权内网能力/域名范围 | [core/extensions/tools/web/network.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/network.py)<br>[core/extensions/tools/web/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/contracts.py) |
| G07 | **网页超时及重定向**：客户端15秒；Brave/DDG10秒；网络最多6次循环（最多5跳后取正文）；带认证头不跟随跳转 | 慢网页易失败；各层时限不一致 | P2／放宽 | 统一网络超时预算；保留跳转上限，跨域认证头不得自动转发 | [core/extensions/tools/web/network.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/network.py)<br>[core/extensions/tools/web/providers/brave.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/providers/brave.py)<br>[core/extensions/tools/web/providers/duckduckgo.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/providers/duckduckgo.py) |
| G08 | **网页响应与解压大小**：响应/解压正文≤4 MiB | 大型文档/PDF无法读取 | P2／放宽 | 流式下载、单资源预算与附件读取路径；保留解压保护 | [core/extensions/tools/web/network.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/network.py) |
| G09 | **网页正文/PDF提取总量**：网页正文只存前200000字符；网络PDF只提取前30页；读取默认4000/最多200000字符一页 | 分页只在已存前缀内，无法补读原文尾部 | P1／删除 | 撤掉网页前20万字符/网络PDF前30页的不可恢复截断；流式保存受控源，全文按范围可达 | [core/extensions/tools/web/network.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/network.py)<br>[core/extensions/tools/kernel/fetch_web_content/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/fetch_web_content/implementation.py) |
| G10 | **网页类型与渲染边界**：只取HTML/纯文本/PDF；无JavaScript渲染或OCR | 动态文档和扫描件无正文 | P2／重构 | 增加独立浏览器/文档OCR能力，明确调用条件 | [core/extensions/tools/web/network.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/network.py) |
| G11 | **搜索查询与结果数**：schema query≤500，SearchQuery 实际≤400字符/50词；每次固定最多5条结果 | 参数契约不一致；全面检索召回少 | P1／重构 | 统一schema与运行时约束；开放结果数量/分页/过滤参数 | [core/extensions/tools/web/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/contracts.py)<br>[core/extensions/tools/kernel/fetch_web_content/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/fetch_web_content/spec.py)<br>[core/extensions/tools/kernel/fetch_web_content/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/fetch_web_content/implementation.py) |
| G12 | **页面链接发现边界**：仅同源；最多扫描200个链接；返回40个/6000字符；URL≤600；自动登记深度<2 | 文档导航深、跨站引用和长URL无法顺畅续查 | P1／重构 | 可配置深度、跨域引用和分页；保留访问资源预算 | [core/extensions/tools/web/links.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/links.py)<br>[core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| G13 | **结构化网页事实裁剪**：HTML前150万字符、JSON-LD 25万字符、事实最多20项 | 复杂产品页信息缺失 | P2／重构 | 按目标字段提取，返回未处理块引用，避免前缀丢失 | [core/extensions/tools/web/structured_data.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/structured_data.py) |

## H．终端命令与后台作业（9项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| H01 | **命令时长与后台寿命**：默认run 120秒，schema最大600秒；前台外层605秒；后台任务同样受所给600秒上限 | 大型测试/编译超过10分钟会被终止，后台不是无限运行 | P1／删除 | 撤掉后台作业600秒寿命；前台等待到点返回job_id，作业继续到完成、取消、显式预算耗尽或确认卡死 | [core/extensions/tools/kernel/run_commands/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/run_commands/spec.py)<br>[core/extensions/tools/kernel/run_commands/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/run_commands/implementation.py)<br>[core/extensions/tools/shell/jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py) |
| H02 | **后台作业并发数**：每会话最多4个活动作业 | 多包并行测试受限 | P2／重构 | 取消固定4作业业务上限；按CPU/内存/用户并发配置调度，资源冲突加锁 | [core/extensions/tools/shell/jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py) |
| H03 | **后台作业日志总量**：最多保存256000字符；超长单行约32768字节整行省略 | 长测试末尾失败报告可能根本未保存 | P1／删除 | 撤掉256000字符后丢尾及长行静默省略；日志分块保存，配额不足显式处理，未经确认不删除用户日志 | [core/extensions/tools/shell/jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py) |
| H04 | **前台命令输出预览**：每流返回12000字符；内部摘要16384，完整日志另存 | 模型只看开头可能漏掉最后的测试失败摘要 | P1／重构 | 返回头尾摘要、退出码和完整日志引用，测试结果从可靠全日志/结构化文件提取 | [core/extensions/tools/shell/process.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/process.py)<br>[core/extensions/tools/kernel/run_commands/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/run_commands/implementation.py) |
| H05 | **读取作业的单次等待与页大小**：wait_seconds≤30；limit≤8000，默认4000；不是作业总寿命 | 合理轮询会消耗工具次数 | P2／保留 | 保留短等待，增加完成事件唤醒，等待不宜挤占业务工具预算 | [core/extensions/tools/kernel/read_job/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/read_job/spec.py)<br>[core/extensions/tools/shell/jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py) |
| H06 | **命令参数与批处理表达能力**：argv≤64，每参数≤4000；BAT/CMD参数不能含双引号或换行 | 大型命令/内联脚本受限 | P2／放宽 | 支持脚本文件/响应文件/输入引用；依据OS能力验证参数 | [core/extensions/tools/shell/commands.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/commands.py)<br>[core/extensions/tools/kernel/run_commands/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/run_commands/spec.py) |
| H07 | **子进程交互入口**：命令stdin=DEVNULL；无该工具的交互stdin/PTY接口 | 交互式安装器、REPL、凭据提示不能继续输入 | P2／重构 | 提供显式交互作业接口；自动化命令仍优先非交互 | [core/extensions/tools/shell/process.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/process.py)<br>[core/extensions/tools/shell/jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py) |
| H08 | **项目/测试自动探测范围**：相对文件扫描最多5000；测试/依赖/HTML展示各100；样例测试只看前8000字符 | 复杂项目误判测试入口；仍可显式指定command | P2／重构 | 以项目配置/用户指定入口优先，分包探测并标注覆盖范围 | [core/extensions/tools/shell/python_support.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/python_support.py) |
| H09 | **作业归属与恢复**：作业归属本会话/宿主进程；宿主不在时标 owner_unavailable | 无法无缝接管跨CLI重启的长作业 | P1／重构 | 若需跨重启长任务，增加持久作业宿主与结果对账，禁止猜测仍在运行 | [core/extensions/tools/shell/jobs.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/shell/jobs.py) |

## I．外挂能力创建、测试与注册（20项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| I01 | **创建/修订必须一次提交完整包**：files正文、definition与tests通过一个JSON调用提交；修订也交完整新版本 | 长参数极易截断；模型重复输出已有文件 | P0／重构 | 先建草稿，再引用工作区文件或逐文件更新，最后冻结版本测试 | [core/capabilities/tools.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/tools.py)<br>[core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py)<br>[core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py) |
| I02 | **能力包总体积**：序列化JSON≤2000000字节，包含测试和夹具 | 中大型插件包及测试数据容纳不足 | P1／重构 | 取消2MB完整JSON包假设；清单、代码、资产、夹具分离并按哈希引用，使用显式存储配额和导入资源保护 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py) |
| I03 | **包内文件数量与类型**：files最多100项；每份fixtures也最多100项；仅无NUL文字 | 二进制资源、图片、数据库夹具无法直接携带 | P1／重构 | 取消最多100个文字文件的包模型；采用文件清单/目录快照/二进制资产，控制单批传输与总存储资源 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py)<br>[core/capabilities/tools.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/tools.py) |
| I04 | **名称/描述/包路径长度**：name 2–48小写字母数字下划线；description≤500；相对路径≤240字符 | 普通能力够用，深目录和已有项目名可能不适配 | P3／保留 | 名称用稳定ID，展示名可另设；路径按平台与包格式统一 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py) |
| I05 | **能力入口与运行格式**：Tool入口必须包内.py；Skill入口.md；工具stdin/stdout使用JSON且结果须object；MCP command最多50项 | 已有Node/可执行程序工具需要Python包装 | P2／重构 | 新增明确的运行器类型/命令契约；保留schema与输出校验 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py)<br>[core/capabilities/execution.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/execution.py) |
| I06 | **能力单次超时**：Tool/MCP定义默认30、可设1–120秒；Tool注册外层额外留5秒 | 长测试/外部操作受2分钟上限阻断；MCP正式调用另有配置丢失问题 | P1／重构 | 取消1–120秒作为能力总寿命；长执行统一为可取消作业，短执行直接返回，测试与正式运行共用配置 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py)<br>[core/capabilities/execution.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/execution.py)<br>[core/capabilities/activation.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/activation.py) |
| I07 | **测试/试用生命周期总时限**：test_capability/try_capability外层300秒；所有用例共享 | 20个合法长用例仍可能来不及跑完 | P1／重构 | 测试套件后台化，分用例保存结果、可恢复；整体有独立预算 | [core/capabilities/tools.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/tools.py)<br>[core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py) |
| I08 | **测试用例数量**：1–20个，至少一个预期成功；每例需expect或expect_error | 20例不足以承载成熟工具测试集 | P1／删除 | 撤掉最多20个测试；保留至少一个预期成功及实际测试通过要求，支持完整测试套件分批执行 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py) |
| I09 | **外挂Tool输出总量**：stdout+stderr合计≤1000000字节，超限失败 | 调试日志或大结构化结果导致整个执行失败 | P2／重构 | 结果与日志分通道；大结果返回文件引用，日志有界分段 | [core/capabilities/execution.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/execution.py) |
| I10 | **Skill试运行预算**：最多12轮、24工具、120秒；每次模型请求≤30秒，仍受父级剩余时间约束 | 稍复杂Skill很难完成真实验收 | P1／删除 | 撤掉Skill专属12轮/24工具/120秒小预算；使用测试场景的显式预算和统一执行器，传递父级取消及总预算 | [core/capabilities/skill_trial.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/skill_trial.py) |
| I11 | **Skill依赖数量与可用性**：最多50个不重复工具，声明依赖须当前可用 | 大流程依赖多或可选依赖暂不可用时无法加载 | P2／删除 | 撤掉Skill最多50依赖的数量上限；使用依赖图、必需/可选标记和按阶段加载，保留依赖校验及循环检测 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py)<br>[core/extensions/skills/registry.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/skills/registry.py)<br>[core/capabilities/activation.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/activation.py) |
| I12 | **Skill试运行工具范围**：仅声明工具＋attempt_completion；禁递归能力构建；部分HostBound依赖不能隔离测试 | 组合能力/需要宿主状态的Skill无法完整测试 | P2／重构 | 保留最小权限与防递归；建立可隔离宿主服务适配接口 | [core/capabilities/skill_trial.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/skill_trial.py) |
| I13 | **Skill测试产物采集**：只收≤200000字节的UTF-8文件；总文字>500000字符报错；其他文件不进入files结果 | 大文本/文档/图片产物无法按同一返回结构验收 | P1／重构 | 采集产物清单、哈希、类型与可读引用，测试按产物类型验证 | [core/capabilities/skill_trial.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/skill_trial.py) |
| I14 | **测试工作目录与链接限制**：隔离工作目录＋显式夹具；不跟随链接/junction；并非OS沙箱 | 不能自然使用真实项目全部数据 | P2／保留 | 保留隔离默认值；允许显式选择快照/只读输入挂载，避免污染真实项目 | [core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py)<br>[core/capabilities/files.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/files.py)<br>[core/capabilities/skill_trial.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/skill_trial.py) |
| I15 | **注册前实际测试通过**：当前版本必须有匹配digest的通过报告；试用不能代替全部测试 | 必要验收门禁，会增加创建流程步骤 | P1／保留 | 这是用户明确要求；优化测试执行和报告，不绕过测试 | [core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py) |
| I16 | **用户明确验收后注册**：register requires_acceptance；auto模式也要确认 | 长任务在真正注册处等待用户 | P1／保留 | 这是用户明确的‘满意后注册’；准备完整可审查结果后一次确认 | [core/capabilities/tools.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/tools.py)<br>[core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py)<br>[core/tools/permissions.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/permissions.py) |
| I17 | **修订/报告/验收绑定**：预期revision、digest、当前report必须一致；修订/重测后旧验收失效 | 避免测试A却注册B；需要重新验收发生变化的结果 | P1／保留 | 保留不可变版本及乐观并发控制；界面清楚展示变化 | [core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py)<br>[core/capabilities/store.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/store.py) |
| I18 | **名称和作用域冲突**：外挂不得隐式覆盖内置；user/project同名已激活能力不可隐式互盖 | 同名多版本需要改名或明确停用 | P2／保留 | 支持显式替换/版本选择流程，仍禁止暗中覆盖 | [core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py)<br>[core/capabilities/activation.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/activation.py) |
| I19 | **能力密钥注入**：MCP只存env_from变量名映射，不接收明文env；缺失变量时不能启用 | 连接需先配置凭据 | P2／保留 | 保留凭据外置；完善缺失变量诊断与安全配置入口 | [core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py)<br>[core/capabilities/execution.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/execution.py) |
| I20 | **能力永久化触发条件**：提示规定仅用户要求复用能力/创造模式时构建永久能力；不再自动加载旧目录或mcp.json | 临时任务不会自动安装新能力 | P2／保留 | 符合本轮产品决定；临时脚本与永久注册分清，勿加入历史兼容层 | [core/prompt/templates/protocols/capability_builder.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/capability_builder.prompt.yaml)<br>[core/extensions/skills/registry.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/skills/registry.py)<br>[core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |

## J．MCP、Skill、规则与语言服务（11项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| J01 | **MCP接入传输**：只提供command/stdio；无原生HTTP远程连接定义 | 接入HTTP MCP需桥接程序，显著增加创建与维护工作 | P1／重构 | 定义stdio与远程传输配置，按协议实现连接/认证/重连；避免每个服务自写桥 | [core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py)<br>[core/capabilities/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/contracts.py) |
| J02 | **MCP消息大小**：文本流单行读取最多32×1024×1024＋1字符；超长或无换行拒绝 | 大型响应受限；不是严格按字节计数的32 MiB | P3／保留 | 改统一字节/消息预算并提供大资源读取，不开放无限消息 | [core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |
| J03 | **MCP工具目录规模**：工具总数≤5000；分页cursor去重且最多100个后续游标 | 超大服务目录无法完整加载 | P3／重构 | 目录按需分页/检索，不因超过5000工具或100个不同游标永久不可用；保留重复游标检测与单响应资源保护 | [core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |
| J04 | **MCP名称、描述与资源参数**：生成工具名裁到63字符；描述300字符；resource uri≤8192、cursor≤4000 | 长名称可能碰撞，描述丢失会影响选择 | P2／重构 | 名称用稳定短ID映射防碰撞；摘要与完整描述分层 | [core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |
| J05 | **MCP转为网页候选的范围**：只从structuredContent.items提取；前40项/每项links前8值，总40 URL，URL≤2000 | 非该结构的合法链接不会登记，后续网页读取被拦 | P1／重构 | 独立可解释的链接提取/引用协议；不与网页可访问权限强绑定 | [core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py)<br>[core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py) |
| J06 | **MCP客户端方法支持边界**：收到服务端请求只支持ping，其余返回Unsupported client method；握手能力为空 | 需要sampling/elicitation等能力的服务器不能完整工作 | P2／重构 | 按实际产品需求显式实现并声明支持能力，不能假装支持 | [core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |
| J07 | **活动Skill数量**：最多2个（主＋辅助）；registry、持久化、上下文三处一致 | 长任务跨多领域无法组合多个Skill | P1／删除 | 撤掉最多2个Skill及固定主辅模型；按阶段、依赖和总上下文预算激活/卸载 | [core/extensions/skills/registry.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/skills/registry.py)<br>[core/prompt/context_sources.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/context_sources.py)<br>[core/persistence/validation.py](E:/PROJECT/GenesisAI/src/genesisai/core/persistence/validation.py) |
| J08 | **AGENTS.md读取与范围**：仅用户根和工作区根；每份≤8000字符；拒绝链接；超限直接报错 | 大型项目规则或子目录规则无法正常表达 | P1／重构 | 支持按路径作用域加载及预算分层；保留规则来源与防冒用 | [core/extensions/rules/__init__.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/rules/__init__.py) |
| J09 | **工作流文本**：只扫描workflows/*.md；名称≤64；正文只取前12000字符 | 长工作流尾部被直接丢弃 | P1／删除 | 撤掉工作流只取12000字符的静默截断；完整文件可寻址，按阶段展开，保留合法名称与来源校验 | [core/extensions/workflows/__init__.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/workflows/__init__.py) |
| J10 | **LSP响应与查询时限**：工具45秒；消息体≤8MiB；头最多32行、每行2048字节 | 大语言服务冷启动/大查询可能超时 | P2／放宽 | 区分启动与查询时限、复用服务；保留协议消息上限 | [core/extensions/lsp/client.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/lsp/client.py)<br>[core/extensions/lsp/extension.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/lsp/extension.py) |
| J11 | **LSP能力/结果范围**：仅definition/references；最多100个位置；只返回工作区内file URI | 大重构引用不全，跨根依赖被省略 | P2／重构 | 增加分页及授权多根；按需扩展rename/diagnostics等，标清未实现能力 | [core/extensions/lsp/client.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/lsp/client.py)<br>[core/extensions/lsp/extension.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/lsp/extension.py) |

## K．权限、数据完整性与授权边界（8项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| K01 | **user模式逐次确认范围**：网络、写入、shell、服务启动等通常需确认；auto可放行，强验收另算 | 长任务重复确认很多；不能误称所有模式都被挡 | P1／重构 | 保留用户模式选择，增加任务/工具/路径范围授权及明确撤销 | [core/tools/permissions.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/permissions.py) |
| K02 | **确认有效期与内容绑定**：确认预览900秒到期；参数/资源改变则失效 | 人离开一段时间后需重新确认 | P2／重构 | 撤掉普通任务范围授权固定15分钟失效；绑定会话/任务、范围和撤销，具体注册验收仍绑定版本、报告和预览 | [core/tools/permissions.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/permissions.py) |
| K03 | **文件访问授权根/输出目录**：普通模式限grants/workspace/output；auto host_access允许更多宿主路径；设备路径仍限制 | 跨仓库任务需配置多个根或相应权限模式 | P2／保留 | 支持清楚的多根授权，不取消范围核验 | [shared/security.py](E:/PROJECT/GenesisAI/src/genesisai/shared/security.py)<br>[core/tools/tool_runtime.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/tool_runtime.py) |
| K04 | **应用状态与密钥文件保护**：文件工具禁止访问内部状态路径和.env/.env.*，auto也不例外 | 某些配置任务需专门入口；此规则不等于OS沙箱 | P2／保留 | 通过配置/会话诊断专用接口完成操作，输出密钥脱敏 | [shared/security.py](E:/PROJECT/GenesisAI/src/genesisai/shared/security.py)<br>[shared/paths.py](E:/PROJECT/GenesisAI/src/genesisai/shared/paths.py) |
| K05 | **子进程环境变量过滤**：环境变量名包含KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL被剔除 | 构建/部署依赖的凭据和某些误匹配变量无法自然继承 | P1／重构 | 保留默认隔离；按已授权命令/能力显式映射环境，避免纯子串误杀 | [shared/process_tree.py](E:/PROJECT/GenesisAI/src/genesisai/shared/process_tree.py) |
| K06 | **参数schema与外部引用边界**：内置schema拒绝未知参数；外挂schema校验；远程JSON Schema引用不会自动联网解析 | 拼错参数会失败，远程schema需要先解析/打包 | P2／保留 | 扩宽业务字段而非关闭类型校验；支持受控预解析依赖 | [core/tools/base.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/base.py)<br>[core/tools/external_schema.py](E:/PROJECT/GenesisAI/src/genesisai/core/tools/external_schema.py) |
| K07 | **当前状态格式与完整性校验**：不兼容的持久化状态拒绝；来源失效需重读；内容哈希/修订用于对账 | 旧格式不可强行继续，已改动文件不能沿用旧快照 | P2／保留 | 按用户要求维护当前格式，不新增历史迁移/兼容回退；错误清楚可诊断 | [core/persistence/validation.py](E:/PROJECT/GenesisAI/src/genesisai/core/persistence/validation.py)<br>[core/persistence/session.py](E:/PROJECT/GenesisAI/src/genesisai/core/persistence/session.py) |
| K08 | **删除与覆盖的数据保护**：仓库约定删除用户文件/日志/历史先列路径确认；文件整理Skill另要求删除确认；不静默覆盖外部变更 | 破坏性步骤可能等待确认 | P1／保留 | 这是用户明确约定；任务授权范围内的可逆编辑继续推进，不能把保护泛化到所有写操作 | [AGENTS.md](E:/PROJECT/GenesisAI/AGENTS.md)<br>[core/extensions/skills/builtin/file_organization/SKILL.md](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/skills/builtin/file_organization/SKILL.md)<br>[shared/changes.py](E:/PROJECT/GenesisAI/src/genesisai/shared/changes.py) |

## L．办公文档、媒体与长篇内容（11项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| L01 | **压缩文档解包规模**：类型识别≤64MiB/5000个ZIP条目；通用DOCX/XLSX提取另限32MiB | 同一文档在不同入口表现不一致 | P2／重构 | 统一解包预算、流式处理，保留压缩炸弹防护 | [shared/filetypes.py](E:/PROJECT/GenesisAI/src/genesisai/shared/filetypes.py)<br>[core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py) |
| L02 | **DOCX读取/创建/替换规模**：通用提取3000段/50表/每表200行/每行50格；专用读2000段/50表/200行；创建1000段×5000字/表500行；替换查找2000/替换5000字 | 长报告读取尾部缺失、写入需分批 | P2／重构 | 按段落/表格范围分页与批量追加；不同入口统一coverage | [core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py)<br>[core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py)<br>[core/extensions/tools/office/docx_create/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/docx_create/spec.py)<br>[core/extensions/tools/office/docx_edit/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/docx_edit/spec.py) |
| L03 | **XLSX读取规模**：通用10表、专用20表；值最多1000行×50列；公式/样式坐标各前2000个 | 大表尾部及更多sheet缺失 | P2／重构 | 按sheet/range读取与分页；公式位置单独查询 | [core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py)<br>[core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py) |
| L04 | **PPTX读取与创建规模**：读取前200页；创建≤100页 | 大型课程/汇报被截断或无法单次生成 | P2／删除 | 撤掉PPTX前200页读取/100页创建总量上限；按页读取和分批追加，受实际资源配额约束 | [core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py)<br>[core/extensions/tools/office/pptx_create/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/pptx_create/spec.py) |
| L05 | **本地PDF读取页数**：通用提取前50页；专用pdf_read前200页；无OCR，受支持路径不解密 | 长PDF尾部无法读取，扫描件无法提取 | P2／重构 | 页范围/分页读取；OCR另设能力，授权密码使用独立输入 | [core/extensions/tools/filesystem/shared.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/filesystem/shared.py)<br>[core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py) |
| L06 | **PDF生成文本规模**：最多5000行，每行schema1000字符；实际drawString只取120字符 | 不只是任务变慢，会丢掉成品文字 | P1／删除 | 撤掉PDF每行120字符静默裁切及固定总行数上限；自动换行、分页，输入按块处理 | [core/extensions/tools/office/pdf_create/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/pdf_create/spec.py)<br>[core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py) |
| L07 | **复杂文档编辑与验证边界**：宏/外部链接/嵌入对象拒绝有限编辑；公式不重算；PPTX基础布局/PDF基础文本；仅结构重开验证 | 复杂交付无法保证保真/视觉正确 | P2／保留 | 保持诚实能力边界；新增专用编辑/重算/渲染验证后再扩展，不仅解除拒绝 | [core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py)<br>[core/prompt/templates/protocols/office.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/office.prompt.yaml) |
| L08 | **Office产物只写新路径**：使用Access.write，已存在目标报错 | 反复修改产物要不断改文件名 | P2／重构 | 显式授权覆盖＋前态哈希＋临时文件原子替换/版本记录 | [shared/security.py](E:/PROJECT/GenesisAI/src/genesisai/shared/security.py)<br>[core/extensions/tools/office/helpers.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/office/helpers.py) |
| L09 | **静态图片类型与音视频边界**：只支持PNG/JPEG/WebP；提示不尝试音频或视频 | 相关任务无法通过内置多模态路径完成 | P2／重构 | 按需求增设解码/转写/抽帧能力；格式支持和任务禁令同步更新 | [core/media/images.py](E:/PROJECT/GenesisAI/src/genesisai/core/media/images.py)<br>[core/prompt/templates/protocols/local_files.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/local_files.prompt.yaml) |
| L10 | **章节正文读取页大小**：chapter_read默认20000、最大50000字符，支持offset；chapter_list使用glob扫描，没有1000文件硬上限 | 长篇正文需分页；不能把list_files的总扫描限制套到章节目录 | P3／保留 | 保留有界分页，按任务预算调整页大小；章节目录覆盖不受E08的1000文件限制 | [core/extensions/tools/novel/chapter_read/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/novel/chapter_read/spec.py)<br>[core/extensions/tools/novel/chapter_read/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/novel/chapter_read/implementation.py)<br>[core/extensions/tools/novel/chapter_list/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/novel/chapter_list/implementation.py) |
| L11 | **小说大纲/一致性启发式范围**：大纲段落只看前5000字，标题返回20/段落10、摘要200；人物前3000字/最多15；一致性人物提取前5000字、名字/提示各20 | 不代表全书完整审查，容易漏后段问题 | P2／重构 | 分章节结构化抽取与跨章事实表；每块有覆盖率与待处理游标 | [core/extensions/tools/novel/chapter_outline/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/novel/chapter_outline/implementation.py)<br>[core/extensions/tools/novel/consistency_check/implementation.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/novel/consistency_check/implementation.py) |

## M．提示词中的额外行为限制（7项）

| 编号 | 限制及当前值 | 长任务影响 | 等级／建议 | 参考解决方法 | 修改范围 |
| --- | --- | --- | --- | --- | --- |
| M01 | **coding禁止Git写操作**：coding提示绝对‘不执行Git写操作’，git_read提示却允许用户要求且已授权的写操作 | 用户已要求提交时仍可能拒绝；两份提示冲突 | P1／删除 | 删除coding绝对禁令，统一按用户授权与命令风险执行 | [core/prompt/templates/protocols/coding.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/coding.prompt.yaml)<br>[core/prompt/templates/protocols/git_read.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/git_read.prompt.yaml) |
| M02 | **不得修改测试的过宽措辞**：coding/testing同时有‘不修改测试来掩盖缺陷’及更宽的‘修复产品代码而非修改测试’ | 需求改变、测试本身错误或接口替换时可能拒绝同步测试 | P1／删除 | 删除一刀切不得改测试；需求、接口或测试错误时同步更新，保留不得掩盖缺陷及伪造验证的要求 | [core/prompt/templates/protocols/coding.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/coding.prompt.yaml)<br>[core/prompt/templates/protocols/testing.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/testing.prompt.yaml) |
| M03 | **源码写入限定editor**：提示要求实际开发必须经editor写源码，开发恢复也点名editor | 不利于代码生成器、格式化器、迁移工具等正规项目工作流 | P1／删除 | 删除源码只能由editor写入；允许生成器、格式化器、脚本和项目工具链，统一记录变更并验证 | [core/prompt/templates/protocols/coding.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/coding.prompt.yaml)<br>[core/development/policy.py](E:/PROJECT/GenesisAI/src/genesisai/core/development/policy.py) |
| M04 | **每次修改前必须read_files并记录哈希**：提示级要求指定工具和顺序 | 批量机械重构容易反复读取已有可靠前态 | P2／删除 | 删除修改前必须再调read_files的机械流程；要求可靠当前内容、前态哈希和冲突检测即可 | [core/prompt/templates/protocols/coding.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/coding.prompt.yaml) |
| M05 | **修改后一律运行测试**：coding提示一律要求run_commands测试，testing另说按风险选择 | 文档/配置/低风险修改可能不必要地跑全套；无测试项目可能无法交付 | P2／删除 | 删除所有修改一律跑测试；按验收和风险选择测试、构建、静态检查或文档核对，如实呈现结果 | [core/prompt/templates/protocols/coding.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/coding.prompt.yaml)<br>[core/prompt/templates/protocols/testing.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/testing.prompt.yaml) |
| M06 | **默认测试入口优先于明确项目入口**：testing提示优先不带command，让Runtime猜入口 | 多语言/多包项目可能跑错范围或重复大套件 | P2／删除 | 删除优先让Runtime猜测试命令；仓库规则、明确入口和用户要求优先，未知时再探测 | [core/prompt/templates/protocols/testing.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/testing.prompt.yaml) |
| M07 | **最小修改/最少工具的普遍目标**：coding目标‘最小修改’，local_files目标‘最少文件工具’，bug_fix强调最小补丁 | 大型重构可能被模型误解为不允许充分改造 | P2／删除 | 删除最小补丁/最少工具作为普遍硬目标；改造规模以用户目标和验收为准，避免无关改动 | [core/prompt/templates/protocols/coding.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/coding.prompt.yaml)<br>[core/prompt/templates/protocols/local_files.prompt.yaml](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/templates/protocols/local_files.prompt.yaml)<br>[core/extensions/skills/builtin/bug_fix/SKILL.md](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/skills/builtin/bug_fix/SKILL.md) |

## 实现问题另列：不能用“放松限制”代替修复

以下6个问题与上表部分策略有关联，不重复计入149项限制总数。

| 编号 | 问题与证据 | 处理方向／关联项 | 修改范围 |
| --- | --- | --- | --- |
| X01 | 工具参数截断被当成普通回答截断：loop先判断finish_reason=length，再进入_handle_length，只保留content；恢复tools=[]。session_9fd9f1a24dad4bb4中4次create_mcp均未形成可执行调用。 | 区分输出类型，保留原任务，重发完整合法调用或分块提交；C01、B03、B07、I01。不得执行半截JSON。 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)<br>[agent/recovery.py](E:/PROJECT/GenesisAI/src/genesisai/agent/recovery.py) |
| X02 | 续接依赖有限短语/关键词；“那就不保”等短句会重新路由，后面的“继续”继承不到原构建意图。 | 任务意图与阶段持久化；用户回复更新当前任务，明确换题才切换；D01、D02。 | [core/prompt/task_protocols.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/task_protocols.py) |
| X03 | _finish_answer主要依赖[[PARTIAL]]、stop_reason和验证标记；finish_reason_length被排除在强制partial条件外，能力构建缺少“目标注册是否发生”验收。可能口头说明未完成却记录completed。 | 按任务验收事实收敛；待注册、待用户验收、部分完成不得伪装completed。 | [agent/loop.py](E:/PROJECT/GenesisAI/src/genesisai/agent/loop.py)<br>[agent/completion.py](E:/PROJECT/GenesisAI/src/genesisai/agent/completion.py)<br>[core/capabilities/service.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/service.py) |
| X04 | URL_PATTERN会把URL后紧接的中文任务描述吃进去；register_user_urls只去尾部标点，导致登记错误URL，正确URL反被candidate_not_registered拦截。 | 正确提取用户URL，保留原始文本与标准化地址；同步评判G04。不能简单禁止URL里的所有非ASCII字符。 | [core/research.py](E:/PROJECT/GenesisAI/src/genesisai/core/research.py)<br>[core/prompt/task_protocols.py](E:/PROJECT/GenesisAI/src/genesisai/core/prompt/task_protocols.py) |
| X05 | MCP测试使用definition.timeout_seconds，但McpServerSpec不含timeout；注册后动态ToolSpec未传该值，实际回到默认30秒。测试与正式执行不一致。 | 超时配置贯穿能力激活、桥接、连接与调用；I06、E01。 | [core/capabilities/execution.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/execution.py)<br>[core/capabilities/activation.py](E:/PROJECT/GenesisAI/src/genesisai/core/capabilities/activation.py)<br>[core/extensions/mcp/bridge.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/mcp/bridge.py) |
| X06 | fetch_web_content schema允许500字符query，SearchQuery实际上只接受400字符/50词。模型遵守schema也会被实现拒绝。 | 一个约束来源生成/验证schema和执行参数；G11。PDF每行1000/实际120的类似不一致已列L06。 | [core/extensions/tools/kernel/fetch_web_content/spec.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/kernel/fetch_web_content/spec.py)<br>[core/extensions/tools/web/contracts.py](E:/PROJECT/GenesisAI/src/genesisai/core/extensions/tools/web/contracts.py) |

## 后续改动的验证要求

本轮是代码审计，未改运行逻辑，未运行产品测试或付费模型调用。后续逐项实施时，测试应验证真实效果：

- 模型生成长工具参数：尤其推理消耗输出预算、被截断后能继续完成而非只回短答。
- 长任务经历超时、限流、修复失败、跨消息续接、上下文压缩后仍保留目标与验收事实。
- 多于1000/5000文件的仓库、大于1MiB源码、超出文档页数上限后仍有可达的后续内容。
- 超过10分钟的后台测试可持续运行、可取消、能取到末尾日志，跨宿主恢复不重复产生副作用。
- 能力创建/修订/测试/试用/验收/注册全链路；测试与正式运行使用相同生效配置；未通过/未验收仍不可注册。
- 只读、禁网、密钥隔离、数据删除确认、外部变更冲突保护继续有效；正常授权不被旧提示词反向阻止。
- 验证完成标准必须由实际产物与执行结果决定，不能仅靠模型有没有输出[[PARTIAL]]。

用户已批准本文改造方向。各编号的实现与证据仍需逐项核销，不能把“已批准”当作“已完成”；当前结果见实施记录。
