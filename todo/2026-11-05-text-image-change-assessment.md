# 文字＋图片多模态：改动范围与验收

日期：2026-11-05。状态：已实施，文字＋图片链路完成离线回归与当前供应商真实验收。

用户已确定本期只接入文字和图片；支持用户附图、本地读图、MCP 图片、多轮追问及会话恢复。输出继续为文字和工具调用。视频、音频、视频抽帧本期不接入。

评估依据是当前工作树的入口、消息、工具执行、上下文、模型适配和持久化实现。之前对话提及的 `2026-11-05-multimodal-context-audit.md` 在当前工作树中不存在，本文件单独记录本轮评估，不恢复或覆盖此前文件。

以下保留实施前的范围评估，实际改动与验收结果见文末。

## 实施前规模与假设

- 推荐方案预计修改 **24 个现有实现/配置文件，新增 8 个实现/工具文件**。这是文件级估算，其中不少只是接线、展示或配置调整，不是 32 个文件全部重写。
- 另预计调整约 10～14 个已有测试文件，新增 3～4 个测试文件，并更新 2～3 份文档。最终数量取决于模型能力配置与工具图片协议的适配结果。
- 入口优先采用终端附件命令及本地路径；用户粘贴截图、拖放、远程图片 URL 自动下载属于额外入口能力，没有计入本次基线。
- 采用独立 `read_image` 工具；图片附件与工具结果复用同一图片服务。该选择避免在文本行号、offset 分页参数中混入图片尺寸语义。
- 会话与日志保存图片引用和元数据；文件解码、尺寸检查、缩放、快照及请求载荷分离处理。
- 初次评估时没有修改生产代码或执行真实模型请求；实施后的验证另行记录，不沿用此前测试结论。

## 预计修改的现有文件（24 个）

下表路径均相对仓库根目录。

| 文件 | 改动及原因 |
|---|---|
| `src/genesisai/shared/messages.py` | 定义文字块、图片块、图片引用和输入数据对象，提供统一文字提取、内容校验与序列化函数。当前 `Message.content` 只有 `str | None`，各层直接使用字符串方法。`Response.content` 保持文本输出语义。 |
| `src/genesisai/shared/results.py` | 定义工具返回图片引用的约定及转换辅助函数，使 Runner 无须针对每个图片工具编写分支。优先将图片引用放在 `data` 内，复用现有结果外层结构。 |
| `src/genesisai/shared/context_config.py` | 增加图片数量、像素、字节和请求预算相关限制及校验；当前只定义文本上下文容量。配置是应用限制，不冒充供应商真实规格。 |
| `src/genesisai/agent/loop.py` | 接受结构化用户输入，允许纯图片请求；任务分类和目标记录提取文字部分；工具结果同时保留文字及图片引用，替换无条件 `json.dumps(result)` 的模型交付方式。 |
| `src/genesisai/agent/recovery.py` | 接入图片不可用、模型不支持和媒体超限错误；压缩重试使用同一图片策略；摘要仅包含有效文字、图片引用及已产生的观察，不将 Base64 作为历史文字。 |
| `src/genesisai/model/base.py` | 声明模型图片输入能力和请求内容契约，供 Runner/上下文预算使用；不要求实现多模态输出。 |
| `src/genesisai/model/openai_compatible.py` | 将内部内容转换为目标模型图片请求格式，统一 chat 与 stream_chat 的处理；校验角色与图片组合；请求日志使用不含媒体字节的表示。当前仅原样复制 `content`。 |
| `src/genesisai/model/providers/catalog.py` | 在模型选择目录中提供图片能力信息，区分已确认支持、不支持与未知。不能只按供应商品牌判断。 |
| `src/genesisai/model/config.py` | 构建客户端时结合 provider、model、endpoint 绑定能力和预算信息；让自定义模型或端点有明确处理结果。继续沿用现有凭证解析。 |
| `src/genesisai/core/tools/tool_runtime.py` | 组装图片服务，供本地工具和 MCP 复用；校验 `data` 中的图片引用；将图片配置接入工具与上下文。 |
| `src/genesisai/core/extensions/mcp/bridge.py` | 保留并校验 MCP image 块，转换为受管理图片引用；当前只过滤提取 text。非本期支持的类型应给出明确限制，不能静默丢弃后声称完整处理。 |
| `src/genesisai/core/state/store.py` | 校验消息块和图片资产索引，持久化引用并支持恢复；图片快照缺失或变更时报告准确原因。当前只校验 `messages` 为 list。 |
| `src/genesisai/core/state/io_journal.py` | 在统一日志边界将图片载荷替换为引用、MIME、尺寸、哈希等信息；避免 user/model/provider/tool 多次记录完整 Base64。普通文本日志仍保持原有完整性。 |
| `src/genesisai/core/prompt/context_budgeter.py` | 分别预算文字、图片和请求字节；加载本轮实际需要的图片；修正字符统计；把压缩后的图片引用转换为可请求内容。 |
| `src/genesisai/core/prompt/compaction.py` | 保留用户文字约束，单独处理当前图片与历史图片引用；禁止把图像载荷当长字符串截断。历史图像可卸载出工作上下文，但资产与引用仍保留并能重新加载。 |
| `src/genesisai/app/cli.py` | 附件命令、提交、排队、取消、新会话及模型切换接线；发送前给出明确的图片校验结果。 |
| `src/genesisai/app/command_input.py` | 增加附件操作的命令补全和输入提示，保持已有终端输入行为。 |
| `src/genesisai/app/run_queue.py` | 队列项从单纯 text 扩展为输入对象和稳定图片引用；入队时固定附件集合，避免等待期间读取到后一次输入的附件。 |
| `src/genesisai/app/terminal_view.py` | 显示附件摘要、纯图片输入、带图历史、队列和预算信息；使用统一文字提取函数，避免输出原始内容列表/Base64。 |
| `src/genesisai/app/session_picker.py` | 标题生成支持内容块；纯图片消息可用附件名作标题，避免 `.strip()` 导致整个会话被跳过。 |
| `src/genesisai/app/onboarding.py` | 模型选择中展示图片支持状态；带图会话切换模型时给出准确提示。密钥输入与环境变量优先规则复用。 |
| `src/genesisai/core/prompt/templates/protocols/local_files.prompt.yaml` | 说明文字用文本读取、图片用读图工具，以及历史图片何时需要重新加载。仅修改与图片实际使用相关的指令。 |
| `config/model.yaml` | 提供图片限制配置示例与说明，不擅自切换用户当前提供商或模型。 |
| `pyproject.toml` | 声明图片解码和缩放依赖（拟采用 Pillow；具体版本在实施时确认）；沿用现有工具包自动发现及 package-data 模式。 |

## 建议新增的文件（8 个，名称为拟定）

| 文件 | 职责 |
|---|---|
| `src/genesisai/core/media/__init__.py` | 图片服务包入口。 |
| `src/genesisai/core/media/images.py` | 统一处理本地/MCP 图片：授权后读取、有界解码、真实格式与像素检查、方向校正、缩放、哈希、受管理快照、引用解析与请求准备。复用服务，避免在 CLI、工具和 MCP 分别实现。 |
| `src/genesisai/model/capabilities.py` | 维护端点/模型图片能力及供应商适配所需限制，供配置、UI 与请求层共享。未知能力应明确标记，具体供应商支持范围须实施时核验。 |
| `src/genesisai/app/attachments.py` | 管理本轮待发附件、命令参数及展示摘要，避免进一步扩大已经较长的 `cli.py`。实际文件处理委托 core 图片服务。 |
| `src/genesisai/core/extensions/tools/kernel/read_image/__init__.py` | 新工具包入口。 |
| `src/genesisai/core/extensions/tools/kernel/read_image/tool.yaml` | 名称、描述、分类和启用状态，让自动注册器发现工具。 |
| `src/genesisai/core/extensions/tools/kernel/read_image/spec.py` | 声明本地路径或历史图片引用参数、只读权限及参数边界。 |
| `src/genesisai/core/extensions/tools/kernel/read_image/implementation.py` | 调用统一图片服务，返回图片引用及来源信息；支持重新加载已从工作上下文卸载的历史图片。 |

图片 I/O 放在 core，数据结构放在 shared，供应商格式转换放在 model。agent 通过现有注入路径协调；model 不反向导入 core。受管理图片引用只能解析到指定资产，不应为恢复图片而放开工具读取整个程序状态目录的权限。

## 优先复用、暂不列为必改

- `core/tools/registry.py`、`loader.py`：已按目录扫描 `tool.yaml` 并按约定加载实现，新增 read_image 无需再维护手工注册清单。
- `core/tools/executor.py`、`shared/results.py` 中现有错误外层结构：Executor 已将实现返回值完整放进 `data`。若图片引用放在 `data` 内，Executor 不必重写，也不必给所有工具强加新的外层字段；上表对 results.py 的调整是共享内容约定/辅助函数。
- `core/tools/base.py`、`permissions.py`、`shared/security.py`：图片工具可复用 `permission='read'`、授权路径检查和现有工具上下文；图片专有校验在图片服务处理。若后续要求为图片读写增加新授权模式，才扩大这些文件的改动。
- `model/credentials.py`、`model/settings.py`：图片预算沿用已有 `context` 字典持久化，能力来源由客户端绑定时，可复用当前存储结构。只有增加显式用户能力覆盖配置时才需调整 settings。
- `model/providers/qwen.py`、`qwen_token_plan.py`、`deepseek.py`：先通过公共适配器与能力描述接入；核验发现供应商特有参数或协议需求时，再修改相应实现。不会为尚未确认支持的端点自动打开图片能力。
- `agent/ports.py`：如接线需要新图片端口，则补充 Protocol；如由现有上下文端口准备内容，则无需新增接口。
- `core/prompt/composer.py`、`task_protocols.py`、`shared/budgets.py`：继续接收从输入中提取的文字和可信状态，不应要求这些模块理解图像二进制。
- Office/PDF 工具：本期不包含自动渲染文档页面或 OCR 管线。用户已提供的页面截图按普通图片处理。

## 测试与文档

建议新增以下测试文件（名称拟定），覆盖真实行为而非只测 dataclass 构造：

- `tests/test_image_assets.py`：格式、尺寸、数量与总大小、损坏文件、授权边界、缩放、快照完整性和缺失图片。
- `tests/test_multimodal_messages.py`：文字/纯图/混合/多图内容、请求序列化、供应商能力拒绝、流式及非流式等价。
- `tests/test_multimodal_runtime.py`：用户附图、本地读图、MCP 图片进入模型请求；多轮、工具关联、压缩后重载、取消与恢复。
- 可将 CLI 附件交互单列 `tests/test_image_input.py`，或合并入现有交互测试。

已有测试重点调整/补充：

| 文件 | 验收目标 |
|---|---|
| `tests/test_context_pipeline.py` | 图片预算、压缩、重载、容量错误；保留已有用户约束和工具成对消息。 |
| `tests/test_storage_schema.py` | 内容块及图片索引的校验、持久化、恢复，不覆盖非法数据。 |
| `tests/test_extensions_seams.py` | MCP 文图混合、纯图片、多图片、错误和不支持内容类型。 |
| `tests/test_io_logging.py` | 图片引用可追踪且日志不重复记录媒体字节，文字日志不被截断。 |
| `tests/test_command_input.py` | 附件命令补全、纯图片提交入口。 |
| `tests/test_interactive_queue.py` | 排队时固定图片、登记失败保留任务、取消和恢复不串图。 |
| `tests/test_cli_ui.py` | 附件预览、超限提示、历史回放、预算展示。 |
| `tests/test_workspace_history.py` | 带图会话切换、纯图片标题、恢复到正确工作区。 |
| `tests/test_model_onboarding.py` | 模型能力显示及带图切换模型。 |
| `tests/test_tool_contracts.py` | read_image 参数/权限/结果约定，其他工具结果结构仍成立。 |
| `tests/test_streaming_terminal.py` | 图片输入后的文字流式回答和错误显示。 |

此外复跑架构层级、凭证优先及现有工具回归；不因为新增图片输入就机械修改所有测试文件。真实供应商验收需覆盖选定的图片模型与端点，mock 请求通过不能替代模型实际识图验证。

文档预计更新 `docs/用户指南.md`、`docs/问题及解决清单/004-上下文预算与压缩续跑.md`，以及涉及新模块职责的架构说明。本文件在实施后更新待办完成情况。

## 实施先后与主要不确定项

1. 先确定消息/图片引用契约、模型能力与角色映射，再实现图片存储和请求序列化。
2. 打通用户附图 → Runner → 模型 → 文本回答及保存恢复。
3. 将 read_image、MCP 图像结果接入相同链路，并验证工具配对及图片来源。
4. 完成压缩、历史图重载、队列、终端展示及日志控制，再做完整验收。

主要不确定项是工具返回的图片如何进入所选供应商的合法请求结构：不能假定每个兼容接口都允许 `role=tool` 原样携带图片块。需要验证后在 model 层适配，保留工具来源和调用关联，不提升图片中内容的指令优先级。这可能增加少量供应商代码与测试。

第二个关键点是历史图片策略。只固定保留全部用户图片会越积越多；只删图片块又会让追问失去依据。应保留稳定资产引用，对当前必需图片计入预算，并支持按引用重新读取历史图片。

遵循仓库约定：不额外添加旧接口兼容层或历史格式自动迁移；新格式会话的恢复是正式功能；不自动删除用户原图、已有会话或日志。

## 实施结果

- 修改 23 个现有实现/配置文件，新增上表 8 个实现/工具文件。模型能力在客户端构造时按实际提供商、模型和端点绑定，因此不需要修改原先预计的 `model/config.py`。
- 新增 4 个图片测试文件及 `tests/manual/live_image_smoke.py` 真实验收脚本；更新用户指南、上下文预算说明和五层架构说明。既有测试继续复用，没有机械调整其断言。
- 输入支持文字、纯图、文字＋多图；终端提供 `/attach`、`/attachments`、`/attachments clear`、`/send` 和单次执行的重复 `--image`。附件在选择时冻结像素，登记成功后消费；队列不会因原文件被修改而换图。
- 静态 PNG/JPEG/WebP 经校验、方向修正及等比缩放，保存为会话独立 PNG 快照；会话保存引用，日志记录载荷摘要。`read_image` 和 MCP 图片使用同一服务。原图、既有会话与日志均保留。
- chat 与 stream_chat 共用请求转换。工具消息保持合法的文字结果和调用配对，在整批结果后通过标明工具来源的图片观察消息传输像素；该消息不写成真实用户输入。
- 图片数量、字节、解码像素、最长边和上下文 Token 都有预算；较早图片可从工作上下文卸载并按 `image_ref` 重新加载。未知图片能力、损坏快照及不支持的媒体有明确错误。
- 本期没有接入视频、音频、动画、剪贴板、远程图片下载或文档自动渲染，也没有增加历史格式迁移及兼容层。ENV 优先、终端密钥回退和会话恢复继续沿用正式实现。

## 验证记录

1. 全量回归在排除下列 4 个缺失夹具用例后：**494 passed, 1 skipped, 4 deselected**，用时 101.83 秒。覆盖层级依赖、凭证优先、会话、交互队列、工具及本次图片功能。skip 为宿主未允许创建符号链接；相关 junction 测试仍执行。
2. 最后补强不可变输入契约后，重新执行 4 个图片测试文件：**21 passed**，用时 14.82 秒。与上项测试存在重叠，不相加计数。
3. 真实模型为当前配置的 `qwen_token_plan / qwen3.8-flash`。使用用户指定的 `E:\PROJECT\GenesisAI\src\genesisai\app\assets\pet\spritesheet.webp`，完成直接附图、从磁盘恢复会话后的流式追问、`read_image` 工具图片请求三条路径。模型识别出了黄色尖刺角色及动作帧网格。验收只检查链路及图像理解基本特征，不保证每项视觉描述都准确。
4. 原素材哈希未变；规范化快照为 1260×1536 PNG、1782536 字节；会话与日志未内嵌 Base64。真实验收报告保留在 `temp/live-image-5d7bcbe518ba4d84bb91d356ade2e82d/report.json`。
5. `git diff --check` 通过。工作树中另外出现的 `app/pet_window.py` 和 `tests/test_desktop_pet.py` 修改不属于本次多模态实现，保持原样。

全量回归首次暴露的命令补全顺序、主菜单默认项和取消模型设置时 `model=None` 的问题已修复，复跑通过。

## 完成项

- [x] 消息与图片引用契约、图片能力描述。
- [x] 统一图片服务、持久化、日志及预算。
- [x] 用户附件输入与完整会话链路。
- [x] read_image 与 MCP 图片结果。
- [x] 历史图片重载、压缩、队列与 UI。
- [x] 离线回归、选定供应商验收与文档。

## 既有测试基线待办

以下 4 个用例首次全量运行时因仓库缺少数据而失败；对应路径没有被 Git 跟踪，也没有在本次任务中删除或重建。后续全量回归仅排除这 4 个具体用例。

- [ ] `tests/test_integration.py::test_A03_source_baseline`：缺少 `docs/SOURCE_BASELINE.json`。
- [ ] `tests/test_product_simulation.py::test_dataset_is_strict_and_has_five_isolated_cases`：缺少 `tests/product_acceptance_dataset/case_01_code_fix/workspace` 等产品验收工作区。
- [ ] `tests/test_product_simulation.py::test_all_five_offline_product_simulations_pass`：同上，无法执行五组产品仿真。
- [ ] `tests/test_python_project_v1.py::test_python_development_simulation_passes`：缺少 `agent_tests_workspace`。
