# 模型目录语义（Model Catalog Semantics）

生成的目录是 SDK 对提供商与模型元数据的规范化副本。大多数内置目录数据来自 [models.dev](https://models.dev)，经由 `catalog-live.ts` 获取，并由模型生成脚本写入 `catalog.generated.ts`。

本文档说明 token 上限字段的预期含义，以及目录元数据与运行时请求策略之间的边界。

## 各模型的 API 协议（Per-model API Protocols）

models.dev 的模型级 `provider.npm` 被保留为 `metadata.apiProtocol`，用于 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 与 Gemini。当提供商在共享 base URL 下提供这些协议时，可通过 `metadata.routing.modelApiProtocol` 选择启用这些模型路由。选定的适配器还负责请求选项、序列化与流解析。原生提供商与本地 CLI 提供商保留各自的传输实现。

OpenCode Go 选择启用，并从请求元数据的 `sessionId` 发送 `x-opencode-session`，外加一个 Cline User-Agent。直接调用网关的调用方应为每个会话提供稳定的 `sessionId`；ClineCore 会提供其会话身份。Go 中未声明上游适配器的 Qwen 条目使用 Anthropic Messages，与 [Go 的端点文档](https://opencode.ai/docs/go/#endpoints) 一致。显式的上游声明优先于这个窄范围回退。

## 音频模态（Audio Modalities）

支持音频的条目会保留 models.dev 的方向性模态元数据：

```text
modalities.input   模型接受的内容
modalities.output  模型产生的内容
```

这两个方向不可互换。麦克风转写模型接受 `audio` 并产生 `text`；文本转语音模型接受 `text` 并产生 `audio`。即使音频模型不支持工具调用也会被保留，以便非聊天界面能够发现它们。聊天模型选择器必须排除不接受、也不产生文本的实用型模型。

## 来源字段（Source Fields）

`models.dev` 在 `limit` 下暴露模型限制：

```text
limit.context  为模型上报的最大上下文预算
limit.input    可选、更严格的 prompt/输入 token 预算
limit.output   为生成上报的最大输出 token 数
```

并非每个模型都有 `limit.input`。存在时应将其视为可用的最佳 prompt/输入上限。不存在时，目录会回退到以 `limit.context` 作为 prompt/输入上限。

## Cline 字段（Cline Fields）

目录将这些来源字段映射到 `ModelInfo`：

```text
contextWindow   提供商上报的上下文预算
maxInputTokens  压缩（compaction）与诊断使用的 prompt/输入 token 预算
maxTokens       提供商上报的输出 token 预算
```

这些字段不是可加的保证。特别地，以下目录元数据是合法的：

```text
contextWindow:  200000
maxInputTokens: 200000
maxTokens:      128000
```

这意味着：

```text
prompt 可以接近 200000 token
模型最多可以生成 128000 token
单个请求仍需让「prompt + 输出」符合提供商规则
```

不要推断 `maxInputTokens + maxTokens` 必须小于或等于 `contextWindow`。许多提供商目录暴露的独立最大值共享同一个底层上下文预算。

## 目录数据 vs 请求限制（Catalog Data vs Request Limits）

目录应当描述上报的模型能力。它应避免固化 Cline 请求默认值、产品级安全限制或提供商特定的变通方案，除非没有更好的地方来表达一个稳定事实。

发送模型请求的代码负责决定该具体轮次要请求多少输出 token。该决定可以取决于：

- 当前请求的实际 prompt 大小
- 提供商的上下文窗口行为
- 产品级默认输出上限
- 用户覆盖，如 `request.options.maxTokens`
- tokenizer 漂移与隐藏的提供商开销
- 推理、工具、图像与格式 token

对于「prompt 与输出 token 都必须装进同一上下文窗口」的模型，请求限制应基于当前 prompt，而不是在生成目录时臆造。概念上：

```text
safeOutputTokens = min(
	modelReportedMaxOutput,
	contextWindow - estimatedPromptTokens - reserveTokens,
	userConfiguredOutputCap or productDefaultOutputCap,
)
```

SDK 网关会为提供商请求解析一个输出 token 限制。存在时会使用 `request.options.maxTokens` 或等价宿主配置，否则当模型目录具有输出限制或上下文窗口时，应用产品默认输出上限（`DEFAULT_GATEWAY_MAX_OUTPUT_TOKENS`，当前为 32000）。提供商模块负责仅把该限制转发给支持它的线上 API 表面。

确切的请求限制策略属于 provider/gateway/core 的请求路径，而不属于生成的目录数据。

## 不要臆造输出限制（Do Not Invent Output Limits）

目录生成器不应把模糊的提供商元数据变成新的 Cline 输出限制。

例如，如果提供商上报：

```text
limit.context = 202800
limit.output  = 202800
```

生成的目录应保留那个上报的输出限制：

```text
contextWindow:  202800
maxInputTokens: 202800
maxTokens:      202800
```

在具体请求中，Cline 仍可能要求更少的输出 token。这属于请求路径的职责，因为只有请求路径知道当前 prompt 大小与用户配置的输出上限。

## 生成流程（Generation Flow）

生成的目录由实时规范化器产出：

```text
models.dev/api.json
	|
	v
src/catalog/catalog-live.ts
	|
	v
scripts/generate-models.ts
	|
	+--> src/catalog/catalog.generated.ts
	|
	+--> src/providers/providers.generated.ts
	|
	+--> src/providers/provider-ids.generated.ts
```

在仓库根目录运行以下命令重新生成并格式化目录：

```bash
bun run build:models
```

目录变更通常应在 `catalog-live.test.ts` 中加入测试，记录来源 `limit` 字段如何映射到 `ModelInfo`。

## 提供商差异（Provider Differences）

提供商的 token 语义并不统一：

- 一些提供商发布独立的输入与输出上限。
- 一些提供商只发布上下文预算与最大生成参数。
- 一些路由提供商上报的限制与上游模型文档不同。
- 一些提供商会拒绝 `prompt + requestedOutput > contextWindow` 的请求。
- 另一些提供商则改为截断、压缩或停止生成。
- 推理 token 可能计入输出预算。
- 工具 schema、工具调用、图像与提供商格式化可能消耗隐藏的输入或输出预算。

由于这些差异，目录生成应尽可能保留来源元数据，而运行时请求策略则应保守且可观测。

## 相关文件（Related Files）

- `catalog-live.ts`：规范化实时的 `models.dev` 数据。
- `catalog-live.test.ts`：测试目录规范化行为。
- `catalog.generated.ts`：已入库的生成提供商/模型目录。
- `../providers/providers.generated.ts`：已入库的生成提供商规格（specs）。
- `../providers/provider-ids.generated.ts`：已入库的生成提供商 ID。
- `../../scripts/generate-models.ts`：写出生成的目录输出。
- `../providers/ai-sdk.ts`：有条件地把 `maxOutputTokens` 传给 AI SDK。
- `../providers/gateway.ts`：解析按请求/默认的 `maxTokens`。

### 离线 Cline 精选列表（Offline Cline featured lists）

`bun run build:models` 还会把上游的 recommended、free 与 Cline Pass 列表捕获到 `cline-recommended.generated.ts` 中。当实时 feed 不可用时，SDK 使用此快照，保留 feed 顺序、标签与描述，并针对生成的模型目录解析名称。通过运行生成器来更新这些列表；不要在 core 中维护单独的模型 ID。生成过程要求两个上游来源都成功，这样一次宕机就不会用部分数据替换捆绑目录。

所有上游抓取、规范化与输出渲染都在任何文件写入之前完成。未变更的文件会被跳过；生成日志会区分已更新文件与未更新文件。生成器直接写入 git 检出目录，若写入失败可以在那里检查并回滚变更。