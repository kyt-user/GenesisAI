# 上下文组装评估与厂商公开方案对照

日期：2026-11-05。范围：当前工作树静态审查、两个无模型调用的本地复现、官方公开资料核对。未实施生产代码修改。

## 判断

现有模块化 YAML、运行状态协调、结构化工具结果、完整会话与工作上下文分离、图片引用和日志构成了可用基础。优先改善消息语义与材料来源边界，再改善预算分配和缓存稳定性。不需要为了更换 YAML 格式或引入另一套 Agent 框架重写整个系统。

## 当前实现证据

- `src/genesisai/core/prompt/composer.py:64`：按身份、政策、状态、主协议、附加协议、工具说明、完成契约组装，再追加可信运行状态。YAML 经过结构校验并记录名称和哈希。
- `src/genesisai/core/prompt/context_budgeter.py:52`：继续追加工作区、失效快照、上一轮执行结果、AGENTS.md、手工加载 Skill 和活动项目文档。最后合成单条 system，再接工作历史。
- `src/genesisai/core/prompt/compaction.py:156`：用户消息全部固定保留并移至前部，再拼 assistant 摘要及近期助手/工具交互。
- `src/genesisai/core/tools/catalog.py:41`：普通执行阶段全量提供启用且依赖可用的工具。工具定义与消息分开传输，结束/恢复阶段禁用工具。
- Skill 有两个不同入口：CLI `/skills load` 更新 `active_skills`，下轮 system 自动注入；`core/extensions/tools/kernel/use_skill/implementation.py` 返回正文作为工具结果，不更新 `active_skills`。不能把二者都描述为常驻 system。
- 既有模型输入日志和 `/budget` 已有可观测性；需要增强的是逐块来源、选择原因、范围、预算和变化，而非从零新增日志。

## 复现

直接调用纯函数，不调用模型、不改会话或用户文件：

1. 输入 `user(A) → assistant(A) → user(改用B) → assistant(B)`，调用 `project(history, 2000, 0)`，输出角色为 `user → user → assistant摘要`。更大保留预算下还会在摘要后保留近期助手/工具单元。重排是确定行为；是否造成某一模型回答出错尚未做行为评测。
2. `TaskProtocolSelector().protocols_for('这个类比有什么问题')` 返回 `['coding']`，因为关键词列表包含单字“类”。这证明误选协议，不等于证明会实际执行不相关工具。

## 改进待办

- [ ] **P1：保留活动对话时间顺序。** 不把全部用户消息抽离集中到摘要前。摘要只覆盖已折叠区间，保留近期完整轮次；单独记录仍有效的约束及被撤销/替代的要求。验证“改用 B”“取消 A”“刚才第二个”等指代和纠正，以及工具调用/结果配对。
- [ ] **P1：区分应用指令、用户规则与参考资料。** AGENTS.md、手工 Skill、项目任务文档现在与应用政策合并为 system，依赖正文声明低优先级。内部使用带来源、用途、作用范围的结构化块，按供应商支持的角色和内容结构输出。不能仅改标题就视为完成隔离，也不能把 OpenAI 的 developer 角色强加给未确认支持的端点。实际权限仍由现有 Runtime/工具执行器执行。
- [ ] **P2：固定可复用前缀，延后动态状态。** 当前 lifecycle 模板位置较早，usage、stop_reason、故障状态等又位于规则/Skill/项目记忆之前；这些字段变化会缩短可复用前缀。将稳定政策与稳定工具定义保持一致，动态事实以明确状态消息或更新块交付。当前尚未测量 Qwen 端点缓存命中或费用变化，不宣称已确认具体收益。
- [ ] **P2：修正过宽关键词与协议叠加。** 对单字“类”、否定语句、普通讨论等建立路由反例。允许实际工具观察更新任务上下文，不让初始关键词判断长期决定所有协议。是否引入模型分类器应以误路由评测决定。
- [ ] **P2：逐块预算与有依据的取舍。** AGENTS、Skill、项目文档主要按字符截取，system 和工具定义作为整体开销，压缩集中作用于历史。增加每块预算、必要性和超限诊断；重要规则超限应提示，不静默截断成残缺要求。
- [ ] **P2：按路径加载项目规则，并统一 Skill 生命周期。** 当前只注入用户级和工作区根目录 AGENTS；需要明确子目录范围、加载/失效/恢复时机，避免无关规则扩散。明确手工常驻和工具按需两条 Skill 路径在压缩后的行为。
- [ ] **P2：补逐块检查和行为回归。** 基于现有日志增加组装清单：块来源、哈希、角色、顺序、加入原因、预算和丢弃原因。检查规则读取失败是否可见。用否定/纠正、规则冲突、长工具输出、纯图追问和恢复场景比较改造前后。
- [ ] **后续评估：大工具目录的延迟加载。** 小工具集全量提供是合理方案；先测当前工具 Schema 的 Token 占比、调用正确率与延迟。MCP 目录增长后再决定工具搜索或延迟加载，避免每轮随意改变工具列表而损害缓存或隐藏所需能力。

## 公开方案对照

这里只比较官方文档明确说明的机制，不推测 ChatGPT、Claude 或 Gemini 消费端未公开的完整内部提示词。

### OpenAI / Codex

官方 Cookbook 对 Codex 的说明强调稳定的指令、工具、沙箱和环境前缀；配置变化尽量通过追加消息表达。OpenAI API 文档区分 developer/user 等角色。Responses API 另有返回加密、不透明状态项的 compaction，应直接使用其返回上下文。这是该 API 的专有能力，当前 Qwen 兼容接口不能假定支持。

- [Prompt Caching 201](https://developers.openai.com/cookbook/examples/prompt_caching_201)
- [Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering)
- [Compaction](https://developers.openai.com/api/docs/guides/compaction)

对 GenesisAI 的启示：把稳定前缀与动态状态分开；按供应商适配上下文角色和压缩能力。前缀稳定与工具按需需要同时设计，不应简单认为“工具越少越好”。

### Anthropic / Claude Code

Claude Code 官方文档明确 CLAUDE.md 作为 system 后的用户消息交付；目录/路径规则按范围加载，Skill 描述先可见、正文按需加载。MCP 工具定义可延迟进入模型上下文。Claude API 的 `defer_loading` 仍要求请求包含完整定义，延迟的是模型上下文占用，并非 HTTP 请求中不再发送工具。压缩后不同内容有不同恢复策略，根规则、记忆、Skill、近期文件等分别处理。

- [Memory](https://code.claude.com/docs/en/memory)
- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Tool search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
- [Context window](https://code.claude.com/docs/en/context-window)
- [Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)

对 GenesisAI 的启示：来源和作用范围明确；按需获取已有雏形，但压缩后的恢复机制、规则范围和工具目录扩展能力还可完善。不能把自然语言优先级说明当作强制执行机制。

### Google / Gemini CLI

Gemini CLI 也会拼接上下文文件。官方文档区分全局、工作区和工具访问时发现的局部 GEMINI.md，支持文件导入和 `/memory show` 查看实际拼接内容。system.md 定义核心运行规则，GEMINI.md 承载项目指导。

- [GEMINI.md](https://geminicli.com/docs/cli/gemini-md/)
- [System prompt](https://geminicli.com/docs/cli/system-prompt/)

对 GenesisAI 的启示：字符串拼接本身正常，关键在规则发现范围、加载时机和实际输入的可检查性。

## 建议实施次序

第一步修正历史重排，建立明确的内容块及消息边界；第二步调整稳定/动态内容顺序并补逐块观测；第三步改善规则作用域、协议路由和预算。工具搜索以及供应商专用压缩在测量后决定，不作为第一轮改造前提。
