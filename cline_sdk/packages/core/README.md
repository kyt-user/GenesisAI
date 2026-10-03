# [experimental] @cline/core

`@cline/core` 是 Cline SDK 的有状态编排层。它把 Agent 运行时、提供商设置、存储、默认工具与会话生命周期连接成一个「宿主就绪（host-ready）」的运行时。

## 你将获得

- 会话生命周期与编排原语
- 提供商设置与账号服务
- 默认运行时工具与 MCP 集成
- 基于存储的会话与团队状态辅助工具
- 通过 `@cline/core` 暴露的面向宿主的 Node 辅助工具

## 安装

```bash
npm install @cline/core
```

## 入口点

- `@cline/core`：核心契约、共享工具，以及用于构建宿主与运行时的 Node/服务端辅助工具

## 典型用法

大多数宿主应用都应从 `@cline/core` 开始。

```ts
import { ClineCore } from "@cline/core";

const cline = await ClineCore.create({});

const result = await cline.start({
	config: {
		providerId: "anthropic",
		modelId: "claude-sonnet-4-6",
		apiKey: process.env.ANTHROPIC_API_KEY ?? "",
		cwd: process.cwd(),
		mode: "act",
		enableTools: true,
		enableSpawnAgent: false,
		enableAgentTeams: false,
		systemPrompt: "You are a concise assistant.",
	},
	prompt: "Summarize this project.",
	interactive: false,
});

console.log(result.result?.text);
await cline.dispose();
```

当 `cwd` 与 `workspaceRoot` 都被省略时，执行宿主会把会话放到共享聊天工作区 `<cline-data-dir>/workspaces/chat`（默认 `~/.cline/data/workspaces/chat`），并预置一个 `AGENTS.md` 规则文件，告诉 Agent 把会话当作聊天处理，只有在用户要求时才创建命名项目文件夹。可从 `result.manifest.cwd` 与 `result.manifest.workspace_root` 读取解析后的路径。

## 会话引导（Session Bootstrap）

`ClineCore.create(...)` 还接受 `prepare(input)`。

当宿主需要在每个会话启动前准备「工作区作用域」的运行时状态，并通过显式的 `localRuntime` 引导字段应用 watcher/扩展/遥测输入、同时不希望扩大共享宿主契约时，使用它。

准备工作在「执行宿主解析被省略的工作区」之前运行，因此无路径启动的 `prepare(input)` 既不会得到 `cwd` 也不会得到 `workspaceRoot`。

## 主要 API

### 运行时与会话

使用 `@cline/core` 进行面向宿主的运行时装配：

- `ClineCore.create(...)`
- `createRuntimeHost(...)`
- `LocalRuntimeHost`
- `HubRuntimeHost` 与 `RemoteRuntimeHost`
- `DefaultRuntimeBuilder`

`ClineCore` 是面向应用的会话 API。更底层的 `RuntimeHost` 边界使用 `startSession`、`runTurn` 等「运行时原语」命名，以便传输适配器与 `start`、`send` 这类产品方法保持区分。诸如待发 prompt 编辑、累计用量查询、活动会话模型切换等服务型操作，会在所选传输支持时通过 `ClineCore` 暴露，而不属于最小宿主原语词汇表。

### 默认工具

`@cline/core` 负责内置宿主工具与执行器：

- `createBuiltinTools(...)`
- `createDefaultTools(...)`
- `createDefaultExecutors(...)`

### 存储与设置

该包还导出存储与设置辅助工具，例如：

- `ProviderSettingsManager`
- `CoreSettingsService` 与 `createCoreSettingsService`
- `setMcpServerDisabled` 等 MCP 设置辅助工具
- `SqliteTeamStore`
- 通过 `@cline/core` 提供的 SQLite 本地会话存储与产物

## 相关包

- `@cline/agents`：无状态 Agent 循环与工具原语
- `@cline/llms`：提供商/模型配置与 handler

## 更多示例

- 仓库示例：[examples](https://github.com/cline/sdk/tree/main/examples)、[apps/examples](https://github.com/cline/sdk/tree/main/apps/examples)
- 工作区总览：[README.md](https://github.com/cline/cline/blob/main/README.md)
- API 与架构参考：[DOC.md](https://github.com/cline/cline/blob/main/DOC.md)、[ARCHITECTURE.md](https://github.com/cline/cline/blob/main/ARCHITECTURE.md)
