# 提供商请求捕获（Provider Request Capture）

本目录包含 SDK 提供商网关与 AI SDK 提供商适配器。`provider-request-capture.ts` 增加了一条「可选启用」的本地捕获路径，用于调试发送给模型提供商的精确 prompt/请求。

捕获层与提供商无关。它不导入 Weave、W&B、OTel 或插件代码。插件或外部工具可以在请求到达 `sdk/packages/llms` 之前，通过写入 `request.options.metadata` 来关联记录。

## 为什么需要它（Why This Exists）

插件的 hook 可以观察 Cline 的对话状态，但它们运行在 Core 的最终「提供商消息准备」之前。最后这遍处理可能会修复缺失的工具结果、截断工具输出、重写过期的文件内容、应用 prompt 缓存提供商选项，以及为提供商格式化消息。

对于 token 调查，请比较这些层：

```text
插件可见的消息
  captureStage = pre_build_for_api
        |
        v
Core MessageBuilder.buildForApi(...)
        |
        v
传给 streamText(...) 的 AI SDK prompt
  captureStage = ai_sdk_prompt
        |
        v
提供商客户端 fetch(...)
  captureStage = wire_request，仅当 CLINE_CAPTURE_WIRE=true
        |
        v
提供商收到请求
```

如果 token 增长只出现在 `wire_request` 中，问题出在提供商序列化。如果出现在 `ai_sdk_prompt` 但不在 `pre_build_for_api` 中，问题出在 Cline 的最终提供商格式化/构建步骤。如果已经出现在 `pre_build_for_api` 中，问题出在最终构建步骤的上游。

## 环境变量（Environment Variables）

| 变量 | 取值 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `CLINE_CAPTURE_PROVIDER_REQUEST` | `off`、`summary`、`full` | `off` | 启用提供商请求捕获。 |
| `CLINE_CAPTURE_WIRE` | `true`、`false` | `false` | 包装提供商的 `fetch` 以捕获字面请求体。 |
| `CLINE_CAPTURE_DIR` | 文件系统路径 | 未设置 | 捕获文件的显式输出目录。 |
| `CLINE_CAPTURE_CLEANUP` | `on`、`off` | `on` | 清理旧捕获文件。设为 `off` 以保留本地文件。 |
| `CLINE_CAPTURE_MAX_PREVIEW_BYTES` | 正整数 | `65536` | full 模式的载荷预览字节上限。 |
| `CLINE_DATA_DIR` | 文件系统路径 | 未设置 | 回退基础目录。捕获写入 `CLINE_DATA_DIR/provider-request-captures`。 |

若 `CLINE_CAPTURE_DIR` 与 `CLINE_DATA_DIR` 都未设置，捕获为空操作（no-op）。这可以防止 prompt 内容被意外写入仓库工作树。

## 输出（Output）

捕获会为每个被捕获的阶段写一个 JSON 文件：

```text
${CLINE_CAPTURE_DIR}/<captureId>.<captureStage>.<attempt>.provider-request.json
```

或者，当只设置 `CLINE_DATA_DIR` 时：

```text
${CLINE_DATA_DIR}/provider-request-captures/<captureId>.<captureStage>.<attempt>.provider-request.json
```

文件通过临时文件与同目录重命名进行原子写入，因此消费者应忽略 `*.tmp`。`captureId` 在存在时来自 `GatewayStreamRequest.metadata.captureId`；否则 SDK 从请求关联元数据派生一个稳定 ID。当同一阶段在同一次请求中被捕获多次（例如提供商重试）时，`attempt` 递增。

当 `CLINE_CAPTURE_CLEANUP` 打开时，SDK 会机会性地清理超过 24 小时的捕获文件。消费者也可以在处理完成后删除文件。当你需要保留本地捕获文件以供人工检查时，设置 `CLINE_CAPTURE_CLEANUP=off`。

每条记录包括：

- `timestamp`
- `captureStage`：`ai_sdk_prompt` 或 `wire_request`
- `attempt`
- `mode`：`summary` 或 `full`
- `correlation`：从 `GatewayStreamRequest.metadata` 复制，外加提供商与模型 ID
- `summary`：字节数、估算 token 数、哈希、角色计数、最大消息、推理/工具结果计数
- `payload`：仅在 `full` 模式中，截断到 `CLINE_CAPTURE_MAX_PREVIEW_BYTES`

wire 捕获有意只记录 URL、方法与 body。它不记录请求头，因此授权值不会被写入捕获文件。

## 示例（Example）

```bash
export CLINE_DATA_DIR="$(mktemp -d)"
export CLINE_CAPTURE_PROVIDER_REQUEST=summary
export CLINE_CAPTURE_WIRE=true

cline --provider openrouter --model openai/gpt-4o-mini "Say hello"

ls "$CLINE_DATA_DIR/provider-request-captures"
```

对于预期产生完整请求体的内部调查：

```bash
export CLINE_DATA_DIR="$(mktemp -d)"
export CLINE_CAPTURE_PROVIDER_REQUEST=full
export CLINE_CAPTURE_WIRE=true
export CLINE_CAPTURE_MAX_PREVIEW_BYTES=1000000
# 可选：在消费者处理完后保留文件。
# export CLINE_CAPTURE_CLEANUP=off
```

## 关联（Correlation）

捕获模块读取 `GatewayStreamRequest.metadata` 并把它复制到每条记录中。插件可以从 `beforeModel` hook 写入这些元数据：

```text
beforeModel hook
  returns options.metadata = { captureId, sessionId, runId, conversationId, iteration }
        |
        v
agents/core hook 组合对 metadata 做深度合并
        |
        v
GatewayStreamRequest.metadata
        |
        v
provider-request-capture.ts 按 captureId 写入各阶段文件
```

Weave 追踪插件使用这条路径把本地提供商捕获关联到匹配的模型 span，但 SDK 捕获文件即使没有 Weave 也很有用。

## 覆盖范围（Coverage）

此捕获路径目前已接入 `ai-sdk.ts` 中的 AI SDK 提供商适配器。绕过 `createAiSdkProvider(...)` 的提供商在获得等价埋点之前，不会发出 `ai_sdk_prompt` 或 `wire_request` 记录。