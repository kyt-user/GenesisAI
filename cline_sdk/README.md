<p align="center">
  <img src="https://github.com/user-attachments/assets/a05da977-2cb7-498a-88ca-20f24c9562e1" width="100%" />
</p>

<div align="center">
<table>
<tbody>
<td align="center">
<a href="https://discord.gg/cline" target="_blank"><strong>Discord</strong></a>
</td>
<td align="center">
<a href="https://www.reddit.com/r/cline/" target="_blank"><strong>r/cline</strong></a>
</td>
<td align="center">
<a href="https://github.com/cline/cline/discussions/categories/feature-requests?discussions_q=is%3Aopen+category%3A%22Feature+Requests%22+sort%3Atop" target="_blank"><strong>功能请求</strong></a>
</td>
</tbody>
</table>
</div>

Cline SDK 是一个用于构建 AI Agent 的 TypeScript 框架——这些 Agent 可以编辑文件、运行 shell 命令、浏览网页、调用 API，并使用你赋予它们的任何自定义工具。它就是驱动 [Cline](https://github.com/cline/cline) 的同一引擎，被打包成一个可嵌入你自有应用中的库。

```typescript
import { Agent } from "@cline/sdk"

const agent = new Agent({
  providerId: "cline",
  modelId: "openai/gpt-5.5",
  systemPrompt: "You are a helpful coding assistant.",
  tools: [],
})

const result = await agent.run("Create a REST API with Express and TypeScript")
console.log(result.text)
```

就这样。Agent 会流式输出它的响应，在你给了工具时调用工具，并在任务完成时返回。

## 安装

```bash
npm install @cline/sdk
```

## SDK Skill（技能）

如果你使用编码 Agent（Claude Code、Codex、Cline 等），可安装 [Cline SDK skill](https://github.com/cline/sdk-skill)，为你的 Agent 提供 SDK API 与最佳实践的上下文，帮助它用 Cline SDK 构建应用。

```bash
npx skills add cline/sdk-skill
```

你可以让它脚手架生成 Agent、创建自定义工具、接入插件、配置提供商等。

## 你可以构建什么

编码 Agent、Slack 机器人、定时自动化、代码评审流水线、多 Agent 团队、IDE 集成——任何受益于「能采取行动而不只是生成文本」的 LLM 的场景。

```typescript
// Slack 机器人：每个线程拥有自己的、带对话记忆的 Agent
const agents = new Map<string, Agent>()

async function handleMessage(threadId: string, message: string) {
  let agent = agents.get(threadId)
  if (!agent) {
    agent = new Agent({
      providerId: "gemini",
      modelId: "gemini-3.1-pro-preview",
      systemPrompt: "You are a concise Slack assistant.",
      tools: [],
    })
    agents.set(threadId, agent)
  }

  const result = agent.hasRun
    ? await agent.continue(message)
    : await agent.run(message)

  return result.text
}
```

浏览 [`examples/`](examples) 中的完整可运行示例，以及 [`apps/examples/`](apps/examples) 中的应用示例：

| 示例 | 描述 |
|---------|-------------|
| [Plugins](examples/plugins) | 带工作区感知上下文、生命周期 hook 与分支级安全策略的自定义工具 |
| [Subagent Orchestration](examples/plugins/agents-squad) | 用预设、技能与跨 Agent 交接来生成和管理后台 Agent |
| [Hooks](examples/hooks) | 基于文件与运行时的 hook，用于日志、评审门禁、上下文注入与生命周期自动化 |
| [Cron Automations](examples/cron) | 用于定时质量检查与 PR 工作流的周期性/事件驱动自动化 spec |
| [Desktop App](apps/examples/desktop-app) | Tauri 桌面外壳 + Bun sidecar 后端 + Next.js UI |
| [VS Code Extension App](apps/examples/vscode) | 通过 RPC 运行时运行 Cline 会话的 VS Code 扩展示例 |

## 自定义工具

工具是 Agent 与世界交互的方式。定义一个工具需要：名称、模型要读取的描述、输入的 JSON Schema，以及一个真正执行工作的函数：

```typescript
import { createTool } from "@cline/sdk"

const deploy = createTool({
  name: "deploy",
  description: "Deploy the app to staging or production.",
  inputSchema: {
    type: "object",
    properties: {
      environment: { type: "string", enum: ["staging", "production"] },
    },
    required: ["environment"],
  },
  execute: async (input) => {
    const result = await runDeployment(input.environment)
    return { url: result.url, status: "success" }
  },
})

const agent = new Agent({
  providerId: "moonshot",
  modelId: "kimi-k2.5",
  systemPrompt: "You are a deployment assistant.",
  tools: [deploy],
})
```

Agent 根据描述决定何时调用工具。它会看到结果，并将其纳入自己的响应。

## 流式事件

执行期间的每个事件都可以被实时观测：

```typescript
const agent = new Agent({
  providerId: "anthropic",
  modelId: "claude-opus-4-7",
  systemPrompt: "You are a helpful assistant.",
  tools: [myTool],
  onEvent: (event) => {
    switch (event.type) {
      case "content_update":
        if (event.contentType === "text") process.stdout.write(event.text)
        break
      case "content_start":
        if (event.contentType === "tool") console.log(`\n[${event.toolName}]`)
        break
      case "usage":
        console.log(`\ntokens: ${event.inputTokens} in, ${event.outputTokens} out`)
        break
    }
  },
})
```

## 插件

把可复用的能力打包为扩展（extension）。扩展可以注册工具、观察生命周期事件并修改 Agent 行为：

```typescript
const metrics: AgentPlugin = {
  name: "metrics",
  manifest: { capabilities: ["tools", "hooks"] },

  setup(api) {
    api.registerTool(myCustomTool)
  },

  hooks: {
    beforeRun() {
      console.time("agent")
    },

    beforeTool({ toolCall }) {
      console.log(`tool: ${toolCall.toolName}`)
    },

    afterRun({ result }) {
      console.timeEnd("agent")
      console.log(`${result.iterations} iterations, ${result.usage.outputTokens} tokens`)
    },
  },
}
```

## ClineCore：完整运行时

当你需要会话持久化、内置工具、配置发现与多进程支持时，使用 `ClineCore`：

```typescript
import { ClineCore } from "@cline/sdk"

const cline = await ClineCore.create({ clientName: "my-app" })

const session = await cline.start({
  prompt: "Set up CI with GitHub Actions",
  config: {
    providerId: "anthropic",
    modelId: "claude-sonnet-4-6",
    apiKey: process.env.ANTHROPIC_API_KEY,
    cwd: "/path/to/project",
    enableTools: true,
  },
})

console.log(session.result?.text)
```

如果 `cwd` 与 `workspaceRoot` 都省略，执行宿主会把会话放入共享聊天工作区
`<cline-data-dir>/workspaces/chat`（默认
`~/.cline/data/workspaces/chat`），并预置一个 `AGENTS.md` 规则文件，
告诉 Agent 把该会话当作聊天，只在用户明确要求时才创建命名项目文件夹。
`session.manifest` 中的路径是权威的、解析后的工作区路径。

`ClineCore` 为 Agent 提供内置工具（`bash`、`editor`、`read_files`、`apply_patch`、`search`、`fetch_web`），把会话持久化到 SQLite，从 `.cline/` 目录发现配置，并可选地连接 RPC sidecar，以支持定时 Agent 与跨进程会话管理。

### 可移植的 Agent 插件

`ClineCore` 与 Hub 背书的 SDK 客户端无需客户端加载器即可支持 [Agent Plugins v1](https://agent-plugins.org/specification)。执行宿主会自动发现 Hub 主机上 `~/.agents/plugins/*` 下的用户安装包目录。自动发现有意不扫描工作区的 `.agents/plugins` 目录，因此打开一个仓库不会隐式激活由仓库控制的 MCP 服务器。宿主可以通过 `agentPluginPaths` 显式选择加入额外的根目录；这些调用方提供的路径会相对于会话 `cwd` 解析，并同样受包边界校验约束。

每个包都从其根目录的 `plugin.json` 进行校验。`skills/` 下有效的直接子级 Agent Skills 通过 `skills` 工具以 `plugin-name:skill-name` 的形式暴露；根 `mcp.json` 中有效的服务器会被连接，而不会修改 `cline_mcp_settings.json`。无效的包、组件、技能与 MCP 条目会在其规范定义的窄边界处失败。

Agent Plugin 发现是只读的：加载设置时校验 manifest 并检查 skills 与 `mcp.json`，但不会启动 MCP 进程或创建插件数据目录。对于 stdio MCP 服务器，运行时会在启动服务器之前立即创建专用的持久化 `PLUGIN_DATA` 目录，符合 Agent Plugins MCP 契约的要求。

Hub 同样拥有 Agent Plugin 的启用状态。Hub 背书的客户端通过设置 API 读取同一份插件清单并在那里切换条目，而不是维护客户端本地状态。被禁用的插件以其通过校验的 manifest 名称持久化，在构建会话运行时不会贡献 skills 或 MCP 服务器。每次设置变更都会发布 `settings.changed`，使已订阅的客户端可以刷新其设置视图。

Agent Plugin 的贡献是会话运行时快照的一部分。客户端可以在切换后重建空闲会话（CLI 在其交互式设置视图中进行切换时就是这样做的），但已在运行的轮次保留它启动时的工具、技能与规则。其他已存在的会话在重建或重启时拾取新状态；新会话立即使用它。`~/.agents/plugins` 下的文件安装或移除会在下一次设置刷新或会话构建时被检测到，而不是由文件系统 watcher 推送。

你也可以显式提供包根目录。相对路径由 Hub 相对于会话 `cwd` 解析：

```typescript
const session = await cline.start({
  prompt: "Use the release plugin to prepare this repository",
  config: {
    providerId: "anthropic",
    modelId: "claude-sonnet-4-6",
    cwd: "/path/to/project",
    enableTools: true,
    agentPluginPaths: ["./vendor/release-plugin"],
  },
})
```

## 包

SDK 是一个分层栈。按需取用，多少皆可：

| 包 | 作用 |
|---------|-------------|
| `@cline/sdk` | 你需要的一切——安装这一个即可 |
| `@cline/core` | 会话、持久化、内置工具、配置发现、RPC |
| `@cline/agents` | 带工具执行与流式输出的无状态 Agent 循环 |
| `@cline/llms` | LLM 提供商网关（Anthropic、OpenAI、Google、Bedrock、Mistral 等） |
| `@cline/shared` | 类型、工具创建助手、hook 引擎 |

`@cline/sdk` 是 `@cline/core` 的别名，从所有包重新导出，因此一次安装即可获得完整 API。如果你想要最小依赖占用，也可以单独安装各个包。

## CLI

Cline CLI 让你在终端使用完整 SDK：

```bash
# 交互式 Agent
cline

# 单条 prompt
cline "Refactor the auth module to use JWT"

# 调度 Agent 每天运行
cline schedule create "PR summary" --cron "0 9 * * MON-FRI" --prompt "Summarize open PRs"

# 连接用 @BotFather 创建的 Telegram 机器人
cline connect telegram -k "$TELEGRAM_BOT_TOKEN"
# 然后在 Telegram 中向机器人发送 /help 或 /start
```

Telegram 连接器的特定行为见 [`apps/cli/src/connectors/adapters/telegram.md`](./apps/cli/src/connectors/adapters/telegram.md)。

## 提供商

开箱支持所有主流 LLM 提供商：

| 提供商 | 模型 |
|----------|--------|
| Anthropic | Claude Opus 4.7, Sonnet 4.6, Haiku 4.5 |
| OpenAI | GPT-5.5, GPT-5.3 Codex |
| Google | Gemini 3.1 Pro Preview, Gemini 3 Flash Preview |
| AWS Bedrock | Claude, Llama |
| Mistral | Mistral Large, Codestral |
| 任何 OpenAI 兼容 | vLLM, Together, Fireworks, Groq, etc. |

## 文档

完整文档见 [docs.cline.bot/sdk](https://docs.cline.bot/sdk/overview)：

- [快速开始](https://docs.cline.bot/sdk/quickstart) —— 5 分钟从零到运行 Agent
- [核心概念](https://docs.cline.bot/sdk/agents) —— Agent、会话、工具、事件、扩展、hook
- [指南](https://docs.cline.bot/sdk/guides/building-an-agent) —— 常见模式的端到端教程
- [架构](https://docs.cline.bot/sdk/architecture/overview) —— SDK 如何组织以及为什么
- [API 参考](https://docs.cline.bot/sdk/reference/cline-core) —— 每个方法、类型与配置项

## 贡献

要为项目做贡献，请先阅读我们的[贡献指南](CONTRIBUTING.md)了解基础。你也可以加入我们的 [Discord](https://discord.gg/cline)，在 `#contributors` 频道与其他贡献者交流。如果你在寻找全职工作，请查看我们[招聘页面](https://cline.bot/join-us)上的开放职位！

## 许可证

[Apache 2.0 © 2026 Cline Bot Inc.](./LICENSE)
