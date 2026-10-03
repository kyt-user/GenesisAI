# [experimental] @cline/llms

`@cline/llms` 是 Cline SDK 的模型与提供商层。它提供类型化的提供商设置、模型目录、共享网关契约，以及面向受支持 LLM 后端的、基于 AI SDK 的 handler 创建。

## 你将获得

- `@cline/llms/runtime`：用于声明式配置与运行时注册表创建
- `@cline/llms/providers`：用于 handler 创建与提供商设置/类型
- `@cline/llms/models`：用于模型目录与查询辅助工具
- `@cline/llms` 根导出：网关注册表与共享 LLM 契约

## 安装

```bash
npm install @cline/llms zod
```

## 快速开始

```ts
import { createHandler } from "@cline/llms";

const handler = createHandler({
	providerId: "anthropic",
	apiKey: process.env.ANTHROPIC_API_KEY ?? "",
	modelId: "claude-sonnet-4-6",
});

for await (const chunk of handler.createMessage("You are a concise assistant.", [
	{ role: "user", content: [{ type: "text", text: "Say hello." }] },
])) {
	console.log(chunk);
}
```

## 主要 API

### 运行时（Runtime）

当你需要一个围绕以下内容的小型注册表时，使用 `createLlmsRuntime(...)`：

- 已配置的提供商及其默认模型
- 通过 `getBuiltInProviders()` 进行内置提供商发现
- 通过 `registerBuiltinProvider(...)` 注册自定义提供商
- 为内置或自定义提供商创建 handler

推荐的导入方式：

```ts
import { createLlmsRuntime, defineLlmsConfig } from "@cline/llms/runtime";
```

### 提供商（Providers）

`@cline/llms/providers` 用于：

- `createHandler(...)` 与 `createHandlerAsync(...)`
- `ProviderSettings` 与 `ProviderSettingsSchema`
- `ProviderConfig`
- `Message` 与 `ApiStreamChunk`

内置提供商通过内部网关注册表路由，并由 AI SDK 提供商实现支撑。共享网关契约同时从 `@cline/llms` 与 `@cline/shared` 导出。

### 模型目录（Models）

当你需要用于选择 UI、默认值或校验的「生成的提供商/模型元数据」时，使用 `@cline/llms/models`。

关于生成的目录字段语义与 token 上限行为，见 [`src/catalog/README.md`](./src/catalog/README.md)。

支持音频的目录条目会保留其 models.dev 的 `modalities.input` 与 `modalities.output` 值。Node 客户端可以用与网关相同的提供商配置转写录制音频。OpenAI 兼容提供商使用 `/audio/transcriptions`；内置的 ElevenLabs 提供商使用其原生 `/speech-to-text` 端点。Vercel AI Gateway 使用其 AI SDK 原生的 `/v4/ai/transcription-model` 传输，而不是它的 OpenAI 兼容接口：

```ts
import { transcribeAudio } from "@cline/llms";

const result = await transcribeAudio({
  providerConfig,
  modelId: "whisper-large-v3",
  audio: recordedBytes,
});
```

音频必须是编码后的录音（如 WAV、MP3、M4A 或 WebM）；其格式从字节推断，而不是由调用方提供的 MIME 类型。网关与 OpenAI 兼容请求使用 AI SDK 转写，包括 `maxRetries`、取消与 `providerOptions`。其结果在文本、语言与时长之外，还会保留 `segments` 与 `warnings`。

转写在提供商边界上采用「默认失败（fail-closed）」。内置提供商在其 manifest 中声明具体传输；自定义提供商必须把 `routingProviderId` 设为一个「其转写传输被显式复用」的提供商。通用的 OpenAI 兼容聊天配置不隐含 `/audio/transcriptions` 的存在。

`operationModes` 包含 `streaming` 的转写模型使用实时 WebSocket，而不是录制音频调用。SDK 可以签发一个短时、限定于转写的浏览器凭据，而不暴露提供商 API key：

```ts
import { createStreamingAudioTranscriptionSession } from "@cline/llms";

const session = await createStreamingAudioTranscriptionSession({
  providerConfig,
  modelId: "openai/gpt-realtime-whisper",
});
```

原生 OpenAI、Vercel AI Gateway 与 ElevenLabs 支持流式转写。语音发现会包含外部目录缺失但 SDK 支持的实时模型，使用与请求校验相同的提供商能力声明。原生 OpenAI 使用 `gpt-realtime-whisper` 与「限定于转写的客户端 secret」；浏览器把该 token 提供给 AI SDK 的 OpenAI provider。Gateway token 默认 60 秒用于建立连接（最大 300 秒）；这不是已建立录制会话的时长限制。对 Gateway 会话，把 `session.token`、`session.baseUrl` 与 `session.modelId` 传给 AI SDK 的 `createGateway` 与 `experimental_streamTranscribe`。通过 `ReadableStream` 提供实时 PCM 块，消费 `fullStream` 以获得中间转写更新，并在停止时关闭音频流以获得最终文本。桌面端的 composer 使用这条路径。批处理模型继续使用 `transcribeAudio`。

## 入口点

- `@cline/llms`：以运行时为中心的便捷入口
- `@cline/llms/node`：显式的 Node/运行时入口
- `@cline/llms/browser`：浏览器安全的 bundle
- `@cline/llms/runtime`：聚焦运行时的入口
- `@cline/llms/models`：模型目录/查询入口
- `@cline/llms/providers`：提供商 handler/设置入口

## 相关包

- `@cline/agents`：Agent 循环与工具执行
- `@cline/core`：有状态运行时装配与提供商设置存储

## 更多示例

- 工作区总览：[README.md](https://github.com/cline/cline/blob/main/README.md)
- API 与架构参考：[DOC.md](https://github.com/cline/cline/blob/main/DOC.md)、[ARCHITECTURE.md](https://github.com/cline/cline/blob/main/ARCHITECTURE.md)

## 实时提供商冒烟测试（Live Provider Smoke Test）

用于对真实端点进行 API-key 驱动的提供商验证。

1. 确保环境中有提供商密钥（`ANTHROPIC_API_KEY`、`OPENAI_API_KEY`、`GEMINI_API_KEY`、`CLINE_API_KEY` 等）。
2. 使用示例配置 `packages/llms/src/tests/live-providers.example.json` 作为 providers 列表。
3. 运行：

```bash
LLMS_LIVE_TESTS=1 \
LLMS_LIVE_PROVIDERS_PATH=/absolute/path/to/packages/llms/src/tests/live-providers.example.json \
bun -F @cline/llms run test:live
```

聚焦推理的实时运行（相同命令，不同 flag）：

```bash
LLMS_LIVE_REASONING_TESTS=1 \
LLMS_LIVE_REASONING_PROVIDERS_PATH=/absolute/path/to/packages/llms/src/tests/live-providers.reasoning.example.json \
bun -F @cline/llms run test:live
```

聚焦工具调用的实时运行（相同命令，不同 flag）：

```bash
LLMS_LIVE_TOOL_TESTS=1 \
LLMS_LIVE_TOOL_PROVIDERS_PATH=/absolute/path/to/packages/llms/src/tests/live-providers.tools.example.json \
bun -F @cline/llms run test:live
```

可选配置：

- `LLMS_LIVE_PROVIDER_TIMEOUT_MS=120000` 增加每个提供商的超时。
- `LLMS_LIVE_PROVIDER_RETRIES=2` 对每个提供商的瞬时上游/提供商失败重试（总尝试次数 = 重试次数 + 1）。
- `LLMS_LIVE_PROVIDER_CONCURRENCY=3` 并行运行多个提供商条目。默认为 `3`；如需更严格的提供商限流行为可调低。
- 如需更窄的提供商集合，把 `LLMS_LIVE_PROVIDERS_PATH` 指向自定义文件。
- 把 `LLMS_LIVE_REASONING_PROVIDERS_PATH` 指向自定义文件以运行推理套件。
- 把 `LLMS_LIVE_TOOL_PROVIDERS_PATH` 指向自定义文件以运行工具调用套件。
- 当实时配置需要密钥但不想写入 JSON 时，可在提供商条目中使用 `apiKeyEnv`、`baseUrlEnv` 与 `headersEnv`。
- 为提交到仓库的回放测试录制提供商 cassette 时，设置 `CLINE_VCR=record` 与 `CLINE_VCR_INCLUDE_REQUEST_BODY=1`，使回放同时校验脱敏后的请求体契约。

OpenAI Codex 订阅的实时运行会在 `cline auth --provider openai-codex` 之后使用 `~/.cline/data/settings/providers.json` 中保存的 OAuth 凭据。把普通或推理套件指向 `packages/llms/src/tests/live-providers.openai-codex.example.json` 或 `packages/llms/src/tests/live-providers.openai-codex.reasoning.example.json`。

每个提供商的实时断言通过 JSON 中的 `expectations` 配置：

- `requireUsage`：若未发射 `usage` 块则失败（默认 `true`；设为 `false` 可退出）。
- `requireCacheReadTokens`：除非 `cacheReadTokens > 0` 否则失败（若未提供 prompt 覆盖，会自动用长缓存探测 prompt 至少运行 2 次尝试）。
- `minCacheReadTokens`：更严格的缓存下限检查。
- `requireReasoningChunk`：除非至少发射一个推理块，否则失败。
- `requireNoReasoningChunk`：若发射任何推理块则失败。
- `minInputTokens` / `minOutputTokens`：强制下限。
- `requireToolCall`：除非至少发射一个 `tool_calls` 块，否则失败。

在推理套件中，设置 `requireReasoningSignal: true` 要求「推理块或提供商上报的隐藏推理 token」二者之一（因提供商而异；在某些端点上可能不稳定）。
要检查「关闭推理确实在各模型上抑制了推理输出」，使用 `packages/llms/src/tests/live-providers.reasoning-disabled.example.json`；它覆盖 `cline`、`openai`、`openrouter`、`anthropic`、`gemini`、`vercel-ai-gateway`、`zai` 与 `deepseek` 中「模型支持」的直连与路由路径，配置 `reasoning.enabled: false` 并施加各提供商可用的最强「无推理」期望。

常见的实时测试失败类别：

- `Overloaded`：提供商/模型容量问题或上游瞬时饱和。
- `Insufficient Balance`：提供商账户需要充值。
- `Model Not Exist`：该模型 id 对当前提供商/账号不可用。
- `expected no reasoning chunks` 之类的断言失败：很可能是真实的 SDK/提供商选项行为回归。

### 向实时测试添加模型

在任一配置文件的 `providers` 对象下添加新条目：

- 缓存/冒烟套件：`packages/llms/src/tests/live-providers.example.json`
- 推理套件：`packages/llms/src/tests/live-providers.reasoning.example.json`
- 推理禁用套件（断言关闭推理时无推理块）：`packages/llms/src/tests/live-providers.reasoning-disabled.example.json`
- 工具调用套件：`packages/llms/src/tests/live-providers.tools.example.json`

最小冒烟/缓存条目：

```json
"my-openai-model": {
  "settings": {
    "provider": "openai",
    "model": "gpt-5.4"
  },
  "expectations": {
    "requireUsage": true
  }
}
```

带缓存断言的条目（自动强制多轮缓存探测）：

```json
"my-cache-model": {
  "settings": {
    "provider": "openai",
    "model": "gpt-5.4"
  },
  "expectations": {
    "requireUsage": true,
    "requireCacheReadTokens": true
  }
}
```

推理条目：

```json
"my-reasoning-model": {
  "settings": {
    "provider": "anthropic",
    "model": "claude-sonnet-4-6",
    "reasoning": {
      "effort": "high"
    }
  },
  "expectations": {
    "requireUsage": true,
    "requireReasoningChunk": true
  }
}
```

工具调用条目：

```json
"my-tools-model": {
  "settings": {
    "provider": "openai",
    "model": "gpt-5.4"
  },
  "expectations": {
    "requireUsage": true,
    "requireToolCall": true
  }
}
```
