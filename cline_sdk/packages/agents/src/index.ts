/**
 * @cline/agents
 *
 * 下一代 Cline SDK 的浏览器安全（browser-safe）Agent 运行时。
 *
 * 导出：
 *   - `AgentRuntime` / `Agent` —— Agent 循环类（同一类的两个名字）。
 *     提供提供商/模型 ID 时使用 `Agent`，提供预构建的 `AgentModel` 时
 *     使用 `AgentRuntime`。
 *   - `createAgentRuntime` / `createAgent` —— 等价的工厂函数。
 *   - `AgentRuntimeConfig` 及其两个变体（`AgentRuntimeConfigWithModel`、
 *     `AgentRuntimeConfigWithProvider`）—— 可辨识配置联合。
 *   - `AgentRunInput` / `AgentEventListener` —— 便捷类型别名。
 *   - `createTool` —— 从 `@cline/shared` 重新导出，用于编写工具。
 *
 * 共享类型（`AgentMessage`、`AgentRunResult` 等）应直接从
 * `@cline/shared` 导入。
 */

export type {
	AgentAfterToolResult,
	AgentBeforeModelResult,
	AgentBeforeToolResult,
	AgentMessage,
	AgentMessagePart,
	AgentModel,
	AgentModelFinishReason,
	AgentModelRequest,
	AgentRunResult,
	AgentRuntimeConfig as BaseAgentRuntimeConfig,
	AgentRuntimeEvent,
	AgentRuntimeHooks,
	AgentRuntimeStateSnapshot,
	AgentStopControl,
	AgentTool,
	AgentToolCallPart,
	AgentToolDefinition,
	AgentToolResult,
	AgentUsage,
	ToolApprovalResult,
	ToolPolicy,
} from "@cline/shared";
export { createTool } from "@cline/shared";
export type {
	AgentEventListener,
	AgentRunInput,
	AgentRuntimeConfig,
	AgentRuntimeConfigWithModel,
	AgentRuntimeConfigWithProvider,
} from "./agent-runtime";
export {
	Agent,
	AgentRuntime,
	AgentRuntimeAbortError,
	createAgent,
	createAgentRuntime,
} from "./agent-runtime";
