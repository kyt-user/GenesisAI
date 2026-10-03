# messages.json 契约 — v1

本文档描述由 `@cline/core` 写入的已持久化会话 messages 产物（artifact），路径为：

```
~/.cline/data/sessions/<sessionId>/<sessionId>.messages.json
```

它是规范的「重放/导出」产物。下游消费者（例如，ATIF 转换器）应能仅凭该文件重建完整的会话轨迹。同目录下的 `hooks.jsonl` 属于可观测性/调试遥测，不是重放或导出的必需项。

schema 由顶层 `version` 字段进行版本化。本文档描述 **version `1`**。

## 文件级结构

```jsonc
{
  "version": 1,
  "updated_at": "2026-04-22T17:42:10.123Z",
  "agent": "lead" | "subagent" | "teammate",
  "sessionId": "<session-id>",
  "taskType": "...",              // 可选；subagent/team 运行中出现
  "messages": [ /* 见下文 */ ],
  "system_prompt": "..."          // 可选
}
```

生产者：[`buildMessagesFilePayload`](../src/services/session-data.ts)。

## 消息结构

`messages[]` 中的每一项都是一条规范化的已存储消息：

```jsonc
{
  "id": "<stable-nanoid>",             // 始终存在；缺失时自动生成
  "role": "user" | "assistant",
  "content": [ /* 内容块，见下文 */ ],
  "ts": 1745343730123,                 // epoch 毫秒；出现在助手轮次消息上
  "modelInfo": {                       // 仅助手消息；见「助手轮次保证」
    "id": "claude-sonnet-4-6",
    "provider": "anthropic",
    "family": "claude-sonnet-4"        // 可选
  },
  "metrics": {                         // 该轮次的末尾助手消息；见下文
    "inputTokens": 21,
    "outputTokens": 8,
    "cacheReadTokens": 3,
    "cacheWriteTokens": 1,
    "cost": 0.13
  }
}
```

说明：

- `role` 是 `"user"` 或 `"assistant"`。静态存储中没有独立的 `"tool"` 角色——工具结果以 `tool_result` 块的形式内嵌在 `user` 消息上（Anthropic 原生形态）。
- `content` 始终是数组。不存在字符串形式的 content。

## 内容块结构（Anthropic 原生）

持久化的 content 使用提供商原生的块结构，而不是网关的 kebab-case 结构。合法的块 `type` 值：

| `type`        | 字段                                                  | 角色      |
|---------------|-------------------------------------------------------|-----------|
| `text`        | `text: string`                                        | any       |
| `thinking`    | `thinking: string`（推理文本）                          | assistant |
| `tool_use`    | `id: string`、`name: string`、`input: unknown`         | assistant |
| `tool_result` | `tool_use_id: string`、`content: unknown`、`is_error?: boolean` | user |

关联关系：`tool_result.tool_use_id` 与助手消息上先前的 `tool_use.id` 相匹配。ID 在会话内保持稳定。

工具结果上的错误信号：`is_error` 是唯一的规范字段。由提供商路径设置时会被规范化为布尔值（默认 `false`）。

## 助手轮次保证

对于每个 **已完成** 的助手轮次（即模型实际产生了输出的轮次），该轮次的末尾助手消息携带：

- `modelInfo.id`（必需）
- `modelInfo.provider`（必需）
- `metrics.inputTokens`（必需）
- `metrics.outputTokens`（必需）
- `metrics.cacheReadTokens`（必需；允许 `0`）
- `metrics.cacheWriteTokens`（必需；允许 `0`）
- `metrics.cost`（必需；允许 `0`）

若一个助手轮次在同一次运行中发出多条助手消息，只有该轮次的 **最后一条** 助手消息携带 `metrics`。同一轮次中较早的助手消息仍携带 `modelInfo`。

由 [`withLatestAssistantTurnMetadata`](../src/services/session-data.ts) 强制执行，并由 [`../src/runtime/host/local-runtime-host.e2e.test.ts`](../src/runtime/host/local-runtime-host.e2e.test.ts) 中的 LocalRuntimeHost e2e 契约测试覆盖。

## 失败与重试语义

1. **轮次成功。** 末尾助手消息具有如上完整的 `modelInfo` + `metrics`。

2. **轮次在任何助手输出出现之前失败。** 不会追加助手消息。持久化文件仍是有效快照，不会伪造用量/模型元数据。消费者不得假定每个已完成的会话都以助手消息结束。

3. **瞬时失败后重试成功**（例如，认证刷新 + 重试）。此前的助手消息保留其 `modelInfo` 与 `metrics`；重试的末尾助手消息携带新轮次的 `metrics`。先前的 metrics 不会被覆盖或丢弃。

4. **一个轮次中有多条助手消息。** 该轮次的最后一条助手消息携带该轮次的 `metrics`。同一轮次中较早的助手消息携带 `modelInfo` 但没有 `metrics`。

这些行为在 CLI、桌面端 sidecar、以及 subagent / team 任务会话路径上完全一致。

## 版本化

顶层 `version` 字段是一个数字。目前为 `1`。对上述结构的任何不向后兼容的变更都会递增该值。增量字段（新的可选顶层键、新的可选消息字段）可以在不提升版本的情况下出现，因此消费者应容忍未知键。

## 示例

v1 的黄金示例位于 [`../fixtures/messages/success.messages.json`](../fixtures/messages/success.messages.json)：单轮次，包含推理 + 工具调用 + 工具结果 + 最终文本。它作为下游转换器的复制粘贴参考提供。

权威保证来自真实的写入路径与 [`../src/runtime/host/local-runtime-host.e2e.test.ts`](../src/runtime/host/local-runtime-host.e2e.test.ts) 及 [`../src/runtime/host/local-runtime-host.test.ts`](../src/runtime/host/local-runtime-host.test.ts) 中的端到端测试；如果示例与写入器实际输出的内容发生偏离，这些测试是事实来源。