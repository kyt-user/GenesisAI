/**
 * AgentRuntime 契约类型（从 clinee `@cline/shared` 移植）。
 *
 * 这些是 `AgentRuntime` 消费的规范类型定义。
 */

import type { GeneratedMedia } from "./llms/media";
import type { ModelInfo } from "./llms/model-info";
import type {
	ToolApprovalRequest,
	ToolApprovalResult,
	ToolPolicy,
} from "./llms/tools";
import type { BasicLogger } from "./logging/logger";
import type { ITelemetryService } from "./services/telemetry";

// =============================================================================
// AgentRuntime 使用的轻量级遥测接口
// =============================================================================

// =============================================================================
// 消息部件
// =============================================================================

export interface AgentTextPart {
	type: "text";
	text: string;
}

export interface AgentReasoningPart {
	type: "reasoning";
	text: string;
	redacted?: boolean;
	metadata?: unknown;
}

export interface AgentImagePart {
	type: "image";
	image: string | Uint8Array | ArrayBuffer | URL;
	mediaType?: string;
}

export interface AgentFilePart {
	type: "file";
	path: string;
	content: string;
}

export interface AgentMediaPart {
	type: "media";
	media: GeneratedMedia;
}

export interface AgentToolCallPart {
	type: "tool-call";
	toolCallId: string;
	toolName: string;
	input: unknown;
	metadata?: unknown;
	/** 普通 AgentRuntime 执行的工具不存在此字段。 */
	execution?: ModelToolExecution;
}

export interface AgentToolResultPart {
	type: "tool-result";
	toolCallId: string;
	toolName: string;
	output: unknown;
	isError?: boolean;
	/** 普通 AgentRuntime 执行的工具不存在此字段。 */
	execution?: ModelToolExecution;
}

export type ModelToolExecution = "client" | "provider";

/** 对在 AgentRuntime 外部执行的模型工具的观察记录。 */
export interface AgentModelToolActivity {
	toolCallId: string;
	toolName: string;
	execution: ModelToolExecution;
	input?: unknown;
	output?: unknown;
	isError?: boolean;
}

export type AgentMessagePart =
	| AgentTextPart
	| AgentReasoningPart
	| AgentImagePart
	| AgentFilePart
	| AgentMediaPart
	| AgentToolCallPart
	| AgentToolResultPart;

// =============================================================================
// 消息与 token 用量
// =============================================================================

export type AgentMessageRole = "user" | "assistant" | "tool";

export interface AgentTokenUsage {
	inputTokens: number;
	outputTokens: number;
	cacheReadTokens: number;
	cacheWriteTokens: number;
	/** 提供商报告的隐藏推理 token 数（可用时）。 */
	reasoningTokenCount?: number;
}

/**
 * 新运行时的规范 `AgentUsage` 形状。
 *
 * 这取代了旧版 `AgentUsage`（现为 `./agents/types` 中的 `LegacyAgentUsage`）。
 * 旧的面向宿主形状通过门面保留给 `AgentResult`/`AgentUsageEvent` 消费者。
 */
export interface AgentUsage extends AgentTokenUsage {
	totalCost?: number;
}

export interface AgentMessage {
	id: string;
	role: AgentMessageRole;
	content: AgentMessagePart[];
	createdAt: number;
	metadata?: Record<string, unknown>;
	modelInfo?: {
		id: string;
		provider: string;
		family?: string;
	};
	metrics?: AgentTokenUsage & {
		cost?: number;
	};
}

// =============================================================================
// 运行时状态
// =============================================================================

export type AgentRole = string;

export type AgentRunStatus =
	| "idle"
	| "running"
	| "completed"
	| "aborted"
	| "failed";

export interface AgentRuntimeStateSnapshot {
	agentId: string;
	agentRole?: AgentRole;
	parentAgentId?: string | null;
	conversationId?: string;
	runId?: string;
	status: AgentRunStatus;
	iteration: number;
	messages: readonly AgentMessage[];
	pendingToolCalls: readonly string[];
	usage: AgentUsage;
	lastError?: string;
	/** 当 `lastError` 来自提供商流时的分类。 */
	lastErrorClass?: ProviderErrorClass;
}

// =============================================================================
// 工具
// =============================================================================

export interface AgentToolDefinition {
	name: string;
	description: string;
	inputSchema: Record<string, unknown>;
	lifecycle?: {
		/**
		 * 对此工具的成功调用是否完成当前运行。
		 */
		completesRun?: boolean;
	};
}

export interface AgentToolResult<TOutput = unknown> {
	output: TOutput;
	isError?: boolean;
	metadata?: Record<string, unknown>;
}

export interface AgentToolContext {
	sessionId?: string;
	agentId: string;
	conversationId?: string;
	runId?: string;
	iteration: number;
	toolCallId?: string;
	signal?: AbortSignal;
	metadata?: Record<string, unknown>;
	snapshot?: AgentRuntimeStateSnapshot;
	emitUpdate?: (update: unknown) => void;
}

export interface AgentTool<TInput = unknown, TOutput = unknown>
	extends AgentToolDefinition {
	/** 覆盖运行时执行模式。相邻的并行调用可能重叠；顺序调用形成排序边界。 */
	executionMode?: "sequential" | "parallel";
	timeoutMs?: number;
	retryable?: boolean;
	maxRetries?: number;
	execute: (
		input: TInput,
		context: AgentToolContext,
	) => Promise<TOutput> | TOutput;
}

// =============================================================================
// 模型适配器契约
// =============================================================================

export interface AgentModelRequest {
	systemPrompt?: string;
	messages: readonly AgentMessage[];
	tools: readonly AgentToolDefinition[];
	/** 为此模型请求启用的提供商执行工具。 */
	modelTools?: readonly import("./llms/model-tools").ModelTool[];
	signal?: AbortSignal;
	options?: Record<string, unknown>;
}

export interface AgentRuntimePrepareTurnContext {
	agentId: string;
	conversationId?: string;
	parentAgentId?: string | null;
	iteration: number;
	messages: readonly AgentMessage[];
	systemPrompt?: string;
	tools: readonly AgentToolDefinition[];
	model: {
		id?: string;
		provider?: string;
		info?: ModelInfo;
	};
	signal?: AbortSignal;
	/**
	 * 当上一次模型请求因超出模型上下文窗口被拒绝时设置；
	 * 要求 prepare-turn 管道强制执行压缩，而不是信任其 token 估算。
	 */
	overflowRecovery?: boolean;
	/**
	 * 提供商为本次运行的上一次请求实际计数的输入 token 数（可用时）。
	 * 压缩用它作为基于字符估算的下限，后者会低估密集内容
	 * （反汇编、图片转储），否则可能导致实际上下文超过窗口而不触发。
	 */
	previousRequestInputTokens?: number;
	emitStatusNotice?: (
		message: string,
		metadata?: Record<string, unknown>,
	) => void;
}

export interface AgentRuntimePrepareTurnResult {
	messages?: readonly AgentMessage[];
	systemPrompt?: string;
}

export type AgentModelFinishReason =
	| "stop"
	| "tool-calls"
	| "max-tokens"
	| "aborted"
	| "error";

/**
 * 提供商错误的粗粒度分类，从原始提供商错误对象派生，
 * 在被扁平化为显示字符串之前。由运行时的恢复策略和
 * 遥测（`error_class`）共享。可根据需要扩展新类别
 * （rate_limit、billing 等）。
 *
 * `auth`：提供商拒绝了请求的凭据（HTTP 401/403）——
 * 宿主应引导用户检查其 API 密钥配置。
 */
export type ProviderErrorClass = "context_window_exceeded" | "auth" | "unknown";

export type AgentModelEvent =
	| { type: "text-delta"; text: string }
	| { type: "media"; media: GeneratedMedia }
	| {
			type: "reasoning-delta";
			text: string;
			redacted?: boolean;
			metadata?: unknown;
	  }
	| {
			type: "tool-call-delta";
			index?: number;
			toolCallId?: string;
			toolName?: string;
			inputText?: string;
			input?: unknown;
			metadata?: unknown;
			/** 当执行由 AI SDK 或模型提供商处理时设置。 */
			execution?: ModelToolExecution;
	  }
	| {
			type: "tool-result";
			toolCallId: string;
			/**
			 * 声明的模型工具携带 ModelToolName；提供商执行的工具
			 * （如 Claude Code CLI 自身的工具）携带任意名称。
			 */
			toolName: string;
			input?: unknown;
			output: unknown;
			isError?: boolean;
			execution: ModelToolExecution;
	  }
	| {
			type: "usage";
			usage: Partial<AgentUsage>;
	  }
	| {
			type: "finish";
			reason: AgentModelFinishReason;
			/** 已浮现响应的 HTTP X-Request-ID，非提供商的生成 ID。 */
			requestId?: string;
			error?: string;
			errorClass?: ProviderErrorClass;
			/**
			 * 底层提供商错误是否为瞬态且值得重试，
			 * 在模型边界从 AI SDK 的类型化 `isRetryable` 标志决定，
			 * 此时结构化错误仍在手中（`error` 是扁平化字符串，
			 * 因此 Agent 循环无法重新推导）。缺失时，
			 * Agent 循环从消息中分类。
			 */
			errorRetryable?: boolean;
			/**
			 * 模型层已在其自身的错误边界为此失败记录了
			 * `sdk.error` 遥测。`error` 是扁平化字符串，
			 * 因此此标志跨边界传递报告所有权：
			 * Agent 循环在设置时跳过重复报告，
			 * 并对不记录自身遥测的模型实现仍然报告失败。
			 */
			errorReported?: boolean;
	  };

export interface AgentModel {
	stream: (
		request: AgentModelRequest,
	) => AsyncIterable<AgentModelEvent> | Promise<AsyncIterable<AgentModelEvent>>;
}

// =============================================================================
// Hook 上下文
// =============================================================================

export interface AgentBeforeModelContext {
	snapshot: AgentRuntimeStateSnapshot;
	request: AgentModelRequest;
}

export interface AgentStopControl {
	stop?: boolean;
	reason?: string;
}

export interface AgentRunStartResult {
	stop?: boolean;
	reason?: string;
	/**
	 * 作为 hook 上下文注入对话的文本（如 hook 的
	 * `contextModification`）。跨 hook 收集，在运行的输入消息之后
	 * 作为 `<hook_context>` 用户消息追加，使模型在运行的
	 * 第一次请求中看到它。
	 */
	appendContext?: string;
}

export interface AgentBeforeModelResult {
	stop?: boolean;
	reason?: string;
	messages?: readonly AgentMessage[];
	tools?: readonly AgentToolDefinition[];
	options?: Record<string, unknown>;
}

export interface AgentAfterModelContext {
	snapshot: AgentRuntimeStateSnapshot;
	assistantMessage: AgentMessage;
	finishReason: AgentModelFinishReason;
	/** 模型适配器暴露时的 HTTP X-Request-ID；不包含隐藏的重试 ID。 */
	requestId?: string;
}

export interface AgentBeforeToolContext {
	snapshot: AgentRuntimeStateSnapshot;
	tool: AgentTool;
	toolCall: AgentToolCallPart;
	input: unknown;
}

export interface AgentBeforeToolResult {
	skip?: boolean;
	stop?: boolean;
	reason?: string;
	input?: unknown;
	policy?: ToolPolicy;
	/**
	 * 作为 hook 上下文注入对话的文本（如 hook 的
	 * `contextModification`）。跨 hook 收集，在此迭代的工具结果之后
	 * 作为 `<hook_context>` 用户消息追加，使模型在下次请求中看到它。
	 */
	appendContext?: string;
}

export interface AgentAfterToolContext {
	snapshot: AgentRuntimeStateSnapshot;
	tool: AgentTool;
	toolCall: AgentToolCallPart;
	input: unknown;
	result: AgentToolResult;
	startedAt: Date;
	endedAt: Date;
	durationMs: number;
}

export interface AgentAfterToolResult {
	stop?: boolean;
	reason?: string;
	result?: AgentToolResult;
	/**
	 * 作为 hook 上下文注入对话的文本（如 hook 的
	 * `contextModification`）。跨 hook 收集，在此迭代的工具结果之后
	 * 作为 `<hook_context>` 用户消息追加，使模型在下次请求中看到它。
	 */
	appendContext?: string;
}

export interface AgentRunLifecycleContext {
	snapshot: AgentRuntimeStateSnapshot;
}

// =============================================================================
// 运行时 hook 集合
// =============================================================================

/**
 * `AgentRuntime` 消费的 7 个回调 hook 集合。
 */
export interface AgentRuntimeHooks {
	beforeRun?: (
		context: AgentRunLifecycleContext,
	) =>
		| AgentRunStartResult
		| undefined
		| Promise<AgentRunStartResult | undefined>;
	afterRun?: (
		context: AgentRunLifecycleContext & { result: AgentRunResult },
	) => void | Promise<void>;
	beforeModel?: (
		context: AgentBeforeModelContext,
	) =>
		| AgentBeforeModelResult
		| undefined
		| Promise<AgentBeforeModelResult | undefined>;
	afterModel?: (
		context: AgentAfterModelContext,
	) => AgentStopControl | undefined | Promise<AgentStopControl | undefined>;
	beforeTool?: (
		context: AgentBeforeToolContext,
	) =>
		| AgentBeforeToolResult
		| undefined
		| Promise<AgentBeforeToolResult | undefined>;
	afterTool?: (
		context: AgentAfterToolContext,
	) =>
		| AgentAfterToolResult
		| undefined
		| Promise<AgentAfterToolResult | undefined>;
	onEvent?: (event: AgentRuntimeEvent) => void | Promise<void>;
}

// =============================================================================
// 插件
// =============================================================================

export interface AgentRuntimePluginContext {
	agentId: string;
	agentRole?: AgentRole;
	systemPrompt?: string;
}

export interface AgentRuntimePluginSetup {
	// biome-ignore lint/suspicious/noExplicitAny: tool input/output types vary per tool
	tools?: readonly AgentTool<any, any>[];
	hooks?: Partial<AgentRuntimeHooks>;
}

export interface AgentRuntimePlugin {
	name: string;
	setup?: (
		context: AgentRuntimePluginContext,
	) =>
		| AgentRuntimePluginSetup
		| undefined
		| Promise<AgentRuntimePluginSetup | undefined>;
}

// =============================================================================
// 运行时配置
// =============================================================================

export interface AgentRuntimeConfig {
	/**
	 * 用于提供商和可观测性元数据的稳定终端用户区分 ID。
	 * 这有意与宿主拥有的会话 id 分开。
	 */
	distinctId?: string;
	/** 调用的客户端界面，例如 `cline-vscode` 或 `cline-sdk`。 */
	clientName?: string;
	/** 调用的客户端版本，如 VS Code 扩展版本。 */
	clientVersion?: string;
	/** 执行运行时的 Cline Core SDK 版本。 */
	clineCoreVersion?: string;
	/**
	 * Core/hub 运行时会话标识符。
	 *
	 * 包含此运行时的任务/会话的宿主拥有生命周期 id。
	 * 它对 hub 订阅、会话持久化、中止/停止命令和审批路由是稳定的。
	 * 它可能与 `conversationId` 不同，后者跟踪 Agent 对话记录。
	 */
	sessionId?: string;
	agentId?: string;
	/**
	 * Agent 对话/记录标识符。
	 *
	 * 被无状态 Agent 循环、工具、hooks、遥测和模型历史关联使用。
	 * 此 id 跟随当前对话存储，不应用作 hub/会话路由键。
	 */
	conversationId?: string;
	parentAgentId?: string | null;
	agentRole?: AgentRole;
	systemPrompt?: string;
	messageModelInfo?: AgentMessage["modelInfo"];
	model: AgentModel;
	modelOptions?: Record<string, unknown>;
	/** 提供商执行的工具，与本地执行的 AgentTools 分开。 */
	modelTools?: readonly import("./llms/model-tools").ModelTool[];
	// biome-ignore lint/suspicious/noExplicitAny: tool input/output types vary per tool
	tools?: readonly AgentTool<any, any>[];
	hooks?: Partial<AgentRuntimeHooks>;
	plugins?: readonly AgentRuntimePlugin[];
	logger?: BasicLogger;
	telemetry?: ITelemetryService;
	initialMessages?: readonly AgentMessage[];
	maxIterations?: number;
	completionPolicy?: {
		requireCompletionTool?: boolean;
		completionGuard?: () => string | undefined;
	};
	toolExecution?: "sequential" | "parallel";
	toolPolicies?: Record<string, ToolPolicy>;
	toolContextMetadata?: Record<string, unknown>;
	requestToolApproval?: (
		request: ToolApprovalRequest,
	) => Promise<ToolApprovalResult> | ToolApprovalResult;
	/**
	 * 可选的宿主拥有的请求投影 hook，在每次模型调用前调用。
	 *
	 * 返回的消息仅影响当前调用的提供商请求。
	 * 它们不替换规范运行时对话记录，不作为会话历史持久化，
	 * 也不反映在 AgentRunResult.messages 中。
	 */
	prepareTurn?: (
		context: AgentRuntimePrepareTurnContext,
	) =>
		| Promise<AgentRuntimePrepareTurnResult | undefined>
		| AgentRuntimePrepareTurnResult
		| undefined;
	// 可选的宿主回调，交互式会话用来在 Agent 循环迭代之间、
	// 下一次模型请求之前注入排队的用户引导消息。
	consumePendingUserMessage?: () =>
		| string
		| undefined
		| Promise<string | undefined>;
}

// =============================================================================
// 运行时事件联合类型
// =============================================================================

export type AgentRuntimeEvent =
	| {
			type: "run-started";
			snapshot: AgentRuntimeStateSnapshot;
	  }
	| {
			type: "message-added";
			snapshot: AgentRuntimeStateSnapshot;
			message: AgentMessage;
	  }
	| {
			type: "turn-started";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
	  }
	| {
			type: "assistant-text-delta";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			text: string;
			accumulatedText: string;
	  }
	| {
			type: "assistant-reasoning-delta";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			text: string;
			accumulatedText: string;
			redacted?: boolean;
			metadata?: unknown;
	  }
	| {
			type: "assistant-media";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			media: GeneratedMedia;
	  }
	| {
			type: "assistant-message";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			message: AgentMessage;
			finishReason: AgentModelFinishReason;
	  }
	| {
			type: "tool-started";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			toolCall: AgentToolCallPart;
	  }
	| {
			type: "tool-updated";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			toolCall: AgentToolCallPart;
			update: unknown;
	  }
	| {
			type: "tool-finished";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			toolCall: AgentToolCallPart;
			message: AgentMessage;
	  }
	| {
			type: "usage-updated";
			snapshot: AgentRuntimeStateSnapshot;
			usage: AgentUsage;
	  }
	| {
			type: "turn-finished";
			snapshot: AgentRuntimeStateSnapshot;
			iteration: number;
			toolCallCount: number;
	  }
	| {
			type: "status-notice";
			snapshot: AgentRuntimeStateSnapshot;
			message: string;
			metadata?: Record<string, unknown>;
	  }
	| {
			type: "run-finished";
			snapshot: AgentRuntimeStateSnapshot;
			result: AgentRunResult;
	  }
	| {
			type: "run-failed";
			snapshot: AgentRuntimeStateSnapshot;
			error: Error;
			/** 使运行失败的提供商错误分类。 */
			errorClass?: ProviderErrorClass;
	  };

// =============================================================================
// 运行结果
// =============================================================================

export interface AgentRunResult {
	agentId: string;
	agentRole?: AgentRole;
	runId: string;
	status: Exclude<AgentRunStatus, "idle" | "running">;
	iterations: number;
	outputText: string;
	messages: readonly AgentMessage[];
	usage: AgentUsage;
	error?: Error;
}
