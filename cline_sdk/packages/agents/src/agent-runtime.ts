import {
	classifyProviderError,
	createGateway,
	type GatewayProviderSettings,
	isRetryableProviderError,
} from "@cline/llms";
import type {
	AgentAfterToolResult,
	AgentBeforeModelResult,
	AgentBeforeToolResult,
	AgentMessage,
	AgentMessagePart,
	AgentModel,
	AgentModelEvent,
	AgentModelFinishReason,
	AgentModelRequest,
	AgentModelToolActivity,
	AgentRunResult,
	AgentRuntimeEvent,
	AgentRuntimeHooks,
	AgentRuntimeStateSnapshot,
	AgentStopControl,
	AgentTool,
	AgentToolCallPart,
	AgentToolDefinition,
	AgentToolResult,
	AgentUsage,
	AgentRuntimeConfig as BaseAgentRuntimeConfig,
	CaptureTaskLifecycleEventInput,
	ProviderErrorClass,
	TelemetryProperties,
	ToolApprovalResult,
	ToolPolicy,
} from "@cline/shared";
import {
	captureAgentUnexpectedReasoningTokens,
	captureSdkError,
	captureTaskLifecycleEvent,
	estimateTokens,
	mergeModelOptions,
	normalizeJsonLikeStringsForSchema,
	omitUndefinedValues,
	TASK_CANCELLED_EVENT,
	TASK_FIRST_CHUNK_RECEIVED_EVENT,
	TASK_MAX_TOKENS_RECOVERY_EVENT,
	TASK_PROVIDER_REQUEST_STARTED_EVENT,
	TASK_PROVIDER_STREAM_FAILED_EVENT,
	TASK_PROVIDER_STREAM_STARTED_EVENT,
	TOOL_REJECTION_SUFFIX,
	trimNonEmpty,
} from "@cline/shared";
import { nanoid } from "nanoid";

/** 经过轮次准备（turn preparation）之后、可以发出的模型请求（可能发出不止一次）。 */
interface PreparedModelRequest {
	request: AgentModelRequest;
	/** 准备开始的时间；作为提供商请求生命周期计时（timings）的锚点。 */
	startedAt: number;
}

const MAX_TOKENS_INCOMPLETE_TURN_MESSAGE =
	"Model reached the maximum output token limit before completing the turn";

/**
 * 一个在模型输出 token 上限处结束、且没有产生可用工具调用的轮次，
 * 在整次运行失败之前会被重试多少次。每次重试都会提示（nudge）模型更简洁
 * （见 MAX_TOKENS_RECOVERY_NUDGE）。计数器会在任何有进展的轮次
 * （产生工具调用）上重置，因此它只限制「连续被截断轮次的长度」——
 * 单次过长的响应可以恢复，而持续溢出的模型仍会结束而不是永远循环。
 */
const MAX_TOKENS_RECOVERY_LIMIT = 3;
/** 在输出上限截断后追加的提示，要求更简洁的输出。 */
const MAX_TOKENS_RECOVERY_NUDGE =
	"Your previous response was cut off because it reached the model's output-token limit before finishing. Keep responses concise: take one small step at a time, avoid long explanations, and write large files or command output in smaller chunks across multiple tool calls.";

/**
 * 一个因「瞬时的提供商侧错误」（速率限制、5xx、网络抖动、OpenRouter 的
 * 通用「Provider returned error」）而失败的模型轮次会被重试多少次。
 * 首次尝试不计数，所以值为 3 意味着一个轮次最多发出 4 次请求。
 * 只重试瞬时错误——绝不重试认证、上下文溢出或其他客户端错误
 * （见 {@link isRetryableProviderError}）——让行为良好的提供商保持现有的
 * 单请求路径，因此对端点不抛瞬时错误的模型不会改变行为。
 */
const PROVIDER_ERROR_MAX_RETRIES = 3;
/** 第一次重试前的基础退避（backoff）；之后每次尝试翻倍。 */
const PROVIDER_ERROR_RETRY_BASE_DELAY_MS = 1_000;
/** 任何单次退避等待的上限。 */
const PROVIDER_ERROR_RETRY_MAX_DELAY_MS = 15_000;

/**
 * 当上下文窗口溢出因「没有可压缩的对话历史」而无法恢复时的终止消息——
 * 仅系统 prompt、工具与当前输入本身就已超出窗口。
 */
export const CONTEXT_WINDOW_OVERFLOW_NOTHING_TO_COMPACT_MESSAGE =
	"The request exceeds the model's context window and there is no conversation history to compact — the system prompt, tools, and current input alone are too large. Reduce attached content or switch to a model with a larger context window.";

/**
 * 当运行时已压缩对话并重试过一次后、上下文窗口溢出仍然存在时的终止消息。
 */
export const CONTEXT_WINDOW_OVERFLOW_RECOVERY_FAILED_MESSAGE =
	"The conversation still exceeds the model's context window after compacting it. Start a new session or switch to a model with a larger context window.";

/**
 * 当没有可用的压缩管线来从上下文窗口溢出中恢复（例如压缩被禁用）时的终止消息。
 */
export const CONTEXT_WINDOW_OVERFLOW_NO_RECOVERY_MESSAGE =
	"The conversation exceeds the model's context window. Compact the conversation, start a new session, or switch to a model with a larger context window.";

/** 当溢出恢复无法继续时抛出；携带终止文本。 */
class ContextWindowOverflowError extends Error {
	constructor(message: string, providerError: string | undefined) {
		super(
			providerError?.trim()
				? `${message} (provider reported: ${providerError.trim()})`
				: message,
		);
		this.name = "ContextWindowOverflowError";
	}
}

// 本地的 `createUID` 助手。clinee 源码从
// `@cline/shared` 导入它（见 `packages/shared/dist/identifier.ts`），但
// sdk-re 的 shared 包尚未暴露它。内联在这里让
// PLAN.md Step 1 的范围保持在 `packages/agents/src/` 内，并与
// clinee 的精确实现一致（`${prefix}_${nanoid(length)}`）。
function createUID(prefix: string, length = 8): string {
	return `${prefix}_${nanoid(length)}`;
}

export type AgentRunInput = string | AgentMessage | readonly AgentMessage[];
export type AgentEventListener = (event: AgentRuntimeEvent) => void;

/**
 * 高级形式：调用方提供预构建的 `AgentModel`。由 `@cline/core` 使用，
 * 它自己构造模型，以便与会话运行时的其余部分共享网关/遥测接线。
 */
export interface AgentRuntimeConfigWithModel extends BaseAgentRuntimeConfig {
	model: AgentModel;
}

/**
 * 友好形式：调用方提供提供商/模型 ID 与凭据，运行时通过 `@cline/llms`
 * 在内部构建 `AgentModel`。这是大多数独立用户想要的入口点。
 */
export interface AgentRuntimeConfigWithProvider
	extends Omit<BaseAgentRuntimeConfig, "model"> {
	/** 提供商 ID（例如 "anthropic"、"openai"） */
	providerId: string;
	/** 要使用的模型 ID */
	modelId: string;
	/** 提供商的 API key */
	apiKey?: string;
	/** API 的自定义 base URL */
	baseUrl?: string;
	/** API 请求的附加 headers */
	headers?: Record<string, string>;
	/** 提供商特定的网关选项 */
	options?: GatewayProviderSettings["options"];
}

/**
 * `new AgentRuntime(...)` / `createAgentRuntime(...)` /
 * `new Agent(...)` / `createAgent(...)` 接受的配置。要么提供预构建的
 * `model`（高级），要么提供 `providerId` + `modelId`（+ 凭据），
 * 运行时将通过 `@cline/llms` 自行构造模型。
 */
export type AgentRuntimeConfig =
	| AgentRuntimeConfigWithModel
	| AgentRuntimeConfigWithProvider;

function hasPrebuiltModel(
	config: AgentRuntimeConfig,
): config is AgentRuntimeConfigWithModel {
	return (config as AgentRuntimeConfigWithModel).model !== undefined;
}

function resolveRuntimeConfig(
	config: AgentRuntimeConfig,
): BaseAgentRuntimeConfig {
	if (hasPrebuiltModel(config)) {
		return config;
	}
	const { providerId, modelId, apiKey, baseUrl, headers, options, ...rest } =
		config;
	const gateway = createGateway({
		providerConfigs: [{ providerId, apiKey, baseUrl, headers, options }],
		telemetry: rest.telemetry,
	});
	const model = gateway.createAgentModel({ providerId, modelId });
	// 预构建模型路径会保留调用方提供的 messageModelInfo；
	// 这里镜像该行为，使「提供商/模型构造」路径也会给助手消息打上
	// modelInfo 标签。调用方显式提供的值仍然优先。
	const messageModelInfo = rest.messageModelInfo ?? {
		id: modelId,
		provider: providerId,
	};
	return { ...rest, model, messageModelInfo };
}

function resolveToolPolicy(
	toolName: string,
	policies: BaseAgentRuntimeConfig["toolPolicies"],
): ToolPolicy {
	return {
		...(policies?.["*"] ?? {}),
		...(policies?.[toolName] ?? {}),
	};
}

interface PendingToolAssembly {
	toolCallId: string;
	toolName?: string;
	inputText: string;
	inputValue?: unknown;
	metadata?: unknown;
	parseError?: string;
}

interface InvalidToolCall {
	toolCallId: string;
	toolName?: string;
	input: Record<string, unknown>;
	reason: "missing_name" | "missing_arguments" | "invalid_arguments";
}

function safeJsonSize(value: unknown): number {
	try {
		return JSON.stringify(value).length;
	} catch {
		return String(value).length;
	}
}

function getOutputSize(output: unknown): number {
	if (typeof output === "string") {
		return output.length;
	}
	return safeJsonSize(output);
}

function summarizeModelRequest(
	request: AgentModelRequest,
): Record<string, unknown> {
	let textChars = request.systemPrompt?.length ?? 0;
	let toolResultCount = 0;
	let toolResultChars = 0;
	let maxToolResultChars = 0;
	for (const message of request.messages) {
		for (const part of message.content) {
			switch (part.type) {
				case "text":
					textChars += part.text.length;
					break;
				case "reasoning":
					textChars += part.text.length;
					break;
				case "file":
					textChars += part.content.length;
					break;
				case "tool-call":
					textChars += safeJsonSize(part.input);
					break;
				case "tool-result": {
					const outputChars = getOutputSize(part.output);
					toolResultCount += 1;
					toolResultChars += outputChars;
					maxToolResultChars = Math.max(maxToolResultChars, outputChars);
					textChars += outputChars;
					break;
				}
			}
		}
	}

	return {
		messageCount: request.messages.length,
		toolSchemaCount: request.tools.length,
		systemPromptChars: request.systemPrompt?.length ?? 0,
		requestJsonChars: safeJsonSize({
			systemPrompt: request.systemPrompt,
			messages: request.messages,
			tools: request.tools,
			options: request.options,
		}),
		visibleTextChars: textChars,
		estimatedTextTokens: estimateTokens(textChars),
		toolResultCount,
		toolResultChars,
		maxToolResultChars,
	};
}

interface PreparedToolExecution {
	toolCall: AgentToolCallPart;
	tool?: AgentTool;
	input: unknown;
	skipReason?: string;
}

interface HookBag {
	beforeRun: NonNullable<AgentRuntimeHooks["beforeRun"]>[];
	afterRun: NonNullable<AgentRuntimeHooks["afterRun"]>[];
	beforeModel: NonNullable<AgentRuntimeHooks["beforeModel"]>[];
	afterModel: NonNullable<AgentRuntimeHooks["afterModel"]>[];
	beforeTool: NonNullable<AgentRuntimeHooks["beforeTool"]>[];
	afterTool: NonNullable<AgentRuntimeHooks["afterTool"]>[];
	onEvent: NonNullable<AgentRuntimeHooks["onEvent"]>[];
}

class ControlledStopError extends Error {
	readonly reason?: string;

	constructor(reason?: string) {
		super(reason ?? "Run stopped by runtime control");
		this.name = "ControlledStopError";
		this.reason = reason;
	}
}

export class AgentRuntimeAbortError extends Error {
	readonly reason?: unknown;

	constructor(reason?: unknown) {
		const message =
			typeof reason === "string"
				? reason
				: reason instanceof Error
					? reason.message
					: reason === undefined
						? "Run aborted"
						: String(reason);
		super(message);
		this.name = "AgentRuntimeAbortError";
		this.reason = reason;
	}
}

const DEFAULT_USAGE: AgentUsage = {
	inputTokens: 0,
	outputTokens: 0,
	cacheReadTokens: 0,
	cacheWriteTokens: 0,
};

function createMessage(
	role: AgentMessage["role"],
	content: AgentMessagePart[],
	metadata?: Record<string, unknown>,
): AgentMessage {
	return {
		id: createUID("msg"),
		role,
		content,
		createdAt: Date.now(),
		metadata,
	};
}

function cloneUsage(usage: AgentUsage): AgentUsage {
	return { ...usage };
}

const HOOK_ATTRIBUTE_ESCAPES: Record<string, string> = {
	_: "__",
	'"': "_q_",
	"<": "_lt_",
	">": "_gt_",
};

function sanitizeHookAttribute(value: string): string {
	// 下划线会对自身转义，使编码具有单射性
	// （可唯一解码的转义码）：任何两个不同的 id 都不可能折叠成
	// 同一个净化后的标记。
	return value.replace(/[_"<>]/g, (char) => HOOK_ATTRIBUTE_ESCAPES[char]);
}

/**
 * hook 上下文块的来源。工具 hook 携带它们所服务的调用；
 * 运行启动类 hook（各层各种拼写的 TaskStart/UserPromptSubmit/TaskResume）
 * 没有工具身份，而且各层会在运行时看到它们之前就合并输出，
 * 因此用一个通用的 source 标记这些块。
 */
type HookContextOrigin =
	| { source: "RunStart" }
	| { source: "PreToolUse" | "PostToolUse"; toolCall: AgentToolCallPart };

function formatHookContextBlock(
	origin: HookContextOrigin,
	text: string,
): string {
	// 工具身份让每个块都能归属到它对应的调用：上下文会在工具结果之后
	// 批量合并进一条消息，而并行工具执行按完成顺序收集，
	// 所以单凭位置无法识别工具。属性值经过净化处理，内嵌的
	// hook_context 标签（开、闭）都被中和，这样提供商提供的 id 和
	// hook 输出都无法破坏或伪造块标记。
	const attributes = [`source="${origin.source}"`];
	if ("toolCall" in origin) {
		attributes.push(
			`tool_name="${sanitizeHookAttribute(origin.toolCall.toolName)}"`,
			`tool_call_id="${sanitizeHookAttribute(origin.toolCall.toolCallId)}"`,
		);
	}
	const body = text.trim().replace(/<(\/?)hook_context/gi, "<\\$1hook_context");
	return `<hook_context ${attributes.join(" ")}>\n${body}\n</hook_context>`;
}

function cloneMessages(messages: readonly AgentMessage[]): AgentMessage[] {
	return messages.map((message) => ({
		...message,
		content: message.content.map((part: AgentMessagePart) => ({ ...part })),
		metadata: message.metadata ? { ...message.metadata } : undefined,
		modelInfo: message.modelInfo ? { ...message.modelInfo } : undefined,
		metrics: message.metrics ? { ...message.metrics } : undefined,
	}));
}

function usageDelta(
	start: AgentUsage,
	end: AgentUsage,
): NonNullable<AgentMessage["metrics"]> | undefined {
	const inputTokens = Math.max(
		0,
		(end.inputTokens ?? 0) - (start.inputTokens ?? 0),
	);
	const outputTokens = Math.max(
		0,
		(end.outputTokens ?? 0) - (start.outputTokens ?? 0),
	);
	const cacheReadTokens = Math.max(
		0,
		(end.cacheReadTokens ?? 0) - (start.cacheReadTokens ?? 0),
	);
	const cacheWriteTokens = Math.max(
		0,
		(end.cacheWriteTokens ?? 0) - (start.cacheWriteTokens ?? 0),
	);
	const reasoningTokenCount = Math.max(
		0,
		(end.reasoningTokenCount ?? 0) - (start.reasoningTokenCount ?? 0),
	);
	const startCost = start.totalCost ?? 0;
	const endCost = end.totalCost ?? 0;
	const cost = Math.max(0, endCost - startCost);
	if (
		inputTokens === 0 &&
		outputTokens === 0 &&
		cacheReadTokens === 0 &&
		cacheWriteTokens === 0 &&
		reasoningTokenCount === 0 &&
		cost === 0
	) {
		return undefined;
	}
	return {
		inputTokens: inputTokens > 0 ? inputTokens : 0,
		outputTokens: outputTokens > 0 ? outputTokens : 0,
		cacheReadTokens: cacheReadTokens > 0 ? cacheReadTokens : 0,
		cacheWriteTokens: cacheWriteTokens > 0 ? cacheWriteTokens : 0,
		...(reasoningTokenCount > 0 ? { reasoningTokenCount } : {}),
		...(cost > 0 ? { cost } : {}),
	};
}

function reasoningWasRequestedOff(request: AgentModelRequest): boolean {
	return request.options?.thinking === false;
}

function textFromMessage(message: AgentMessage | undefined): string {
	if (!message) {
		return "";
	}
	return message.content
		.filter(
			(
				part: AgentMessagePart,
			): part is Extract<AgentMessagePart, { type: "text" }> =>
				part.type === "text",
		)
		.map((part: Extract<AgentMessagePart, { type: "text" }>) => part.text)
		.join("");
}

function textFromToolMessage(message: AgentMessage | undefined): string {
	const result = message?.content.find(
		(part): part is Extract<AgentMessagePart, { type: "tool-result" }> =>
			part.type === "tool-result",
	);
	if (!result || result.isError) {
		return "";
	}
	if (typeof result.output === "string") {
		return result.output;
	}
	try {
		return JSON.stringify(result.output);
	} catch {
		return String(result.output);
	}
}

function normalizeInput(input: AgentRunInput): AgentMessage[] {
	if (typeof input === "string") {
		return [createMessage("user", [{ type: "text", text: input }])];
	}
	if (Array.isArray(input)) {
		return cloneMessages(input);
	}
	return cloneMessages([input as AgentMessage]);
}

export class AgentRuntime {
	private config: Required<Pick<BaseAgentRuntimeConfig, "toolExecution">> &
		BaseAgentRuntimeConfig;
	private readonly listeners = new Set<AgentEventListener>();
	// biome-ignore lint/suspicious/noExplicitAny: tool input/output types vary per tool
	private readonly tools = new Map<string, AgentTool<any, any>>();
	private hooks: HookBag = {
		beforeRun: [],
		afterRun: [],
		beforeModel: [],
		afterModel: [],
		beforeTool: [],
		afterTool: [],
		onEvent: [],
	};
	/**
	 * 等待注入为一条用户消息的 `appendContext` 块缓冲。
	 * beforeRun hook 在运行的第一次模型请求之前填充它；beforeTool
	 * 和 afterTool hook 在某次迭代的工具执行期间填充它，并在工具结果之后
	 * 冲刷（flush），这样工具结果部分就能保持连续，满足那些要求它们
	 * 在下一轮中排在最前的提供商。
	 */
	private pendingHookContexts: string[] = [];
	private readonly state = {
		agentId: "",
		agentRole: undefined as string | undefined,
		parentAgentId: undefined as string | null | undefined,
		runId: undefined as string | undefined,
		status: "idle" as AgentRuntimeStateSnapshot["status"],
		iteration: 0,
		messages: [] as AgentMessage[],
		pendingToolCalls: [] as string[],
		usage: cloneUsage(DEFAULT_USAGE),
		lastError: undefined as string | undefined,
		lastErrorClass: undefined as ProviderErrorClass | undefined,
		/** 本次运行最近一次请求中提供商上报的输入 token 数。 */
		lastRequestInputTokens: 0,
		/**
		 * 上一次提供商失败是否为「瞬时的、值得重试的」错误；由模型边界
		 * 经 `finish` 事件上的 `errorRetryable` 传递（AI SDK 的类型化
		 * `isRetryable` 标志）。没有提供该信号时为 undefined，此时
		 * agent 循环改为根据展平后的 `lastError` 消息进行分类。
		 */
		lastErrorRetryable: undefined as boolean | undefined,
		/**
		 * 模型层是否已为 `lastError` 记录过 `sdk.error` 遥测（来自流
		 * `finish` 事件上的 `errorReported`）。不自行记录遥测的自定义
		 * `AgentModel` 实现会保持它为 false，因此它们的失败仍会被上报。
		 */
		lastErrorReported: false,
	};
	/** 每次运行只做一次自动的溢出恢复尝试。 */
	private overflowRecoveryAttempted = false;
	/** 本次运行已恢复的连续输出上限截断次数；见 MAX_TOKENS_RECOVERY_LIMIT。 */
	private maxTokensRecoveryCount = 0;
	/** 针对「被 max-tokens 截断的轮次」每次运行只做一次自动恢复尝试。 */
	private maxTokensRecoveryAttempted = false;
	private initialization?: Promise<void>;
	private abortController?: AbortController;
	private modelSteerController?: AbortController;
	private readonly telemetryProviderId?: string;
	private readonly telemetryModelId?: string;

	constructor(config: AgentRuntimeConfig) {
		this.telemetryProviderId =
			trimNonEmpty(config.messageModelInfo?.provider) ??
			("providerId" in config ? trimNonEmpty(config.providerId) : undefined);
		this.telemetryModelId =
			trimNonEmpty(config.messageModelInfo?.id) ??
			("modelId" in config ? trimNonEmpty(config.modelId) : undefined);
		const resolved = resolveRuntimeConfig(config);
		this.config = {
			...resolved,
			toolExecution: resolved.toolExecution ?? "sequential",
		};
		this.state.agentId = resolved.agentId ?? createUID("agent");
		this.state.agentRole = resolved.agentRole;
		this.state.parentAgentId = resolved.parentAgentId;
		this.state.messages = cloneMessages(resolved.initialMessages ?? []);
	}

	async run(input: AgentRunInput): Promise<AgentRunResult> {
		return this.execute(input);
	}

	async continue(input?: AgentRunInput): Promise<AgentRunResult> {
		return this.execute(input);
	}

	/** 只打断当前的模型请求；正在运行的工具会正常完成。 */
	notifyPendingUserMessage(): void {
		this.modelSteerController?.abort();
	}

	abort(reason?: unknown): void {
		if (!this.abortController) {
			return;
		}
		if (this.abortController.signal.aborted) {
			return;
		}
		const abortError =
			reason instanceof AgentRuntimeAbortError
				? reason
				: new AgentRuntimeAbortError(reason);
		this.state.lastError = abortError.message;
		this.captureTaskLifecycle(TASK_CANCELLED_EVENT, {
			error: abortError,
		});
		this.abortController.abort(abortError);
	}

	subscribe(listener: AgentEventListener): () => void {
		this.listeners.add(listener);
		return () => {
			this.listeners.delete(listener);
		};
	}

	/**
	 * 用一组全新的消息替换整个对话，丢弃进行中的运行与用量状态，
	 * 同时保留底层的模型、工具、hook、插件与活跃的事件订阅者。
	 *
	 * 适用于那些在外部持久化对话、希望从存储重新播种（re-seed）
	 * 运行时而不重建订阅者的独立调用方。
	 */
	restore(messages: readonly AgentMessage[]): void {
		this.abort("Agent state restored");
		// 重置不跨 restore 保留的状态。保留 `listeners`、
		// 工具、hook、插件、模型与 agent 身份，让外部事件
		// 订阅者在 restore() 之后仍能收到事件。
		this.state.runId = undefined;
		this.state.status = "idle";
		this.state.iteration = 0;
		this.state.pendingToolCalls = [];
		this.state.usage = cloneUsage(DEFAULT_USAGE);
		this.state.lastError = undefined;
		this.state.lastErrorClass = undefined;
		this.state.lastErrorRetryable = undefined;
		this.state.lastErrorReported = false;
		this.state.messages = cloneMessages(messages);
		this.config = {
			...this.config,
			initialMessages: cloneMessages(messages),
		};
	}

	snapshot(): AgentRuntimeStateSnapshot {
		return {
			agentId: this.state.agentId,
			agentRole: this.state.agentRole,
			parentAgentId: this.state.parentAgentId,
			conversationId: this.config.conversationId?.trim() || undefined,
			runId: this.state.runId,
			status: this.state.status,
			iteration: this.state.iteration,
			messages: cloneMessages(this.state.messages),
			pendingToolCalls: [...this.state.pendingToolCalls],
			usage: cloneUsage(this.state.usage),
			lastError: this.state.lastError,
			lastErrorClass: this.state.lastErrorClass,
		};
	}

	private async ensureInitialized(): Promise<void> {
		this.initialization ??= this.initialize();
		await this.initialization;
	}

	private async initialize(): Promise<void> {
		this.registerHooks(this.config.hooks);
		for (const tool of this.config.tools ?? []) {
			this.tools.set(tool.name, tool);
		}
		for (const plugin of this.config.plugins ?? []) {
			const setup = await plugin.setup?.({
				agentId: this.state.agentId,
				agentRole: this.state.agentRole,
				systemPrompt: this.config.systemPrompt,
			});
			for (const tool of setup?.tools ?? []) {
				this.tools.set(tool.name, tool);
			}
			this.registerHooks(setup?.hooks);
		}
	}

	private registerHooks(hooks: Partial<AgentRuntimeHooks> | undefined): void {
		if (!hooks) {
			return;
		}
		if (hooks.beforeRun) this.hooks.beforeRun.push(hooks.beforeRun);
		if (hooks.afterRun) this.hooks.afterRun.push(hooks.afterRun);
		if (hooks.beforeModel) this.hooks.beforeModel.push(hooks.beforeModel);
		if (hooks.afterModel) this.hooks.afterModel.push(hooks.afterModel);
		if (hooks.beforeTool) this.hooks.beforeTool.push(hooks.beforeTool);
		if (hooks.afterTool) this.hooks.afterTool.push(hooks.afterTool);
		if (hooks.onEvent) this.hooks.onEvent.push(hooks.onEvent);
	}

	private getRequiredCompletionToolNames(): string[] {
		if (this.config.completionPolicy?.requireCompletionTool !== true) {
			return [];
		}
		return [...this.tools.values()]
			.filter((tool) => tool.lifecycle?.completesRun === true)
			.map((tool) => tool.name)
			.sort();
	}

	private getCompletionToolReminderMessage(): string | undefined {
		const terminalToolNames = this.getRequiredCompletionToolNames();
		if (terminalToolNames.length === 0) {
			return undefined;
		}
		return `[SYSTEM] This run is not complete until you call one of these terminal completion tools: ${terminalToolNames.join(
			", ",
		)}. Continue working if requirements are not met. If the task is complete, call the appropriate terminal completion tool now.`;
	}

	private getCompletionReminderMessages(): string[] {
		return [
			this.getCompletionToolReminderMessage(),
			this.config.completionPolicy?.completionGuard?.(),
		].filter((message): message is string => Boolean(message));
	}

	private async addUserReminderMessage(text: string): Promise<AgentMessage> {
		const reminderMessage = createMessage("user", [{ type: "text", text }], {
			userRunSpan: 0,
		});
		this.state.messages.push(reminderMessage);
		await this.emit({
			type: "message-added",
			snapshot: this.snapshot(),
			message: reminderMessage,
		});
		return reminderMessage;
	}

	private async execute(input?: AgentRunInput): Promise<AgentRunResult> {
		await this.ensureInitialized();
		if (this.state.status === "running") {
			throw new Error("Agent runtime is already running");
		}

		this.abortController = new AbortController();
		this.state.runId = createUID("run");
		this.state.status = "running";
		this.state.iteration = 0;
		this.state.pendingToolCalls = [];
		this.state.lastError = undefined;
		this.state.lastErrorClass = undefined;
		this.state.lastErrorRetryable = undefined;
		this.state.lastErrorReported = false;
		this.state.usage = cloneUsage(DEFAULT_USAGE);
		this.overflowRecoveryAttempted = false;
		this.state.lastRequestInputTokens = 0;
		this.pendingHookContexts = [];
		this.maxTokensRecoveryCount = 0;
		this.maxTokensRecoveryAttempted = false;

		try {
			await this.callBeforeRunHooks();
			await this.emit({ type: "run-started", snapshot: this.snapshot() });

			for (const message of input ? normalizeInput(input) : []) {
				this.state.messages.push(message);
				await this.emit({
					type: "message-added",
					snapshot: this.snapshot(),
					message,
				});
			}

			const completionToolReminder = this.getCompletionToolReminderMessage();
			if (completionToolReminder) {
				await this.addUserReminderMessage(completionToolReminder);
			}

			// beforeRun hook 收集的上下文会排在运行的输入消息之后，
			// 让模型在运行的第一次请求中就能看到它。
			await this.flushPendingHookContexts();

			let finalAssistantMessage: AgentMessage | undefined;

			while (
				this.config.maxIterations === undefined ||
				this.state.iteration < this.config.maxIterations
			) {
				this.throwIfAborted();

				this.state.iteration += 1;
				await this.emit({
					type: "turn-started",
					snapshot: this.snapshot(),
					iteration: this.state.iteration,
				});

				// 每轮都从干净的「错误板面」开始：上一轮的任何东西都不得
				// 泄漏进本轮的误差分类或重试决策。
				this.resetLastError();
				const { message, finishReason, interrupted } =
					await this.generateAssistantMessageWithProviderRetry();
				if (interrupted && message.content.length === 0) {
					await this.emit({
						type: "turn-finished",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						toolCallCount: 0,
					});
					continue;
				}
				if (finishReason === "aborted") {
					throw this.normalizeAbortError();
				}
				if (message.content.length === 0) {
					if (finishReason === "error") {
						throw new Error(this.state.lastError ?? "Model stream failed");
					}
					// 提供商侧执行的工具活动存放在消息 metadata 里，而不是
					// content（投影进 content 会重放模型从未拿到结果的
					// tool_use 块）。一个只有此类活动的轮次并非空的：
					// 保留这条消息，让对话记录与展示投影保留它。
					// 重放依然安全——编解码器会把空 content 渲染成占位文本块。
					const modelToolActivities = message.metadata?.modelToolActivities;
					const hasModelToolActivity =
						Array.isArray(modelToolActivities) &&
						modelToolActivities.length > 0;
					// 因触及输出 token 上限而没有产生内容的轮次并不是真正的
					// 空响应：继续往下走，保留消息，让下面的 max-tokens
					// 恢复分支去提示并重试。
					if (!hasModelToolActivity && finishReason !== "max-tokens") {
						throw new Error("Model returned empty response");
					}
				}
				const toolCalls = message.content.filter(
					(part: AgentMessagePart): part is AgentToolCallPart =>
						part.type === "tool-call",
				);

				finalAssistantMessage = message;
				await this.recordAssistantMessage(message, finishReason);

				if (interrupted) {
					await this.emit({
						type: "turn-finished",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						toolCallCount: 0,
					});
					continue;
				}

				if (finishReason === "max-tokens" && toolCalls.length === 0) {
					if (await this.recoverFromIncompleteMaxTokensTurn()) {
						await this.emit({
							type: "turn-finished",
							snapshot: this.snapshot(),
							iteration: this.state.iteration,
							toolCallCount: 0,
						});
						continue;
					}
					throw new Error(MAX_TOKENS_INCOMPLETE_TURN_MESSAGE);
				}
				if (finishReason === "error" && toolCalls.length === 0) {
					throw new Error(this.state.lastError ?? "Model stream failed");
				}
				// 产生工具调用的轮次就是进展：重置「截断连击」计数。
				if (toolCalls.length > 0) {
					this.maxTokensRecoveryCount = 0;
				}
				this.state.pendingToolCalls = toolCalls.map((part) => part.toolCallId);

				if (toolCalls.length === 0) {
					await this.emit({
						type: "turn-finished",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						toolCallCount: 0,
					});
					const completionReminderMessages =
						this.getCompletionReminderMessages();
					if (completionReminderMessages.length > 0) {
						for (const reminderMessage of completionReminderMessages) {
							await this.addUserReminderMessage(reminderMessage);
						}
						continue;
					}
					const result = this.finishRun("completed", finalAssistantMessage);
					await this.callAfterRunHooks(result);
					await this.emit({
						type: "run-finished",
						snapshot: this.snapshot(),
						result,
					});
					return result;
				}

				const toolMessages = await this.executeToolCalls(toolCalls);
				this.state.pendingToolCalls = [];
				for (const toolMessage of toolMessages) {
					this.state.messages.push(toolMessage);
					await this.emit({
						type: "message-added",
						snapshot: this.snapshot(),
						message: toolMessage,
					});
				}
				await this.flushPendingHookContexts();
				await this.emit({
					type: "turn-finished",
					snapshot: this.snapshot(),
					iteration: this.state.iteration,
					toolCallCount: toolCalls.length,
				});
				const terminalToolMessage = this.findCompletingToolMessage(
					toolCalls,
					toolMessages,
				);
				if (terminalToolMessage) {
					const result = this.finishRun(
						"completed",
						finalAssistantMessage,
						textFromToolMessage(terminalToolMessage) || undefined,
					);
					await this.callAfterRunHooks(result);
					await this.emit({
						type: "run-finished",
						snapshot: this.snapshot(),
						result,
					});
					return result;
				}
			}

			throw new Error(
				`Agent runtime exceeded maxIterations (${this.config.maxIterations})`,
			);
		} catch (error) {
			const normalized =
				error instanceof Error ? error : new Error(String(error));
			const isControlledStop = normalized instanceof ControlledStopError;
			const isAborted = this.abortController.signal.aborted || isControlledStop;
			const status = isAborted ? "aborted" : "failed";
			// 在下面覆盖 lastError 之前先读取：错误分类只在运行确实
			// 死于它所记录的那个提供商错误时才适用。
			const errorClass =
				normalized instanceof ContextWindowOverflowError
					? ("context_window_exceeded" as const)
					: normalized.message === this.state.lastError
						? this.state.lastErrorClass
						: undefined;
			// 同样的防护逻辑：只有运行死于那个被记录的错误本身时，
			// 模型层的遥测才覆盖了这次失败。
			const errorAlreadyReported =
				normalized.message === this.state.lastError &&
				this.state.lastErrorReported;
			this.state.status = status;
			this.state.lastError = normalized.message;
			this.state.lastErrorClass = errorClass;
			this.state.lastErrorReported = errorAlreadyReported;
			const lastAssistantMessage = this.findLastAssistantMessage();
			const result: AgentRunResult = {
				agentId: this.state.agentId,
				agentRole: this.state.agentRole,
				runId: this.state.runId ?? createUID("run"),
				status,
				iterations: this.state.iteration,
				outputText: textFromMessage(lastAssistantMessage),
				messages: cloneMessages(this.state.messages),
				usage: cloneUsage(this.state.usage),
				error: status === "failed" ? normalized : undefined,
			};
			this.config.logger?.log?.("Agent loop caught error", {
				severity: status === "failed" ? "error" : "warn",
				agentId: this.state.agentId,
				agentRole: this.state.agentRole,
				runId: result.runId,
				status,
				iteration: this.state.iteration,
				errorName: normalized.name,
				errorMessage: normalized.message,
				assistantContentPartCount: lastAssistantMessage?.content.length ?? 0,
			});
			await this.callAfterRunHooks(result);
			if (status === "failed") {
				await this.emit({
					type: "run-failed",
					snapshot: this.snapshot(),
					error: normalized,
					errorClass,
				});
			} else {
				await this.emit({
					type: "run-finished",
					snapshot: this.snapshot(),
					result,
				});
			}
			return result;
		} finally {
			this.abortController = undefined;
		}
	}

	/**
	 * 把收集到的 hook 上下文块作为一条用户消息注入到对话末尾。
	 * 一定会送达：之后缓冲区即为空。
	 */
	private async flushPendingHookContexts(): Promise<void> {
		if (this.pendingHookContexts.length === 0) {
			return;
		}
		const hookContextText = this.pendingHookContexts.join("\n\n");
		this.pendingHookContexts = [];
		// displayRole "system" 让注入的块不出现在面向用户的对话记录中
		// （实时与回放皆然），同时它仍能到达模型，与压缩摘要的处理方式一致。
		const hookContextMessage = createMessage(
			"user",
			[{ type: "text", text: hookContextText }],
			{ userRunSpan: 0, displayRole: "system" },
		);
		// 绝不插入到助手的 tool_use 与其 tool_result 之间：被恢复的会话
		// 可能以一条未解决的尾部工具调用作为种子，而现在这个间隙里放
		// 用户消息会破坏提供商的配对规则。改为把上下文放在那次调用
		// 之前——推迟注入只有在模型恰好紧接着又调用工具时才会送达，
		// 而且下一次运行开始时的缓冲区重置会把它丢掉。
		const lastMessage = this.state.messages.at(-1);
		const trailingToolCall =
			lastMessage?.role === "assistant" &&
			lastMessage.content.some((part) => part.type === "tool-call");
		if (trailingToolCall) {
			this.state.messages.splice(-1, 0, hookContextMessage);
		} else {
			this.state.messages.push(hookContextMessage);
		}
		await this.emit({
			type: "message-added",
			snapshot: this.snapshot(),
			message: hookContextMessage,
		});
	}

	private async callBeforeRunHooks(): Promise<void> {
		for (const hook of this.hooks.beforeRun) {
			const result = await hook({
				snapshot: this.snapshot(),
			});
			this.applyStopControl(result);
			// 在这里收集，在运行的输入消息入列之后注入，
			// 让该块与用户提示落在同一个轮次里。
			if (result?.appendContext?.trim()) {
				this.pendingHookContexts.push(
					formatHookContextBlock({ source: "RunStart" }, result.appendContext),
				);
			}
		}
	}

	private async callAfterRunHooks(result: AgentRunResult): Promise<void> {
		for (const hook of this.hooks.afterRun) {
			await hook({ snapshot: this.snapshot(), result });
		}
	}

	/**
	 * 从「在模型输出 token 上限处结束、且没有产生可用工具调用」的轮次中
	 * 恢复：提示模型更简洁，并让调用方重试，最多连续
	 * MAX_TOKENS_RECOVERY_LIMIT 次。一旦达到上限便返回 false，
	 * 让运行失败而不是无限循环。
	 */
	private async recoverFromIncompleteMaxTokensTurn(): Promise<boolean> {
		if (this.maxTokensRecoveryCount >= MAX_TOKENS_RECOVERY_LIMIT) {
			return false;
		}
		this.maxTokensRecoveryCount += 1;
		await this.emit({
			type: "status-notice",
			snapshot: this.snapshot(),
			message: `output-token limit reached before a tool call — nudging for a more concise response (attempt ${this.maxTokensRecoveryCount}/${MAX_TOKENS_RECOVERY_LIMIT})`,
			metadata: {
				kind: "max_tokens_recovery",
				reason: "max_tokens_recovery",
				phase: "started",
				iteration: this.state.iteration,
				attempt: this.maxTokensRecoveryCount,
				maxRetries: MAX_TOKENS_RECOVERY_LIMIT,
			},
		});
		await this.addUserReminderMessage(MAX_TOKENS_RECOVERY_NUDGE);
		return true;
	}

	/**
	 * 运行一个模型轮次，对瞬时的提供商/API 失败做带退避的重试。
	 *
	 * 如果某个轮次的模型流死于「可重试的提供商错误」（速率限制、5xx、
	 * 网络抖动，或 OpenRouter 通用的 "Provider returned error"），
	 * 会在错误被放行、终止整次运行之前重新发出请求，最多
	 * {@link PROVIDER_ERROR_MAX_RETRIES} 次，尝试之间采用指数退避。
	 * 不可重试的错误（认证、上下文窗口溢出、其他客户端错误）以及
	 * 任何已经产生可见输出或提供商工具活动的尝试，都会原样返回给
	 * 调用方处理，所以这里只增加韧性、绝不改变本来就会成功的轮次的行为。
	 * 上下文窗口溢出恢复与 max-tokens 恢复仍在每次尝试内部运行
	 * （后者每次运行至多一次）。
	 */
	private async generateAssistantMessageWithProviderRetry(): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
		interrupted?: boolean;
	}> {
		return await this.withProviderErrorRetry(() =>
			this.generateAssistantMessageWithOverflowRecovery(),
		);
	}

	/**
	 * 运行 `issue`；当它返回的轮次死于「瞬时可重试的提供商错误」时
	 * （见 {@link isRetryableProviderErrorTurn}）带退避地重新运行，
	 * 最多 PROVIDER_ERROR_MAX_RETRIES 次。`issue` 决定重跑什么：
	 * 普通请求重跑整个已准备的轮次，或只重发一个已经准备好的请求。
	 */
	private async withProviderErrorRetry<
		T extends { message: AgentMessage; finishReason: AgentModelFinishReason },
	>(issue: () => Promise<T>): Promise<T> {
		let attempt = 0;
		for (;;) {
			const turn = await issue();
			if (
				attempt >= PROVIDER_ERROR_MAX_RETRIES ||
				!this.isRetryableProviderErrorTurn(turn)
			) {
				return turn;
			}
			attempt += 1;
			const providerError = this.state.lastError;
			// 失败尝试的错误已被上面的通知捕获；清空它，
			// 让下一次尝试的 finish 事件被独立评判。
			this.resetLastError();
			const delayMs = Math.min(
				PROVIDER_ERROR_RETRY_BASE_DELAY_MS * 2 ** (attempt - 1),
				PROVIDER_ERROR_RETRY_MAX_DELAY_MS,
			);
			await this.emit({
				type: "status-notice",
				snapshot: this.snapshot(),
				message: `provider error — retrying (attempt ${attempt}/${PROVIDER_ERROR_MAX_RETRIES})`,
				metadata: {
					kind: "provider_error_retry",
					reason: "provider_error_retry",
					phase: "started",
					iteration: this.state.iteration,
					attempt,
					maxRetries: PROVIDER_ERROR_MAX_RETRIES,
					delayMs,
					providerError,
				},
			});
			await this.abortableDelay(delayMs);
		}
	}

	/**
	 * 当轮次死于「重试有可能恢复的瞬时提供商错误」，且失败尝试没有留下
	 * 任何会被第二次流式输出重复/重放的东西时为 true：
	 * - 完全无内容（文本、推理、媒体或本地工具调用）：那些增量已经
	 *   发给了 UI，且没有机制撤回，重新流式输出会把结果展示两次；
	 * - 无提供商侧执行的工具活动（记录在消息 metadata 而非 content 中）：
	 *   重新发请求可能把这些副作用再执行一次；
	 * - 不是认证或上下文窗口失败，这些同样的请求修不好。
	 */
	private isRetryableProviderErrorTurn(turn: {
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
	}): boolean {
		if (turn.finishReason !== "error") {
			return false;
		}
		if (turn.message.content.length > 0) {
			return false;
		}
		const modelToolActivities = turn.message.metadata?.modelToolActivities;
		if (Array.isArray(modelToolActivities) && modelToolActivities.length > 0) {
			return false;
		}
		const errorClass = this.state.lastErrorClass;
		if (errorClass === "auth" || errorClass === "context_window_exceeded") {
			return false;
		}
		// 可用时取自模型边界的类型化 `isRetryable` 标志，
		// 否则在 finish 处理器中根据展平后的消息进行分类。
		return this.state.lastErrorRetryable === true;
	}

	/**
	 * 清空 last-error 系列字段。在每个轮次开始时、以及每次提供商错误
	 * 重试之前调用，这样省略 `error` 的 `finish` 事件（公开的
	 * AgentModel 契约允许）就不会继承早先尝试的错误分类或可重试性。
	 * 刻意不在溢出恢复内部调用——它的「无可压缩内容」错误需要报告
	 * 第一次尝试的提供商消息。
	 */
	private resetLastError(): void {
		this.state.lastError = undefined;
		this.state.lastErrorClass = undefined;
		this.state.lastErrorRetryable = undefined;
		this.state.lastErrorReported = false;
	}

	/**
	 * 休眠 `ms` 毫秒；若等待期间运行被中止，则提前以中止错误 reject，
	 * 让重试退避永远不会阻塞取消。
	 */
	private async abortableDelay(ms: number): Promise<void> {
		this.throwIfAborted();
		const signal = this.abortController?.signal;
		await new Promise<void>((resolve, reject) => {
			const onAbort = () => {
				clearTimeout(timer);
				reject(this.normalizeAbortError());
			};
			const timer = setTimeout(() => {
				signal?.removeEventListener("abort", onAbort);
				resolve();
			}, ms);
			if (signal) {
				signal.addEventListener("abort", onAbort, { once: true });
			}
		});
	}

	/**
	 * 运行一个模型轮次，对两种情形各做一次（每次运行）恢复：
	 * 提供商拒绝的上下文窗口溢出，以及「无工具调用的轮次在输出 token
	 * 上限处被截断」。两种恢复都会通过 `prepareTurn` 强制压缩并重试请求。
	 * 终止性（不可恢复的）溢出状态会抛出带可操作建议的消息而不是
	 * 原始的提供商错误；不可恢复的截断轮次会原样返回，让循环在保留
	 * 部分内容的同时呈现 max-tokens 错误。
	 */
	private async generateAssistantMessageWithOverflowRecovery(): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
		interrupted?: boolean;
	}> {
		const first = await this.generateAssistantMessage();
		if (this.isRecoverableMaxTokensTurn(first)) {
			return await this.retryTruncatedTurnWithCompaction(first);
		}
		if (!this.isRecoverableOverflowTurn(first)) {
			return first;
		}
		this.overflowRecoveryAttempted = true;
		const providerError = this.state.lastError;
		if (!this.config.prepareTurn) {
			throw new ContextWindowOverflowError(
				CONTEXT_WINDOW_OVERFLOW_NO_RECOVERY_MESSAGE,
				providerError,
			);
		}
		await this.emit({
			type: "status-notice",
			snapshot: this.snapshot(),
			message: "context window exceeded — compacting and retrying",
			metadata: {
				kind: "context_overflow_recovery",
				reason: "context_overflow_recovery",
				phase: "started",
				iteration: this.state.iteration,
				providerError,
			},
		});
		const retry = await this.generateAssistantMessage({
			overflowRecovery: true,
		});
		if (
			retry.finishReason === "error" &&
			this.state.lastErrorClass === "context_window_exceeded"
		) {
			throw new ContextWindowOverflowError(
				CONTEXT_WINDOW_OVERFLOW_RECOVERY_FAILED_MESSAGE,
				this.state.lastError,
			);
		}
		return retry;
	}

	private isRecoverableOverflowTurn(turn: {
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
	}): boolean {
		if (
			turn.finishReason !== "error" ||
			this.state.lastErrorClass !== "context_window_exceeded" ||
			this.overflowRecoveryAttempted
		) {
			return false;
		}
		// 出错但仍产生了工具调用的流会走正常循环（与既有行为一致）；
		// 重试会丢弃那部分已完成的工作。
		return !turn.message.content.some((part) => part.type === "tool-call");
	}

	private isRecoverableMaxTokensTurn(turn: {
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
	}): boolean {
		if (
			turn.finishReason !== "max-tokens" ||
			this.maxTokensRecoveryAttempted ||
			!this.config.prepareTurn
		) {
			return false;
		}
		// 产生工具调用的截断轮次会走正常循环去执行它们；
		// 只有纯文本的截断才是终止性的、值得做恢复尝试。
		if (turn.message.content.some((part) => part.type === "tool-call")) {
			return false;
		}
		// 提供商侧执行的工具活动存放在 metadata 而非 content，且已经发生——
		// 重放该轮次会重复其副作用。
		return !this.hasModelToolActivity(turn.message);
	}

	/** 提供商侧执行的工具活动记录在 metadata 中，而非 content。 */
	private hasModelToolActivity(message: AgentMessage): boolean {
		const activities = message.metadata?.modelToolActivities;
		return Array.isArray(activities) && activities.length > 0;
	}

	/**
	 * 在输出 token 上限处被截断的响应，往往是上下文接近占满的症状：
	 * 本地 OpenAI 兼容服务器（llama.cpp、ollama、LM Studio）会把生成长度
	 * 限制在剩余上下文之内，而不管请求的输出预算。压缩对话能腾出这块
	 * 空间，所以一次强制压缩 + 重试可以救回这些轮次。当压缩无可删除时
	 * 返回原始轮次，让循环在部分内容已持久化的前提下呈现 max-tokens 错误。
	 *
	 * 本方法运行时，被截断的轮次尚未被持久化（循环只在轮次返回后才记录
	 * 它），因此每当这次尝试没有换来替代轮次——压缩抛异常、重试被中止、
	 * 或重试出错且没有可供循环执行的内容——都会先记录被截断的轮次，
	 * 再浮现失败；部分答案绝不会因这次恢复尝试而丢失。
	 *
	 * 真正返回的重试会不加评判地交给循环：运行循环仍是决定轮次是否可接受
	 * 的唯一地方。当压缩帮不上忙——无可删除内容、重试再次截断、或该轮次
	 * 本来就不符合条件——循环自身的「提示并重试」恢复会接管，所以这里
	 * 最先运行且每次运行至多一次。这里的遥测纯粹是观察性的——先
	 * `started`，然后尝试执行时以重试的 finish reason 记 `retried`，
	 * 无法执行时记 `failed`——因此它从不断言运行结果，也就不会与之矛盾。
	 */
	private async retryTruncatedTurnWithCompaction(first: {
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
	}): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
	}> {
		this.maxTokensRecoveryAttempted = true;
		// 与循环的「提示并重试」通知（`max_tokens_recovery`）相区分，
		// 让两种策略在下游可以分辨。
		const noticeMetadata = {
			kind: "max_tokens_compaction",
			reason: "max_tokens_compaction",
			iteration: this.state.iteration,
		};
		this.captureTaskLifecycle(TASK_MAX_TOKENS_RECOVERY_EVENT, {
			phase: "started",
		});
		let retry: { message: AgentMessage; finishReason: AgentModelFinishReason };
		try {
			// 在 try 内部发出，让抛异常的监听者也会流入
			// 下面终止阶段的 catch。
			await this.emit({
				type: "status-notice",
				snapshot: this.snapshot(),
				message:
					"response hit the output token limit — compacting and retrying",
				metadata: { ...noticeMetadata, phase: "started" },
			});
			// 只准备（压缩）一次；压缩后的请求遇到瞬时提供商错误时
			// 与其他请求采用同样的有界重试策略，但重发的是同一个已准备
			// 请求——转录在 429 与重试之间没有变化，重新压缩只会重复
			// 压缩的通知、遥测与任何摘要调用。
			const prepared = await this.prepareModelRequest({
				overflowRecovery: true,
			});
			retry = await this.withProviderErrorRetry(() =>
				this.issuePreparedRequest(prepared),
			);
		} catch (error) {
			if (error instanceof ContextWindowOverflowError) {
				// 无可压缩内容——保留截断的轮次，让循环呈现
				// max-tokens 错误而不是溢出错误。
				this.captureTaskLifecycle(TASK_MAX_TOKENS_RECOVERY_EVENT, {
					phase: "failed",
					eventType: "nothing_to_compact",
				});
				await this.emit({
					type: "status-notice",
					snapshot: this.snapshot(),
					message: "output-token-limit recovery failed: nothing to compact",
					metadata: { ...noticeMetadata, phase: "failed" },
				});
				return first;
			}
			// 重新抛出前先关闭恢复的遥测，让每个 started 阶段都有终止阶段；
			// 抛出的错误本身由运行自身的失败路径呈现，所以这里不发通知。
			// 错误随遥测一起带上，让 `error_type` 能把蓄意停止（ControlledStopError、
			// AgentRuntimeAbortError）与真正的恢复失败区分开，无需重述
			// 运行循环对两者的分类方式。
			this.captureTaskLifecycle(TASK_MAX_TOKENS_RECOVERY_EVENT, {
				phase: "failed",
				eventType: "recovery_threw",
				error,
			});
			await this.recordAssistantMessage(first.message, first.finishReason);
			throw error;
		}
		// `retried` 只表明压缩与重试执行了——它不对整次运行下结论，
		// 运行仍可能拒绝这个轮次。重试的 finish reason 作为观察到的事实
		// 一并带上；将它和运行结果配对即可知道该轮次的最终去向。
		this.captureTaskLifecycle(TASK_MAX_TOKENS_RECOVERY_EVENT, {
			phase: "retried",
			eventType: retry.finishReason,
		});
		// 没有返回替代轮次：保留截断的那次并呈现终止重试的原因，
		// 与循环处理该 finish 的方式完全一致。
		if (retry.finishReason === "aborted") {
			await this.recordAssistantMessage(first.message, first.finishReason);
			throw this.normalizeAbortError();
		}
		if (
			retry.finishReason === "error" &&
			!retry.message.content.some((part) => part.type === "tool-call")
		) {
			await this.recordAssistantMessage(first.message, first.finishReason);
			// 出错但仍产生了输出——文本，或已执行的提供商侧工具——
			// 的重试是可观察的工作，而不是可以丢弃的草稿：把它与截断轮次
			// 一起保留，让转录展示实际发生了什么（循环在失败前也会记录
			// 这样的轮次）。只有什么都没产生的重试才会被丢弃。
			if (
				retry.message.content.length > 0 ||
				this.hasModelToolActivity(retry.message)
			) {
				await this.recordAssistantMessage(retry.message, retry.finishReason);
			}
			throw new Error(this.state.lastError ?? "Model stream failed");
		}
		// 什么都没返回的重试也不算替代品：把它交出去会触发循环的
		// 空响应防护，在记录任何东西之前就抛出——既丢弃截断的答案，
		// 又报了误导性的错误。保留截断轮次，与无可压缩内容时完全一样，
		// 让循环呈现 max-tokens 错误。
		if (
			retry.message.content.length === 0 &&
			!this.hasModelToolActivity(retry.message)
		) {
			return first;
		}
		return retry;
	}

	/** 把助手轮次追加到转录中并对外通告。 */
	private async recordAssistantMessage(
		message: AgentMessage,
		finishReason: AgentModelFinishReason,
	): Promise<void> {
		this.state.messages.push(message);
		await this.emit({
			type: "message-added",
			snapshot: this.snapshot(),
			message,
		});
		await this.emit({
			type: "assistant-message",
			snapshot: this.snapshot(),
			iteration: this.state.iteration,
			message,
			finishReason,
		});
	}

	private async generateAssistantMessage(options?: {
		overflowRecovery?: boolean;
	}): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
		interrupted?: boolean;
	}> {
		const controller = new AbortController();
		this.modelSteerController = controller;
		try {
			return await this.generateAssistantMessageForRequest(controller, options);
		} finally {
			this.modelSteerController = undefined;
		}
	}

	/**
	 * 把已准备好的请求作为一次全新尝试发出：轮次准备（压缩、
	 * before-model hook）不会重跑，但该尝试会获得自己的转向（steer）
	 * 控制器与生命周期计时。
	 */
	private async issuePreparedRequest(prepared: PreparedModelRequest): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
		interrupted?: boolean;
	}> {
		const controller = new AbortController();
		this.modelSteerController = controller;
		try {
			return await this.streamPreparedRequest(
				{ ...prepared, startedAt: Date.now() },
				controller,
			);
		} finally {
			this.modelSteerController = undefined;
		}
	}

	/** 已经应用了轮次准备与 before-model hook 的模型请求。 */
	private async prepareModelRequest(options?: {
		overflowRecovery?: boolean;
	}): Promise<PreparedModelRequest> {
		const modelRequestMetadata = omitUndefinedValues({
			distinctId: trimNonEmpty(this.config.distinctId),
			clientName: trimNonEmpty(this.config.clientName),
			clientVersion: trimNonEmpty(this.config.clientVersion),
			clineCoreVersion: trimNonEmpty(this.config.clineCoreVersion),
			sessionId: trimNonEmpty(this.config.sessionId),
			agentId: this.state.agentId,
			conversationId: trimNonEmpty(this.config.conversationId),
			runId: this.state.runId,
			iteration: this.state.iteration,
		});
		let request: AgentModelRequest = {
			systemPrompt: this.config.systemPrompt,
			messages: cloneMessages(this.state.messages),
			tools: [...this.tools.values()].map<AgentToolDefinition>((tool) => ({
				name: tool.name,
				description: tool.description,
				inputSchema: tool.inputSchema,
			})),
			modelTools: this.config.modelTools,
			signal: this.abortController?.signal,
			options: mergeModelOptions(this.config.modelOptions, {
				metadata: modelRequestMetadata,
			}),
		};

		const startedAt = Date.now();

		if (this.state.iteration > 1) {
			const pendingUserMessage = await this.consumePendingUserMessage();
			if (pendingUserMessage) {
				request = {
					...request,
					messages: [
						...request.messages,
						...cloneMessages([pendingUserMessage]),
					],
				};
			}
		}

		request = await this.prepareTurnForModelRequest(request, options);
		this.throwIfAborted();

		for (const hook of this.hooks.beforeModel) {
			const result = (await hook({
				snapshot: this.snapshot(),
				request,
			})) as AgentBeforeModelResult | undefined;
			this.throwIfAborted();
			this.applyStopControl(result);
			if (result?.messages) {
				request = { ...request, messages: cloneMessages(result.messages) };
			}
			if (result?.tools) {
				request = { ...request, tools: [...result.tools] };
			}
			if (result?.options) {
				request = {
					...request,
					options: mergeModelOptions(request.options, result.options),
				};
			}
		}

		this.config.logger?.debug("Agent model request diagnostics", {
			iteration: this.state.iteration,
			providerId:
				"providerId" in this.config &&
				typeof this.config.providerId === "string"
					? this.config.providerId
					: undefined,
			modelId:
				"modelId" in this.config && typeof this.config.modelId === "string"
					? this.config.modelId
					: undefined,
			...summarizeModelRequest(request),
		});

		return { request, startedAt };
	}

	private async generateAssistantMessageForRequest(
		steerController: AbortController,
		options?: {
			overflowRecovery?: boolean;
		},
	): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
		interrupted?: boolean;
	}> {
		const prepared = await this.prepareModelRequest(options);
		return await this.streamPreparedRequest(prepared, steerController);
	}

	/**
	 * 发出已准备好的请求，并从其流式输出组装助手轮次。
	 * 与准备阶段分离，使已准备的请求可以重发（例如在瞬时提供商错误之后）
	 * 而无需重跑轮次准备。
	 */
	private async streamPreparedRequest(
		prepared: PreparedModelRequest,
		steerController: AbortController,
	): Promise<{
		message: AgentMessage;
		finishReason: AgentModelFinishReason;
		interrupted?: boolean;
	}> {
		const usageBeforeModel = cloneUsage(this.state.usage);
		const getTaskLifecycleDurationMs = () => Date.now() - prepared.startedAt;
		let request = prepared.request;

		this.throwIfAborted();
		this.captureTaskLifecycle(TASK_PROVIDER_REQUEST_STARTED_EVENT, {
			durationMs: getTaskLifecycleDurationMs(),
			phase: "provider_request_started",
		});
		// 转向（steering）取消提供商生成，而请求准备阶段保留运行级
		// 信号，让压缩与 hook 能一致地完成。
		request = {
			...request,
			signal: AbortSignal.any([
				steerController.signal,
				...(this.abortController ? [this.abortController.signal] : []),
			]),
		};
		const stream = this.openTaskLifecycleStream(
			request,
			getTaskLifecycleDurationMs,
		);

		const content: AgentMessagePart[] = [];
		const toolAssemblies = new Map<string, PendingToolAssembly>();
		const modelToolActivities = new Map<string, AgentModelToolActivity>();
		const invalidToolCalls: InvalidToolCall[] = [];
		const sequence: Array<
			{ type: "tool"; key: string } | { type: "part"; part: AgentMessagePart }
		> = [];
		let nextToolIndex = 0;
		let finishReason: AgentModelFinishReason = "stop";
		let requestId: string | undefined;
		let accumulatedText = "";
		let accumulatedReasoning = "";

		for await (const event of stream) {
			if (steerController.signal.aborted) break;
			this.throwIfAborted();
			switch (event.type) {
				case "text-delta": {
					accumulatedText += event.text;
					const last = sequence.at(-1);
					if (last?.type === "part" && last.part.type === "text") {
						last.part.text += event.text;
					} else {
						sequence.push({
							type: "part",
							part: { type: "text", text: event.text },
						});
					}
					await this.emit({
						type: "assistant-text-delta",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						text: event.text,
						accumulatedText,
					});
					break;
				}
				case "media": {
					sequence.push({
						type: "part",
						part: {
							type: "media",
							media: event.media,
						},
					});
					await this.emit({
						type: "assistant-media",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						media: event.media,
					});
					break;
				}
				case "reasoning-delta": {
					accumulatedReasoning += event.text;
					const last = sequence.at(-1);
					if (last?.type === "part" && last.part.type === "reasoning") {
						last.part.text += event.text;
						last.part.redacted = event.redacted ?? last.part.redacted;
						last.part.metadata = event.metadata ?? last.part.metadata;
					} else {
						sequence.push({
							type: "part",
							part: {
								type: "reasoning",
								text: event.text,
								redacted: event.redacted,
								metadata: event.metadata,
							},
						});
					}
					await this.emit({
						type: "assistant-reasoning-delta",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						text: event.text,
						accumulatedText: accumulatedReasoning,
						redacted: event.redacted,
						metadata: event.metadata,
					});
					break;
				}
				case "tool-call-delta": {
					if (event.execution) {
						const toolCall: AgentToolCallPart = {
							type: "tool-call",
							toolCallId: event.toolCallId ?? createUID("model_tool"),
							toolName: event.toolName ?? "tool",
							input: event.input,
							metadata: event.metadata,
							execution: event.execution,
						};
						modelToolActivities.set(toolCall.toolCallId, {
							toolCallId: toolCall.toolCallId,
							toolName: toolCall.toolName,
							execution: event.execution,
							input: toolCall.input,
						});
						await this.emit({
							type: "tool-started",
							snapshot: this.snapshot(),
							iteration: this.state.iteration,
							toolCall,
						});
						break;
					}
					const key =
						event.toolCallId ?? `tool_${event.index ?? nextToolIndex}`;
					if (event.index == null && event.toolCallId == null) {
						nextToolIndex += 1;
					}
					let assembly = toolAssemblies.get(key);
					if (!assembly) {
						assembly = {
							toolCallId: event.toolCallId ?? createUID("tool"),
							inputText: "",
						};
						toolAssemblies.set(key, assembly);
						sequence.push({ type: "tool", key });
					}
					if (event.toolCallId) {
						assembly.toolCallId = event.toolCallId;
					}
					if (event.toolName) {
						assembly.toolName = event.toolName;
					}
					if (event.input !== undefined) {
						assembly.inputValue = event.input;
					}
					if (event.metadata !== undefined) {
						assembly.metadata = mergeToolMetadata(
							assembly.metadata,
							event.metadata,
						);
					}
					if (event.inputText) {
						assembly.inputText = mergeToolInputText(
							assembly.inputText,
							event.inputText,
						);
					}
					break;
				}
				case "tool-result": {
					const existing = modelToolActivities.get(event.toolCallId);
					const activity = {
						...existing,
						toolCallId: event.toolCallId,
						toolName: event.toolName,
						execution: event.execution,
						input: event.input === undefined ? existing?.input : event.input,
						output: event.output,
						isError: event.isError,
					};
					modelToolActivities.set(event.toolCallId, activity);
					const toolCall: AgentToolCallPart = {
						type: "tool-call",
						toolCallId: event.toolCallId,
						toolName: event.toolName,
						input: activity.input,
						execution: event.execution,
					};
					await this.emit({
						type: "tool-finished",
						snapshot: this.snapshot(),
						iteration: this.state.iteration,
						toolCall,
						message: createMessage("tool", [
							{
								type: "tool-result",
								toolCallId: event.toolCallId,
								toolName: event.toolName,
								output: event.output,
								isError: event.isError,
								execution: event.execution,
							},
						]),
					});
					break;
				}
				case "usage": {
					// 记录本请求中提供商自己上报的输入 token 数，让
					// prepare-turn 管线能基于真实用量而不是按字符估算
					// 来触发压缩。
					if (
						typeof event.usage.inputTokens === "number" &&
						event.usage.inputTokens > 0
					) {
						this.state.lastRequestInputTokens = event.usage.inputTokens;
					}
					await this.updateUsage(event.usage);
					break;
				}
				case "finish": {
					finishReason = event.reason;
					requestId = event.requestId;
					if (event.error) {
						this.state.lastError = event.error;
						// 在自身错误边界处分类的模型（此时原始的提供商错误
						// 仍是结构化的）优先。其他情况——自定义 `AgentModel`
						// 实现、只携带展平消息的适配器——根据消息来分类，
						// 使其仍具备溢出恢复资格。
						this.state.lastErrorClass =
							event.errorClass ?? classifyProviderError(event.error);
						// 优先使用边界的类型化 `isRetryable` 信号；对不携带
						// 该信号的模型，回退到根据展平消息分类。
						this.state.lastErrorRetryable =
							event.errorRetryable ?? isRetryableProviderError(event.error);
						this.state.lastErrorReported = event.errorReported === true;
					}
					break;
				}
			}
		}
		this.throwIfAborted();
		const interrupted = steerController.signal.aborted;
		if (interrupted) finishReason = "stop";

		for (const item of sequence) {
			// 被取消的流可能包含不完整的工具 JSON 或未签名的推理。
			// 只保留该响应中可重放的可见内容。
			if (
				interrupted &&
				(item.type === "tool" || item.part.type === "reasoning")
			)
				continue;
			if (item.type === "part") {
				content.push(item.part);
				continue;
			}
			const assembly = toolAssemblies.get(item.key);
			if (!assembly?.toolName) {
				invalidToolCalls.push({
					toolCallId: assembly?.toolCallId ?? item.key,
					input: buildInvalidToolInput(assembly?.inputText ?? ""),
					reason: "missing_name",
				});
				continue;
			}
			const parsed = parseToolInput(assembly);
			if (parsed.reason) {
				invalidToolCalls.push({
					toolCallId: assembly.toolCallId,
					toolName: assembly.toolName,
					input: parsed.invalidInput,
					reason: parsed.reason,
				});
			}
			content.push({
				type: "tool-call",
				toolCallId: assembly.toolCallId,
				toolName: assembly.toolName,
				input: parsed.input,
				metadata: parsed.parseError
					? mergeToolMetadata(assembly.metadata, {
							inputParseError: parsed.parseError,
							rawInputText: assembly.inputText,
						})
					: assembly.metadata,
			});
		}

		const messageMetadata: Record<string, unknown> = {};
		if (invalidToolCalls.length > 0) {
			messageMetadata.invalidToolCalls = invalidToolCalls;
		}
		if (modelToolActivities.size > 0) {
			messageMetadata.modelToolActivities = [...modelToolActivities.values()];
		}
		const message = createMessage(
			"assistant",
			content,
			Object.keys(messageMetadata).length > 0 ? messageMetadata : undefined,
		);
		const metrics = usageDelta(usageBeforeModel, this.state.usage);
		if (metrics) {
			message.metrics = metrics;
			this.captureUnexpectedReasoningTokens(request, metrics);
		}
		if (this.config.messageModelInfo) {
			message.modelInfo = { ...this.config.messageModelInfo };
		}
		for (const hook of this.hooks.afterModel) {
			const control = (await hook({
				snapshot: this.snapshot(),
				assistantMessage: message,
				finishReason,
				...(requestId ? { requestId } : {}),
			})) as AgentStopControl | undefined;
			this.applyStopControl(control);
		}

		return { message, finishReason, interrupted };
	}

	private async *openTaskLifecycleStream(
		request: AgentModelRequest,
		getTaskLifecycleDurationMs: () => number | undefined,
	): AsyncIterable<AgentModelEvent> {
		let stream: AsyncIterable<AgentModelEvent>;
		let phase = "provider_request_started";
		try {
			stream = await this.config.model.stream(request);
			this.throwIfAborted();
			phase = "provider_stream_started";
			this.captureTaskLifecycle(TASK_PROVIDER_STREAM_STARTED_EVENT, {
				durationMs: getTaskLifecycleDurationMs(),
				phase,
			});
		} catch (error) {
			if (request.signal?.aborted && !this.abortController?.signal.aborted)
				return;
			if (!request.signal?.aborted && !this.isAbortError(error)) {
				this.captureTaskLifecycleFailure(
					error,
					phase,
					getTaskLifecycleDurationMs(),
				);
			}
			throw error;
		}

		let receivedFirstChunk = false;
		try {
			for await (const event of stream) {
				if (!receivedFirstChunk) {
					receivedFirstChunk = true;
					phase = "first_chunk_received";
					this.captureTaskLifecycle(TASK_FIRST_CHUNK_RECEIVED_EVENT, {
						durationMs: getTaskLifecycleDurationMs(),
						phase,
						eventType: event.type,
					});
				}
				yield event;
			}
		} catch (error) {
			if (request.signal?.aborted && !this.abortController?.signal.aborted)
				return;
			if (!request.signal?.aborted && !this.isAbortError(error)) {
				this.captureTaskLifecycleFailure(
					error,
					phase,
					getTaskLifecycleDurationMs(),
				);
			}
			throw error;
		}
	}

	private captureTaskLifecycleFailure(
		error: unknown,
		phase: string,
		durationMs: number | undefined,
	): void {
		this.captureTaskLifecycle(TASK_PROVIDER_STREAM_FAILED_EVENT, {
			durationMs,
			error,
			errorClass: classifyProviderError(error),
			phase,
		});
	}

	private captureTaskLifecycle(
		event: string,
		input: Partial<Omit<CaptureTaskLifecycleEventInput, "event">> = {},
	): void {
		const sessionId = trimNonEmpty(this.config.sessionId);
		captureTaskLifecycleEvent(this.config.telemetry, {
			event,
			sessionId,
			ulid: sessionId,
			agentId: this.state.agentId,
			conversationId: trimNonEmpty(this.config.conversationId),
			runId: this.state.runId,
			iteration: this.state.iteration > 0 ? this.state.iteration : undefined,
			providerId: this.getTelemetryProviderId(),
			modelId: this.getTelemetryModelId(),
			...input,
		});
	}

	private getTelemetryProviderId(): string | undefined {
		return (
			trimNonEmpty(this.config.messageModelInfo?.provider) ??
			this.telemetryProviderId
		);
	}

	private getTelemetryModelId(): string | undefined {
		return (
			trimNonEmpty(this.config.messageModelInfo?.id) ?? this.telemetryModelId
		);
	}

	private isAbortError(error: unknown): boolean {
		return (
			error instanceof AgentRuntimeAbortError ||
			this.abortController?.signal.aborted === true
		);
	}

	private captureUnexpectedReasoningTokens(
		request: AgentModelRequest,
		metrics: NonNullable<AgentMessage["metrics"]>,
	): void {
		if (
			!reasoningWasRequestedOff(request) ||
			(metrics.reasoningTokenCount ?? 0) <= 0
		) {
			return;
		}
		const reasoningTokenCount = metrics.reasoningTokenCount;
		if (reasoningTokenCount === undefined) {
			return;
		}

		captureAgentUnexpectedReasoningTokens(this.config.telemetry, {
			sessionId: this.config.sessionId,
			agentId: this.state.agentId,
			runId: this.state.runId,
			iteration: this.state.iteration,
			providerId: this.config.messageModelInfo?.provider,
			modelId: this.config.messageModelInfo?.id,
			requestedThinking: false,
			reasoningTokenCount,
		});
	}

	private async prepareTurnForModelRequest(
		request: AgentModelRequest,
		options?: { overflowRecovery?: boolean },
	): Promise<AgentModelRequest> {
		if (!this.config.prepareTurn) {
			return request;
		}

		const overflowRecovery = options?.overflowRecovery === true;
		const result = await this.config.prepareTurn({
			agentId: this.state.agentId,
			conversationId: this.config.conversationId,
			parentAgentId: this.state.parentAgentId ?? null,
			iteration: this.state.iteration,
			messages: request.messages,
			systemPrompt: request.systemPrompt,
			tools: request.tools,
			model: {
				id: this.config.messageModelInfo?.id,
				provider: this.config.messageModelInfo?.provider,
			},
			signal: request.signal,
			overflowRecovery: overflowRecovery || undefined,
			previousRequestInputTokens:
				this.state.lastRequestInputTokens > 0
					? this.state.lastRequestInputTokens
					: undefined,
			emitStatusNotice: (message, metadata) => {
				void this.emit({
					type: "status-notice",
					snapshot: this.snapshot(),
					message,
					metadata,
				});
			},
		});
		if (overflowRecovery) {
			// 只用一个确实更小的请求去重试被提供商拒绝的溢出——任何
			// 其他情况都注定再次失败。
			//
			// 序列化长度只是 token 的粗略代理，但这正是这道防线所需的：
			// 它回答「到底有没有删掉任何东西」，而且对任意的 `prepareTurn`
			// 实现都成立；共享估算器本身也与字符数线性相关，换算单位
			// 不会改变结论。权威的 token 预算（对照模型上限）发生在
			// 压缩管线内部。
			// TODO: 让 `prepareTurn` 上报它已经计算过的 token 估算
			// （前后对比），使这里能用真实数字做决策，而无需重新推导代理值。
			const shrunk =
				result?.messages !== undefined &&
				JSON.stringify(result.messages).length <
					JSON.stringify(request.messages).length;
			if (!shrunk) {
				throw new ContextWindowOverflowError(
					CONTEXT_WINDOW_OVERFLOW_NOTHING_TO_COMPACT_MESSAGE,
					this.state.lastError,
				);
			}
		}
		if (!result) {
			return request;
		}

		let next = request;
		if (result.messages) {
			const preparedMessages = cloneMessages(result.messages);
			next = { ...next, messages: cloneMessages(preparedMessages) };
		}
		if (result.systemPrompt !== undefined) {
			next = { ...next, systemPrompt: result.systemPrompt };
		}
		return next;
	}

	private async consumePendingUserMessage(): Promise<AgentMessage | undefined> {
		const consumePendingUserMessage = this.config.consumePendingUserMessage;
		if (!consumePendingUserMessage) {
			return undefined;
		}
		const pending = (await consumePendingUserMessage())?.trim();
		if (!pending) {
			return undefined;
		}
		const message = createMessage("user", [{ type: "text", text: pending }], {
			userRunSpan: 0,
		});
		this.state.messages.push(message);
		await this.emit({
			type: "message-added",
			snapshot: this.snapshot(),
			message,
		});
		return message;
	}

	private async updateUsage(usage: Partial<AgentUsage>): Promise<void> {
		this.state.usage = {
			inputTokens: this.state.usage.inputTokens + (usage.inputTokens ?? 0),
			outputTokens: this.state.usage.outputTokens + (usage.outputTokens ?? 0),
			cacheReadTokens:
				this.state.usage.cacheReadTokens + (usage.cacheReadTokens ?? 0),
			cacheWriteTokens:
				this.state.usage.cacheWriteTokens + (usage.cacheWriteTokens ?? 0),
			reasoningTokenCount:
				(this.state.usage.reasoningTokenCount ?? 0) +
				(usage.reasoningTokenCount ?? 0),
			totalCost: (this.state.usage.totalCost ?? 0) + (usage.totalCost ?? 0),
		};
		await this.emit({
			type: "usage-updated",
			snapshot: this.snapshot(),
			usage: cloneUsage(this.state.usage),
		});
	}

	private async executeToolCalls(
		toolCalls: AgentToolCallPart[],
	): Promise<AgentMessage[]> {
		this.pendingHookContexts = [];
		const prepared: PreparedToolExecution[] = [];
		for (const toolCall of toolCalls) {
			prepared.push(await this.prepareToolExecution(toolCall));
		}

		const results: AgentMessage[] = [];
		for (let index = 0; index < prepared.length; ) {
			const execution = prepared[index];
			const mode = execution.tool?.executionMode ?? this.config.toolExecution;
			if (mode === "sequential") {
				results.push(await this.executePreparedTool(execution));
				index += 1;
				continue;
			}

			// 只有相邻的并行调用会重叠。普通的顺序工具必须等待它前面的
			// 组完成，并在下一组开始前结束。
			const start = index;
			while (
				index < prepared.length &&
				(prepared[index].tool?.executionMode ?? this.config.toolExecution) ===
					"parallel"
			) {
				index += 1;
			}
			results.push(
				...(await Promise.all(
					prepared
						.slice(start, index)
						.map((call) => this.executePreparedTool(call)),
				)),
			);
		}
		return results;
	}

	private findCompletingToolMessage(
		toolCalls: AgentToolCallPart[],
		toolMessages: AgentMessage[],
	): AgentMessage | undefined {
		for (let index = 0; index < toolCalls.length; index += 1) {
			const toolCall = toolCalls[index];
			if (this.tools.get(toolCall.toolName)?.lifecycle?.completesRun !== true) {
				continue;
			}
			const toolMessage = toolMessages[index];
			const result = toolMessage?.content.find(
				(part): part is Extract<AgentMessagePart, { type: "tool-result" }> =>
					part.type === "tool-result" &&
					part.toolCallId === toolCall.toolCallId,
			);
			if (result && !result.isError) {
				return toolMessage;
			}
		}
		return undefined;
	}

	private async prepareToolExecution(
		toolCall: AgentToolCallPart,
	): Promise<PreparedToolExecution> {
		const tool = this.tools.get(toolCall.toolName);
		let input = toolCall.input;
		let skipReason: string | undefined;
		const metadata =
			toolCall.metadata &&
			typeof toolCall.metadata === "object" &&
			!Array.isArray(toolCall.metadata)
				? (toolCall.metadata as Record<string, unknown>)
				: undefined;

		if (typeof metadata?.inputParseError === "string") {
			skipReason = metadata.inputParseError;
		}

		const toolSource =
			metadata?.toolSource &&
			typeof metadata.toolSource === "object" &&
			!Array.isArray(metadata.toolSource)
				? (metadata.toolSource as Record<string, unknown>)
				: undefined;
		if (toolSource?.executionMode === "provider") {
			const providerId =
				typeof toolSource.providerId === "string"
					? toolSource.providerId
					: "provider";
			skipReason = `Tool execution is disabled for provider ${providerId}`;
		}

		if (tool && !skipReason) {
			input = normalizeJsonLikeStringsForSchema(input, tool.inputSchema);
		}

		let policyOverride: ToolPolicy | undefined;
		if (tool && !skipReason) {
			for (const hook of this.hooks.beforeTool) {
				const result = (await hook({
					snapshot: this.snapshot(),
					tool,
					toolCall: { ...toolCall, input },
					input,
				})) as AgentBeforeToolResult | undefined;
				if (result?.input !== undefined) {
					input = result.input;
				}
				if (result?.policy) {
					policyOverride = {
						...policyOverride,
						...result.policy,
					};
				}
				if (result?.appendContext?.trim()) {
					this.pendingHookContexts.push(
						formatHookContextBlock(
							{ source: "PreToolUse", toolCall },
							result.appendContext,
						),
					);
				}
				this.applyStopControl(result);
				if (result?.skip) {
					skipReason =
						result.reason ?? `Tool ${tool.name} was blocked by a runtime hook`;
					break;
				}
			}
		}

		if (tool && !skipReason) {
			const policy = {
				...resolveToolPolicy(toolCall.toolName, this.config.toolPolicies),
				...policyOverride,
			};
			if (policy.enabled === false) {
				skipReason = `Tool "${toolCall.toolName}" is disabled by policy`;
			} else if (policy.autoApprove === false) {
				const approval = await this.requestToolApproval(
					toolCall,
					input,
					policy,
				);
				if (!approval.approved) {
					const reason = approval.reason ?? "Tool was not executed";
					skipReason = `${reason} -- ${TOOL_REJECTION_SUFFIX}`;
				}
			}
		}

		return {
			toolCall: { ...toolCall, input },
			tool,
			input,
			skipReason,
		};
	}

	private async requestToolApproval(
		toolCall: AgentToolCallPart,
		input: unknown,
		policy: ToolPolicy,
	): Promise<ToolApprovalResult> {
		const requestApproval = this.config.requestToolApproval;
		if (!requestApproval) {
			return {
				approved: false,
				reason: `Tool "${toolCall.toolName}" requires approval but no approval callback is configured`,
			};
		}
		try {
			return await requestApproval({
				sessionId:
					this.config.sessionId?.trim() ||
					this.config.conversationId?.trim() ||
					this.state.runId ||
					this.state.agentId,
				agentId: this.state.agentId,
				conversationId:
					this.config.conversationId?.trim() ||
					this.state.runId ||
					this.state.agentId,
				iteration: this.state.iteration,
				toolCallId: toolCall.toolCallId,
				toolName: toolCall.toolName,
				input,
				policy,
			});
		} catch (error) {
			return {
				approved: false,
				reason: `Tool "${toolCall.toolName}" approval request failed: ${
					error instanceof Error ? error.message : String(error)
				}`,
			};
		}
	}

	private async executePreparedTool(
		prepared: PreparedToolExecution,
	): Promise<AgentMessage> {
		const startedAt = new Date();
		await this.emit({
			type: "tool-started",
			snapshot: this.snapshot(),
			iteration: this.state.iteration,
			toolCall: prepared.toolCall,
		});

		let result: AgentToolResult;
		if (prepared.skipReason) {
			result = {
				output: { error: prepared.skipReason },
				isError: true,
			};
		} else if (!prepared.tool) {
			result = {
				output: { error: `Unknown tool: ${prepared.toolCall.toolName}` },
				isError: true,
			};
		} else {
			try {
				const output = await prepared.tool.execute(prepared.input, {
					sessionId: this.config.sessionId,
					agentId: this.state.agentId,
					conversationId: this.config.conversationId,
					runId: this.state.runId ?? createUID("run"),
					iteration: this.state.iteration,
					toolCallId: prepared.toolCall.toolCallId,
					signal: this.abortController?.signal,
					metadata: this.config.toolContextMetadata,
					snapshot: this.snapshot(),
					emitUpdate: (update: unknown) => {
						void this.emit({
							type: "tool-updated",
							snapshot: this.snapshot(),
							iteration: this.state.iteration,
							toolCall: prepared.toolCall,
							update,
						});
					},
				});
				result = { output };
			} catch (error) {
				result = {
					output: {
						error: error instanceof Error ? error.message : String(error),
					},
					isError: true,
				};
			}
		}

		const endedAt = new Date();
		const durationMs = Math.max(0, endedAt.getTime() - startedAt.getTime());

		if (prepared.tool) {
			for (const hook of this.hooks.afterTool) {
				const after = (await hook({
					snapshot: this.snapshot(),
					tool: prepared.tool,
					toolCall: prepared.toolCall,
					input: prepared.input,
					result,
					startedAt,
					endedAt,
					durationMs,
				})) as AgentAfterToolResult | undefined;
				if (after?.appendContext?.trim()) {
					this.pendingHookContexts.push(
						formatHookContextBlock(
							{ source: "PostToolUse", toolCall: prepared.toolCall },
							after.appendContext,
						),
					);
				}
				this.applyStopControl(after);
				if (after?.result) {
					result = after.result;
				}
			}
		}

		const message = createMessage("tool", [
			{
				type: "tool-result",
				toolCallId: prepared.toolCall.toolCallId,
				toolName: prepared.toolCall.toolName,
				output: result.output,
				isError: result.isError,
			},
		]);

		await this.emit({
			type: "tool-finished",
			snapshot: this.snapshot(),
			iteration: this.state.iteration,
			toolCall: prepared.toolCall,
			message,
		});

		return message;
	}

	private finishRun(
		status: AgentRunResult["status"],
		assistantMessage?: AgentMessage,
		outputText?: string,
	): AgentRunResult {
		this.state.status = status;
		return {
			agentId: this.state.agentId,
			agentRole: this.state.agentRole,
			runId: this.state.runId ?? createUID("run"),
			status,
			iterations: this.state.iteration,
			outputText:
				outputText ??
				textFromMessage(assistantMessage ?? this.findLastAssistantMessage()),
			messages: cloneMessages(this.state.messages),
			usage: cloneUsage(this.state.usage),
		};
	}

	private findLastAssistantMessage(): AgentMessage | undefined {
		return [...this.state.messages]
			.reverse()
			.find((message) => message.role === "assistant");
	}

	private throwIfAborted(): void {
		if (this.abortController?.signal.aborted) {
			throw this.normalizeAbortError();
		}
	}

	private normalizeAbortError(): Error {
		const reason = this.abortController?.signal.reason;
		if (reason instanceof Error) {
			return reason;
		}
		if (typeof reason === "string") {
			return new Error(reason);
		}
		return new Error(this.state.lastError ?? "Run aborted");
	}

	private async emit(event: AgentRuntimeEvent): Promise<void> {
		const metadata = buildEventMetadata(event);
		switch (event.type) {
			case "run-started":
				// 原版 clinee 调用 `logger?.info?.(...)`。sdk-re 的
				// `BasicLogger` 未声明 `info`（它用 `log`），所以我们在
				// 调用点收窄为可选 info 的形状，在不改动 shared 的
				// `BasicLogger` 接口的前提下保留 clinee 运行时契约。
				(
					this.config.logger as
						| {
								info?: (msg: string, md?: unknown) => void;
						  }
						| undefined
				)?.info?.("Agent run started", metadata);
				break;
			case "tool-finished":
				(
					this.config.logger as
						| {
								info?: (msg: string, md?: unknown) => void;
						  }
						| undefined
				)?.info?.("Agent tool finished", metadata);
				break;
			case "run-failed":
				this.config.logger?.error?.("Agent run failed", {
					...metadata,
					error: event.error,
				});
				// 模型层已在其自身错误边界（`provider.stream`，经流的
				// 字符串展平边界以 `finish.errorReported` 携带）记录过的失败，
				// 不得在这里重复上报——那曾使 `sdk.error` 量正好翻倍。
				// 其他一切仍会照常上报：源自循环的失败，以及来自不自行记录
				// 遥测的模型实现的失败。
				if (!this.state.lastErrorReported) {
					captureSdkError(this.config.telemetry, {
						component: "agents",
						operation: "agent.run",
						error: event.error,
						severity: "error",
						handled: false,
						context: {
							...(metadata as TelemetryProperties),
							providerId: this.getTelemetryProviderId(),
							modelId: this.getTelemetryModelId(),
						},
					});
				}
				break;
			default:
				this.config.logger?.debug?.("Agent event", metadata);
				break;
		}
		switch (event.type) {
			// 逐 token/逐块（chunk）的流事件约占 agent.* 遥测量的 97%，
			// 且从不会被查询，所以不镜像到遥测。下面的监听器与 hook
			// 仍会收到它们。
			case "assistant-text-delta":
			case "assistant-reasoning-delta":
			case "assistant-media":
			case "tool-updated":
				break;
			default:
				this.config.telemetry?.capture({
					event: `agent.${event.type}`,
					properties: metadata as TelemetryProperties,
				});
				break;
		}
		for (const listener of this.listeners) {
			listener(event);
		}
		for (const hook of this.hooks.onEvent) {
			await hook(event);
		}
	}

	private applyStopControl(
		control: AgentStopControl | undefined | undefined,
	): void {
		if (!control?.stop) {
			return;
		}
		if (control.reason) {
			this.state.lastError = control.reason;
		}
		throw new ControlledStopError(control.reason);
	}
}

function buildEventMetadata(event: AgentRuntimeEvent): Record<string, unknown> {
	return {
		agentId: event.snapshot.agentId,
		agentRole: event.snapshot.agentRole,
		runId: event.snapshot.runId,
		status: event.snapshot.status,
		iteration: event.snapshot.iteration,
		eventType: event.type,
	};
}

function mergeToolMetadata(current: unknown, patch: unknown): unknown {
	if (!patch || typeof patch !== "object" || Array.isArray(patch)) {
		return patch;
	}
	if (!current || typeof current !== "object" || Array.isArray(current)) {
		return patch;
	}
	return {
		...(current as Record<string, unknown>),
		...patch,
	};
}

function parseToolInput(assembly: PendingToolAssembly): {
	input: unknown;
	parseError?: string;
	invalidInput: Record<string, unknown>;
	reason?: InvalidToolCall["reason"];
} {
	if (assembly.inputValue !== undefined) {
		return {
			input: assembly.inputValue,
			invalidInput: buildInvalidToolInput(JSON.stringify(assembly.inputValue)),
		};
	}
	if (!assembly.inputText.trim()) {
		return {
			input: {},
			invalidInput: {},
		};
	}
	const parsed = parseToolArguments(assembly.inputText);
	if (parsed.ok) {
		return {
			input: parsed.value,
			invalidInput: buildInvalidToolInput(assembly.inputText),
		};
	}
	return {
		input: {},
		invalidInput: buildInvalidToolInput(assembly.inputText, parsed.error),
		parseError: `Tool call ${assembly.toolName ?? assembly.toolCallId} emitted invalid JSON arguments: ${parsed.error}`,
		reason: "invalid_arguments",
	};
}

function buildInvalidToolInput(
	value: string,
	parseError?: string,
): Record<string, unknown> {
	const trimmed = value.trim();
	if (!trimmed) {
		return {};
	}
	return parseError
		? { rawInputText: value, parseError }
		: { rawInputText: value };
}

function parseToolArguments(
	value: string,
): { ok: true; value: unknown } | { ok: false; error: string } {
	const trimmed = value.trim();
	if (!trimmed) {
		return {
			ok: false,
			error: "Tool call arguments were empty.",
		};
	}

	try {
		return { ok: true, value: JSON.parse(trimmed) };
	} catch {
		// 继续往下走，返回下面规范化后的错误。
	}

	if (!(trimmed.startsWith("{") || trimmed.startsWith("["))) {
		return {
			ok: false,
			error: "Tool call arguments must be encoded as a JSON object or array.",
		};
	}

	return {
		ok: false,
		error:
			"Tool call arguments could not be parsed as JSON. Ensure the outer tool payload is valid JSON and escape embedded quotes/newlines inside string fields.",
	};
}

function mergeToolInputText(current: string, incoming: string): string {
	if (!current) {
		return incoming;
	}
	const trimmed = incoming.trimStart();
	if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
		return incoming;
	}
	return current + incoming;
}

export function createAgentRuntime(config: AgentRuntimeConfig): AgentRuntime {
	return new AgentRuntime(config);
}

/**
 * `Agent` 是 `AgentRuntime` 的用户友好名称。二者是同一个类；
 * 这个别名存在是为了让独立调用方可以写：
 *
 *     const agent = new Agent({ providerId, modelId, apiKey });
 *     await agent.run("hello");
 *
 * 而拥有模型构造职责的 `@cline/core` 继续使用 `AgentRuntime` 名称
 * 搭配 `{ model, ... }` 配置。
 */
export const Agent = AgentRuntime;
export type Agent = AgentRuntime;

export function createAgent(config: AgentRuntimeConfig): AgentRuntime {
	return new AgentRuntime(config);
}
