# [experimental] @cline/agents

`@cline/agents` 是 Cline SDK 中与运行时无关（runtime-agnostic）的 Agent 循环包。它为你提供构建「使用工具的 LLM Agent」所需的核心原语，同时不引入会话存储、Hub 传输或宿主特定的默认工具。

## 你将获得

- `Agent` / `AgentRuntime`——同一个类的两个名字——用于运行与继续「使用工具的 Agent 对话」
- `createAgent` / `createAgentRuntime`——工厂函数等价物
- `AgentRuntimeHooks` 用于生命周期拦截（`beforeRun`、`afterRun`、`beforeModel`、`afterModel`、`beforeTool`、`afterTool`、`onEvent`）
- 通过 `agent.subscribe(listener)` 与 `hooks.onEvent` 回调进行事件流式订阅
- 插件 setup 回调，用于在启动时贡献工具与 hook

## 本包不包含什么

`@cline/agents` 本身不附带完整的应用运行时。

- 文件系统访问、Shell 执行或网页抓取等默认宿主工具位于 `@cline/core`
- 会话持久化与有状态编排位于 `@cline/core`
- 共享 Hub 运行时/会话传输位于 `@cline/core`（见 `@cline/core/hub`）
- 子 Agent 与团队协作原语位于 `@cline/core`

这种拆分使本包可以用于 Node、浏览器与自定义宿主环境——在那里你可能想自带工具与运行时策略。

## 安装

```bash
npm install @cline/agents @cline/shared @cline/llms
```

## 快速开始

```ts
import { Agent } from "@cline/agents";
import type { AgentTool } from "@cline/shared";

const getWeather: AgentTool<{ city: string }, { forecast: string }> = {
	name: "get_weather",
	description: "Return the current weather for a city.",
	inputSchema: {
		type: "object",
		properties: { city: { type: "string" } },
		required: ["city"],
	},
	async execute({ city }) {
		return { forecast: `sunny in ${city}` };
	},
};

const agent = new Agent({
	providerId: "anthropic",
	modelId: "claude-sonnet-4-6",
	apiKey: process.env.ANTHROPIC_API_KEY,
	systemPrompt: "You are a concise assistant.",
	tools: [getWeather],
});

const result = await agent.run("What's the weather in San Francisco?");
console.log(result.outputText);
```

## 两种配置方式

`Agent` / `AgentRuntime` 接受两种配置形态：

**Provider 形态（Provider form）**——友好的入口。运行时会通过 `@cline/llms` 自动为你构建 `AgentModel`：

```ts
new Agent({
	providerId: "openai",
	modelId: "gpt-5",
	apiKey: process.env.OPENAI_API_KEY,
	// 也支持 baseUrl、headers
	tools: [/* ... */],
});
```

**Model 形态（Model form）**——高级用法。直接提供预构建的 `AgentModel`。当宿主已经自行持有网关（gateway）构建逻辑时很有用（`@cline/core` 内部就是这样用的）：

```ts
import { createGateway } from "@cline/llms";

const gateway = createGateway({ providerConfigs: [/* ... */] });
const model = gateway.createAgentModel({ providerId, modelId });

new Agent({
	model,
	tools: [/* ... */],
});
```

## 核心概念

### 工具（Tools）

工具遵循来自 `@cline/shared` 的 `AgentTool<TInput, TOutput>` 接口。每个工具有一个 JSON Schema 的 `inputSchema`，以及一个直接返回工具输出的 `execute(input, context)` 函数：

```ts
import type { AgentTool } from "@cline/shared";

const summarize: AgentTool<{ text: string }, { summary: string }> = {
	name: "summarize_text",
	description: "Summarize text into a short preview.",
	inputSchema: {
		type: "object",
		properties: { text: { type: "string" } },
		required: ["text"],
	},
	async execute({ text }, context) {
		// context.signal — 当运行被取消时中止
		// context.emitUpdate(...) — 以 `tool-updated` 事件流式发送进度
		return { summary: text.slice(0, 120) };
	},
};
```

运行时会用内部的工具结果消息包裹成功的工具输出。从 `execute(...)` 中抛出异常即报告工具失败，或使用 `afterTool` hook 转换内部的 `AgentToolResult` 信封。

### 事件（Events）

用两种方式之一订阅 `AgentRuntimeEvent` 流：

```ts
// 1. 构造后附加监听器。返回取消订阅函数。
const unsubscribe = agent.subscribe((event) => {
	if (event.type === "assistant-text-delta") {
		process.stdout.write(event.text);
	}
});

// 2. 构造时注册 `onEvent` hook。
new Agent({
	providerId,
	modelId,
	apiKey,
	hooks: {
		onEvent(event) {
			// 每个运行时事件都会触发
		},
	},
});
```

`AgentRuntimeEvent` 覆盖 run/turn 边界、助手文本与推理增量、工具生命周期、用量更新，以及运行完成/失败。完整联合类型见 `@cline/shared` 中的 `AgentRuntimeEvent`。

### 对话控制（Conversation Control）

- `agent.run(input)`——开始一次运行。`input` 可以是字符串、`AgentMessage` 或消息数组。也接受 `undefined`，表示不新增用户轮次、直接继续。
- `agent.continue(input?)`——`run(input?)` 的便捷别名。
- `agent.abort(reason?)`——取消活动运行。`.run()` 会以 `status: "aborted"` 结束（resolve）。
- `agent.snapshot()`——当前 `AgentRuntimeStateSnapshot` 的不可变视图（消息、用量、迭代次数、状态等）。
- `agent.restore(messages)`——用持久化的消息数组替换对话。会重置 run/turn 状态，但保留订阅者、工具、hooks、插件与模型。
- 构造函数中的 `initialMessages` 会在启动时播种对话。

### 钩子（Hooks）

传入一个 `hooks` 包（`AgentRuntimeHooks`）来观察或影响循环。所有 hook 都可以是异步的；任何返回 `{ stop: true, reason }` 的 hook 都会以 `aborted` 状态停止运行。

`afterModel` 还会收到一个可选的 `requestId`：当模型适配器暴露它时，这是对外响应的 HTTP `X-Request-ID`。它不是提供商的生成 ID，也不是重试 ID 列表。缺失时保持 undefined；早期失败/取消可能跳过 `afterModel`。Hook 会被 await，因此请把观测性工作另行调度（并捕获其错误），避免拖慢推理。

```ts
new Agent({
	providerId,
	modelId,
	apiKey,
	tools: [/* ... */],
	hooks: {
		beforeModel({ request }) {
			// 在模型调用前修改 messages/tools/options
			return { options: { temperature: 0.2 } };
		},
		beforeTool({ tool, input }) {
			// 基于策略阻止一次工具调用
			if (tool.name === "get_weather" && !(input as { city?: string }).city) {
				return { skip: true, reason: "city required" };
			}
			return undefined;
		},
		afterRun({ result }) {
			console.log("done", result.usage);
		},
	},
});
```

若需要更丰富的宿主侧 hook 编排（15 阶段 `HookEngine`、子进程支撑的 hooks、MCP 扩展），请使用 `@cline/core`。

### 用 `prepareTurn` 准备请求

`prepareTurn` 在消息发送给提供商之前运行。它可以为下一次请求重写消息或系统提示词：

```text
已保存的 transcript
        |
        | 轮次准备
        v
prepareTurn
        |
        v
准备好的提供商请求
```

返回的消息只影响当前模型调用的提供商请求。它们不会替换已保存的历史，也不会出现在 `AgentRunResult.messages` 中。

```text
prepareTurn 返回准备好的消息
        |
        +--> 提供商请求：是
        +--> 已保存的 transcript：否
        +--> AgentRunResult.messages：否
```

这与「修改已保存历史」有意不同。需要持久化脱敏、规范化或策略过滤的宿主，必须在消息进入 transcript 之前应用这些修改。

### 插件（Plugins）

插件可以在 setup 时贡献工具与 hook：

```ts
import type { AgentRuntimePlugin } from "@cline/shared";

const loggingPlugin: AgentRuntimePlugin = {
	name: "logging",
	setup({ agentId }) {
		return {
			hooks: {
				afterTool({ tool, result }) {
					console.log(agentId, tool.name, result.isError);
					return undefined; // hook 可以返回 AgentAfterToolResult
				},
			},
		};
	},
};

new Agent({
	providerId,
	modelId,
	apiKey,
	plugins: [loggingPlugin],
});
```

### 团队与 Spawn

多 Agent 工作流请使用 `@cline/core`：

```ts
import {
	createSpawnAgentTool,
	AgentTeamsRuntime,
	createAgentTeamsTools,
	bootstrapAgentTeams,
} from "@cline/core";
```

这些辅助工具为委派运行、邮箱（mailboxes）、任务管理与结果收敛提供协作原语。

## 入口点

- `@cline/agents`——唯一的包入口。当打包器解析 `browser` 条件时，`package.json` 的 `exports` 映射会自动提供浏览器安全的 bundle。

## 相关包

- `@cline/shared`：共享类型（`AgentTool`、`AgentMessage`、`AgentRuntimeEvent`、`AgentRuntimeHooks` 等）
- `@cline/llms`：提供商设置、模型目录，以及 gateway/handler 创建
- `@cline/core`：有状态运行时装配、存储、默认工具、子进程 hooks、Hub 传输与 MCP 集成

## 更多示例

- 仓库示例：
  [examples/plugins](https://github.com/cline/sdk/tree/main/examples/plugins)、
  [examples/hooks](https://github.com/cline/sdk/tree/main/examples/hooks)、
  [examples/cron](https://github.com/cline/sdk/tree/main/examples/cron)
- 工作区总览：[README.md](https://github.com/cline/sdk/blob/main/README.md)
- API 与架构参考：
  [DOC.md](https://github.com/cline/sdk/blob/main/DOC.md)、
  [ARCHITECTURE.md](https://github.com/cline/sdk/blob/main/ARCHITECTURE.md)
