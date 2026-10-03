/**
 * Agent 类型与 Zod Schema
 *
 * Agent 配置、事件、hooks、扩展和结果的规范类型定义。
 *
 * ProviderConfig 在此保留为 `unknown`，使 shared 不依赖 @cline/llms。
 * 消费包通过重新导出进行窄化。ModelInfo 位于 shared
 * (../llms/model-info) 并直接使用。
 */

import { z } from "zod";
import type {
	AgentRuntimeHooks,
	AgentTool,
	ProviderErrorClass,
} from "../agent";
import type { ExtensionContext } from "../extensions/context";
import type {
	AgentExtensionApi,
	AgentExtensionHooks,
	AgentExtensionRegistry as AgentExtensionRegistryGeneric,
	ContributionRegistryExtension,
	PluginManifest,
	PluginSetupContext,
} from "../extensions/contribution-registry";
import type { HookControl } from "../hooks/contracts";
import type { GeneratedMedia } from "../llms/media";
import type { Message, MessageWithMetadata } from "../llms/messages";
import type { ModelInfo } from "../llms/model-info";
import { ModelInfoSchema } from "../llms/model-info";
import type { ModelTool } from "../llms/model-tools";
import {
	type ReasoningEffort,
	ReasoningEffortSchema,
} from "../llms/reasoning-options";

export {
	REASONING_LEVELS,
	type ReasoningEffort,
	ReasoningEffortSchema,
	type ReasoningLevel,
	ReasoningLevelSchema,
} from "../llms/reasoning-options";

import type {
	ToolApprovalRequest,
	ToolApprovalResult,
	ToolCallRecord,
	ToolPolicy,
} from "../llms/tools";
import { ToolCallRecordSchema } from "../llms/tools";
import type { BasicLogger } from "../logging/logger";
import type { ITelemetryService } from "../services/telemetry";
import type { WorkspaceInfo } from "../session/workspace";

// =============================================================================
// Agent 事件
// =============================================================================

/**
 * Agent 执行期间发射的事件
 */
export type AgentEvent =
	| AgentContentStartEvent
	| AgentContentUpdateEvent
	| AgentContentEndEvent
	| AgentIterationStartEvent
	| AgentIterationEndEvent
	| AgentNoticeEvent
	| AgentUsageEvent
	| AgentDoneEvent
	| AgentErrorEvent;

export type AgentContentType = "text" | "reasoning" | "media" | "tool";

export interface AgentEventMetadata {
	/** 当前 ID */
	agentId?: string;
	/** 任务 ID */
	conversationId?: string;
	/** 创建此 Agent 的 Agent ID */
	parentAgentId?: string | null;
}

export interface AgentContentStartEvent extends AgentEventMetadata {
	type: "content_start";
	contentType: AgentContentType;
	/** 从模型接收的文本块 */
	text?: string;
	/** 此轮次至今累积的文本 */
	accumulated?: string;
	/** 来自模型的推理/思考文本 */
	reasoning?: string;
	/** 是否为脱敏推理 */
	redacted?: boolean;
	/** 被调用工具的名称 */
	toolName?: string;
	/** 此工具调用的唯一标识符 */
	toolCallId?: string;
	/** 传递给工具的输入 */
	input?: unknown;
	/** 当模型工具被执行时设置；普通本地工具不存在此字段。 */
	execution?: "client" | "provider";
}

export interface AgentContentUpdateEvent extends AgentEventMetadata {
	type: "content_update";
	contentType: "tool";
	/** 发射进度的工具名称 */
	toolName?: string;
	/** 此工具调用的唯一标识符 */
	toolCallId?: string;
	/** 工具发射的部分结果 */
	update: unknown;
}

export interface AgentContentEndEvent extends AgentEventMetadata {
	type: "content_end";
	contentType: AgentContentType;
	/** 此轮次生成的最终文本 */
	text?: string;
	/** 此轮次生成的最终推理/思考文本 */
	reasoning?: string;
	/** 模型返回的生成媒体。 */
	media?: GeneratedMedia;
	/** 完成工具的名称 */
	toolName?: string;
	/** 此工具调用的唯一标识符 */
	toolCallId?: string;
	/** 工具的输出 */
	output?: unknown;
	/** 工具失败时的错误消息 */
	error?: string;
	/** 工具内容耗时（毫秒） */
	durationMs?: number;
	/** 当模型工具被执行时设置；普通本地工具不存在此字段。 */
	execution?: "client" | "provider";
}

export interface AgentIterationStartEvent extends AgentEventMetadata {
	type: "iteration_start";
	/** 迭代编号（从 1 开始） */
	iteration: number;
}

export interface AgentIterationEndEvent extends AgentEventMetadata {
	type: "iteration_end";
	/** 刚完成的迭代编号 */
	iteration: number;
	/** 此迭代是否有工具调用 */
	hadToolCalls: boolean;
	/** 此迭代中的工具调用次数 */
	toolCallCount: number;
}

export interface AgentUsageEvent extends AgentEventMetadata {
	type: "usage";
	/** 此轮次的输入 token 数 */
	inputTokens: number;
	/** 此轮次的输出 token 数 */
	outputTokens: number;
	/** 从缓存读取的 token 数 */
	cacheReadTokens?: number;
	/** 写入缓存的 token 数 */
	cacheWriteTokens?: number;
	/** 此轮次的成本 */
	cost?: number;
	/** 此轮次的推理/思考 token 数，已从 outputTokens 中排除 */
	reasoningTokenCount?: number;

	/** 累积总计 */
	totalInputTokens: number;
	totalCacheReadTokens?: number;
	totalCacheWriteTokens?: number;
	totalOutputTokens: number;
	totalCost?: number;
}

export interface AgentNoticeEvent extends AgentEventMetadata {
	type: "notice";
	noticeType: "recovery" | "stop" | "status";
	message: string;
	displayRole?: "system" | "status";
	reason?:
		| "api_error"
		| "invalid_tool_call"
		| "completion_without_submit"
		| "tool_execution_failed"
		| "mistake_limit"
		| "auto_compaction"
		| "manual_compaction"
		| "compaction_budget_emergency";
	metadata?: Record<string, unknown>;
}

export interface AgentDoneEvent extends AgentEventMetadata {
	type: "done";
	/** Agent 停止的原因 */
	reason: AgentFinishReason;
	/** 最终文本输出 */
	text: string;
	/** 总迭代次数 */
	iterations: number;
	/** 聚合的用量信息 */
	usage?: LegacyAgentUsage;
}

export interface AgentErrorEvent extends AgentEventMetadata {
	type: "error";
	/** 发生的错误 */
	error: Error;
	/** 提供商错误的分类（已知时）。 */
	errorClass?: ProviderErrorClass;
	/** 错误是否可恢复 */
	recoverable: boolean;
	/** 错误发生时的当前迭代 */
	iteration: number;
}

export interface ConsecutiveMistakeLimitContext {
	iteration: number;
	consecutiveMistakes: number;
	maxConsecutiveMistakes: number;
	reason: "api_error" | "invalid_tool_call" | "tool_execution_failed";
	details?: string;
}

export type ConsecutiveMistakeLimitDecision =
	| {
			action: "continue";
			/**
			 * 可选的引导文本，作为用户消息追加后继续。
			 */
			guidance?: string;
	  }
	| {
			action: "stop";
			/**
			 * 因限制停止时可选的原因。
			 */
			reason?: string;
	  };

export interface LoopDetectionConfig {
	softThreshold: number;
	hardThreshold: number;
}

export interface AgentExecutionConfig {
	/**
	 * 升级前的最大连续内部错误数。
	 * 错误包括：API 轮次失败、无效/缺失的工具调用参数、
	 * 以及所有执行的工具调用都失败的迭代。
	 * @default 6
	 */
	maxConsecutiveMistakes?: number;
	/**
	 * 在连续多少次有工具调用的迭代后，
	 * 注入提醒文本块要求 Agent 在信息足够时直接回答。
	 * 设为 `0` 或省略则禁用。
	 * @default 0
	 */
	reminderAfterIterations?: number;
	/**
	 * 在 `reminderAfterIterations` 后注入的自定义提醒文本。
	 * @default "REMINDER: If you have gathered enough information to answer the user's question, please provide your final answer now without using any more tools."
	 */
	reminderText?: string;
	/**
	 * 重复工具调用循环检测。启用时，Agent 检测连续相同的工具调用并介入：
	 * - 在 `softThreshold`：注入恢复通知，敦促换一种方法。
	 * - 在 `hardThreshold`：触发连续错误限制决策路径。
	 *
	 * 设为 `false` 显式禁用。省略或保留 `undefined` 则不检测。
	 * CLI 默认启用，值为 `{ softThreshold: 3, hardThreshold: 5 }`。
	 */
	loopDetection?: false | Partial<LoopDetectionConfig>;
}

// =============================================================================
// Hooks
// =============================================================================

/**
 * Hook 错误处理行为。
 * - "ignore"：吞并 hook 错误并继续 Agent 执行
 * - "throw"：hook 抛出异常时失败 Agent 执行
 */
export type HookErrorMode = "ignore" | "throw";

/**
 * 生命周期 hooks 支持的通用控制。
 */
export type AgentHookControl = Omit<HookControl, "appendMessages"> & {
	/**
	 * 可选的追加到历史的消息。
	 * 主要由 before-agent-start hook 阶段使用。
	 */
	appendMessages?: Message[];
	/**
	 * 可选的替换消息历史。
	 * 主要由 before-agent-start hooks 和宿主拥有的上下文管道使用。
	 */
	replaceMessages?: Message[];
};

export interface AgentHookRunStartContext {
	/**
	 * Agent 的 ID
	 */
	agentId: string;
	/**
	 * 会话 ID
	 */
	conversationId: string;
	/**
	 * 生成执行此运行的 Agent 的 Agent ID
	 */
	parentAgentId: string | null;
	/**
	 * 用户提交的提示词
	 */
	userMessage: string;
}

export interface AgentHookScheduleContext {
	scheduleId: string;
	executionId?: string;
	trigger: "scheduled" | "manual";
	triggeredAt?: string;
}

/**
 * 会话作用域和运行作用域上下文共用的工作区位置字段。
 *
 * 这些字段始终来源于宿主会话配置——绝不来自 `process.cwd()`。
 * 插件和 hooks 在需要解析相对于会话工作目录或项目根目录的
 * 路径时必须使用这些值，因为 `--cwd` CLI 标志设置会话 cwd
 * 而不调用 `process.chdir()`，所以 `process.cwd()` 可能返回错误路径。
 */
export interface SessionWorkspaceEnv {
	/**
	 * 宿主配置的会话活动工作目录（如通过 `--cwd`）。
	 * 始终准确——在插件或 hooks 中绝不使用 `process.cwd()`；
	 * 使用此字段。
	 */
	cwd?: string;
	/**
	 * 当工作区/项目根目录与 `cwd` 不同时的值。安装在项目外部的
	 * 全局插件应使用此值而非 `import.meta.url` 技巧或 `process.cwd()`。
	 */
	workspaceRoot?: string;
	/**
	 * 会话的结构化工作区和 git 元数据。
	 *
	 * 包含与系统提示词中 `{{CLINE_METADATA}}` 块相同的信息，
	 * 但以结构化形式：`rootPath`、`hint`、`associatedRemoteUrls`、
	 * `latestGitCommitHash`、`latestGitBranchName`。
	 *
	 * 插件和 hooks 可以用此进行分支感知逻辑、提交归属
	 * 或工具集成，无需运行自己的 `git` 调用。
	 * 在会话启动时每个会话填充一次。
	 */
	workspaceInfo?: WorkspaceInfo;
}

/**
 * 在 Agent 对话的生命周期中恰好发射一次，在第一次运行开始前。
 * 这是会话作用域设置的合适位置。
 */
export interface AgentHookSessionStartContext extends SessionWorkspaceEnv {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	schedule?: AgentHookScheduleContext;
}

/**
 * 每次 `run()` / `continue()` 调用在用户输入被接受后、
 * 循环进入第一次迭代前发射一次。
 */
export interface AgentHookRunEndContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	result: AgentResult;
}

/**
 * 在每个循环迭代顶部、任何轮次级提示词或模型准备之前发射。
 */
export interface AgentHookIterationStartContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
}

export interface AgentHookIterationEndContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	hadToolCalls: boolean;
	toolCallCount: number;
}

export interface AgentHookTurnStartContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	messages: Message[];
}

/**
 * 在迭代的模型调用之前立即发射。
 *
 * 与 `onIterationStart` 相比，此 hook 运行得更晚：在轮次开始处理之后，
 * 并携带将发送给模型的确切消息列表。它仍然可以通过替换系统提示词、
 * 追加消息或取消运行来影响即将到来的轮次。
 */
export interface AgentHookBeforeAgentStartContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	systemPrompt: string;
	messages: Message[];
}

export interface AgentHookTurnEndContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	turn: ProcessedTurn;
}

export interface AgentHookToolCallStartContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	call: PendingToolCall;
}

export interface AgentHookToolCallEndContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	record: ToolCallRecord;
}

export interface AgentHookErrorContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	error: Error;
}

export interface AgentHookStopErrorContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	error: Error;
}

export interface AgentHookSessionShutdownContext {
	agentId: string;
	conversationId: string;
	/** 宿主提供的根会话稳定 core 会话 id。 */
	sessionId?: string;
	parentAgentId: string | null;
	/**
	 * 可选的关闭原因（如 "ctrl_d"、"process_exit"）
	 */
	reason?: string;
}

// =============================================================================
// 扩展
// =============================================================================

export interface AgentExtensionRuntimeEventContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	event: AgentEvent;
}

export interface AgentExtensionSessionStartContext extends SessionWorkspaceEnv {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	schedule?: AgentHookScheduleContext;
}

export interface AgentExtensionSessionShutdownContext {
	agentId: string;
	conversationId: string;
	/** 宿主提供的根会话稳定 core 会话 id。 */
	sessionId?: string;
	parentAgentId: string | null;
	reason?: string;
}

export interface AgentExtensionContext extends PluginSetupContext {}

export interface AgentExtension
	extends ContributionRegistryExtension<AgentTool, Message[]> {
	name: string;
	manifest: PluginManifest;
	hooks?: AgentExtensionHooks;
	setup?: (
		api: AgentExtensionApi<AgentTool, Message[]>,
		ctx: AgentExtensionContext,
	) => void | Promise<void>;
}

export type AgentLoopExtensionRegistry = AgentExtensionRegistryGeneric<
	AgentTool,
	Message
>;

/**
 * 用于观察或影响 Agent 执行的生命周期 hooks。
 */
export type AgentHooks = Partial<AgentRuntimeHooks>;

// =============================================================================
// Agent 完成原因
// =============================================================================

/**
 * Agent 停止执行的原因
 */
export type AgentFinishReason =
	| "completed" // 正常完成（无更多工具调用）
	| "max_iterations" // 达到最大迭代限制
	| "aborted" // 用户或系统中止
	| "mistake_limit" // 连续可恢复错误后停止
	| "error"; // 发生不可恢复错误

export const AgentFinishReasonSchema = z.enum([
	"completed",
	"max_iterations",
	"aborted",
	"mistake_limit",
	"error",
]);

// =============================================================================
// Agent 用量
// =============================================================================

/**
 * 聚合的 token 用量和成本信息（旧版，面向宿主形状）。
 *
 * 从 `AgentUsage` 重命名，为运行时更严格的 `AgentUsage`
 * （见 `../agent.ts`）让路。保留是因为面向宿主的
 * `AgentResult`/`AgentUsageEvent` 接口和 `AgentUsageSchema` Zod schema
 * 使用这个更宽松的形状（所有缓存/成本字段可选）。
 * 门面适配器在运行时在两种形状之间转换。
 */
export interface LegacyAgentUsage {
	/** 所有迭代的总输入 token 数 */
	inputTokens: number;
	/** 所有迭代的总输出 token 数 */
	outputTokens: number;
	/** 从缓存读取的总 token 数 */
	cacheReadTokens?: number;
	/** 写入缓存的总 token 数 */
	cacheWriteTokens?: number;
	/** 总成本（美元） */
	totalCost?: number;
}

export const AgentUsageSchema = z.object({
	inputTokens: z.number(),
	outputTokens: z.number(),
	cacheReadTokens: z.number().optional(),
	cacheWriteTokens: z.number().optional(),
	totalCost: z.number().optional(),
});

export interface AgentPrepareTurnContext {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	messages: MessageWithMetadata[];
	apiMessages: MessageWithMetadata[];
	abortSignal: AbortSignal;
	systemPrompt: string;
	tools: AgentTool[];
	model: {
		id: string;
		provider: string;
		info?: ModelInfo;
	};
	/**
	 * 当上一次模型请求因超出模型上下文窗口被拒绝时设置；
	 * 要求 prepare-turn 管道强制执行压缩，而不是信任其 token 估算。
	 */
	overflowRecovery?: boolean;
	/**
	 * 提供商为本次运行的上一次请求实际计数的输入 token 数（可用时）。
	 * 压缩用它作为自身基于字符估算的下限，后者会低估密集内容
	 * （反汇编、像素转储），否则可能导致实际上下文超过窗口而不触发。
	 */
	previousRequestInputTokens?: number;
	emitStatusNotice?: (
		message: string,
		metadata?: Record<string, unknown>,
	) => void;
}

export interface AgentPrepareTurnResult {
	messages?: MessageWithMetadata[];
	systemPrompt?: string;
}

// =============================================================================
// Agent 结果
// =============================================================================

/**
 * 从 Agent.run() 返回的结果
 */
export interface AgentResult {
	/** Agent 的最终文本输出 */
	text: string;
	/** 聚合的 token 用量和成本 */
	usage: LegacyAgentUsage;
	/** 完整对话历史 */
	messages: MessageWithMetadata[];
	/** 执行期间的所有工具调用 */
	toolCalls: ToolCallRecord[];
	/** 循环迭代次数 */
	iterations: number;
	/** Agent 停止的原因 */
	finishReason: AgentFinishReason;
	/** 使用的模型信息 */
	model: {
		id: string;
		provider: string;
		info?: ModelInfo;
	};
	/** 运行开始时间 */
	startedAt: Date;
	/** 运行结束时间 */
	endedAt: Date;
	/** 总耗时（毫秒） */
	durationMs: number;
}

export const AgentResultSchema = z.object({
	text: z.string(),
	usage: AgentUsageSchema,
	messages: z.array(z.custom<MessageWithMetadata>()),
	toolCalls: z.array(ToolCallRecordSchema),
	iterations: z.number(),
	finishReason: AgentFinishReasonSchema,
	model: z.object({
		id: z.string(),
		provider: z.string(),
		info: ModelInfoSchema.optional(),
	}),
	startedAt: z.date(),
	endedAt: z.date(),
	durationMs: z.number(),
});

// =============================================================================
// Agent 配置
// =============================================================================

/**
 * 创建 Agent 的配置
 */
export interface AgentConfig {
	/** 用于提供商和可观测性元数据的稳定终端用户身份。 */
	distinctId?: string;
	/**
	 * Core/hub 运行时会话标识符。
	 *
	 * 此任务/会话的宿主拥有生命周期 id。Core 用它进行
	 * 持久化、事件路由、中止/停止操作和审批传递。
	 * 这有意与 `conversationId` 分开，后者标识
	 * 由 Agent 运行时管理的模型对话记录。
	 */
	sessionId?: string;
	// -------------------------------------------------------------------------
	// 提供商设置
	// -------------------------------------------------------------------------

	/** 提供商 ID（如 "anthropic"、"openai"、"gemini"） */
	providerId: string;
	/** 要使用的模型 ID */
	modelId: string;
	/** 提供商的 API 密钥 */
	apiKey?: string;
	/** 自定义 API 基础 URL */
	baseUrl?: string;
	/** API 请求的附加头 */
	headers?: Record<string, string>;
	/**
	 * 当运行因类似认证的提供商错误（如运行中过期的 OAuth
	 * 访问令牌）而失败时调用。宿主刷新凭据并通过
	 * `updateConnection` 将新密钥推入运行时；返回 `true`
	 * 使运行时用刷新的连接重试失败的运行一次。
	 */
	onAuthError?: () => Promise<boolean>;
	/** 可选的提供商模型目录覆盖 */
	knownModels?: Record<string, ModelInfo>;
	/** 可选的预解析提供商配置（包括提供商特定字段如 aws/gcp）。 */
	providerConfig?: unknown;
	/**
	 * 可选的预加载对话历史，用于恢复流程。
	 * 提供时，通过调用 continue() 保留历史。
	 */
	initialMessages?: Message[];

	// -------------------------------------------------------------------------
	// Agent 行为
	// -------------------------------------------------------------------------

	/** Agent 的系统提示词 */
	systemPrompt: string;
	/** Agent 可用的工具 */
	tools: AgentTool[];
	/** 为选定模型启用的提供商执行工具。 */
	modelTools?: ModelTool[];
	/**
	 * 最大循环迭代次数
	 * 如果未定义，不强制迭代上限。
	 */
	maxIterations?: number;
	/**
	 * 单次迭代中并发执行的最大工具调用数。
	 * @default 8
	 */
	maxParallelToolCalls?: number;
	/**
	 * 每次 API 调用的最大输出 token 数
	 */
	maxTokensPerTurn?: number;
	/**
	 * 每次 API 调用的采样温度
	 */
	temperature?: number;
	/**
	 * 每次 API 调用的超时时间（毫秒）
	 * @default 180000（3 分钟）
	 */
	apiTimeoutMs?: number;
	/**
	 * 可选的运行时文件内容加载器，在附加用户文件时使用。
	 * 省略时，附加文件将表示为加载器错误。
	 */
	userFileContentLoader?: (path: string) => Promise<string>;
	/**
	 * 可选的元数据，合并到每个工具执行上下文中。
	 * 宿主可以用此线程传递运行时特定标识符，如会话 ID。
	 */
	toolContextMetadata?: Record<string, unknown>;
	/** 执行防护栏和恢复设置。 */
	execution?: AgentExecutionConfig;

	// -------------------------------------------------------------------------
	// 推理设置（适用于有能力的模型）
	// -------------------------------------------------------------------------

	/**
	 * 推理努力级别
	 */
	reasoningEffort?: ReasoningEffort;
	/**
	 * 思考/推理的最大 token 数
	 */
	thinkingBudgetTokens?: number;
	/**
	 * 为支持的模型启用默认思考/推理行为。
	 */
	thinking?: boolean;

	// -------------------------------------------------------------------------
	// 回调
	// -------------------------------------------------------------------------

	/**
	 * Agent 事件的回调（流式、进度等）
	 */
	onEvent?: (event: AgentEvent) => void;
	/**
	 * 用于观察或影响 Agent 执行的生命周期 hooks。
	 */
	hooks?: AgentHooks;
	/**
	 * 可选的父 Agent ID，用于生成/委派的运行。
	 * 根 Agent 应保留为未定义。
	 */
	parentAgentId?: string;
	/**
	 * 可截获生命周期事件并注册工具/命令的扩展模块。
	 */
	extensions?: AgentExtension[];
	/**
	 * hook 错误的处理方式。
	 * @default "ignore"
	 */
	hookErrorMode?: HookErrorMode;
	/**
	 * 可选的调度元数据，用于由调度服务发起的运行。
	 * 被 session_start 生命周期 hooks 使用。
	 */
	schedule?: AgentHookScheduleContext;
	/**
	 * 每工具执行策略。未在此列出的工具名称默认为启用 + 自动审批。
	 */
	toolPolicies?: Record<string, ToolPolicy>;
	/**
	 * 可选回调，当工具策略禁用自动审批时请求客户端审批。
	 */
	requestToolApproval?: (
		request: ToolApprovalRequest,
	) => Promise<ToolApprovalResult> | ToolApprovalResult;
	/**
	 * 可选回调，当连续错误达到 maxConsecutiveMistakes 时调用。
	 */
	onConsecutiveMistakeLimitReached?: (
		context: ConsecutiveMistakeLimitContext,
	) =>
		| Promise<ConsecutiveMistakeLimitDecision>
		| ConsecutiveMistakeLimitDecision;
	/**
	 * 可选日志器，用于跟踪 Agent 循环生命周期和可恢复失败。
	 */
	logger?: BasicLogger;
	/**
	 * 可选的请求投影 hook，在每次模型调用前调用。
	 *
	 * 返回的消息仅影响当前调用的提供商请求。
	 * 它们不替换规范运行时对话记录，不作为会话历史持久化，
	 * 也不反映在 AgentRunResult.messages 中。
	 *
	 * 需要持久脱敏或规范化的宿主必须在消息进入规范对话记录之前应用。
	 */
	prepareTurn?: (
		context: AgentPrepareTurnContext,
	) =>
		| Promise<AgentPrepareTurnResult | undefined>
		| AgentPrepareTurnResult
		| undefined;
	/**
	 * 可选遥测服务，用于向配置的遥测后端发射有关 Agent 执行的结构化事件。
	 */
	telemetry?: ITelemetryService;
	/**
	 * 环境运行时上下文：用户身份、客户端界面、工作区、日志器
	 * 和遥测。线程传递到 ProviderConfig 以便处理器访问。
	 */
	extensionContext?: ExtensionContext;

	// -------------------------------------------------------------------------
	// 完成防护
	// -------------------------------------------------------------------------

	/**
	 * 一等运行时完成策略。基于工具的完成从最终 Agent 工具列表解析，
	 * 因此内置和插件工具可以通过 `lifecycle.completesRun` 选择加入。
	 *
	 * `completionGuard` 在模型未返回工具调用时运行。
	 * 如果它返回非空字符串，该字符串作为系统级提示注入，
	 * 循环继续而不是完成。用此防止 Agent 在
	 * 有未完成义务（如进行中的团队任务）时过早退出。
	 */
	completionPolicy?: {
		requireCompletionTool?: boolean;
		completionGuard?: () => string | undefined;
	};

	// -------------------------------------------------------------------------
	// 待处理用户消息
	// -------------------------------------------------------------------------

	/**
	 * 可选回调，在每个 Agent 循环迭代顶部调用
	 * （第一次之后）。如果返回非空字符串，该字符串作为
	 * 用户消息在下一次 API 调用前注入对话。
	 * 这允许宿主将用户输入馈入运行中的循环，
	 * 而无需等待当前运行完成。
	 */
	consumePendingUserMessage?: () => string | undefined;

	// -------------------------------------------------------------------------
	// 取消
	// -------------------------------------------------------------------------

	/**
	 * 用于取消的中止信号
	 */
	abortSignal?: AbortSignal;
}

export const AgentConfigSchema = z.object({
	distinctId: z.string().optional(),
	sessionId: z.string().optional(),
	// 提供商设置
	providerId: z.string(),
	modelId: z.string(),
	apiKey: z.string().optional(),
	baseUrl: z.string().url().optional(),
	headers: z.record(z.string(), z.string()).optional(),
	knownModels: z.record(z.string(), ModelInfoSchema).optional(),
	providerConfig: z.unknown().optional(),
	initialMessages: z.array(z.custom<Message>()).optional(),

	// Agent 行为
	systemPrompt: z.string(),
	tools: z.array(z.custom<AgentTool>()),
	modelTools: z.array(z.custom<ModelTool>()).optional(),
	maxIterations: z.number().positive().optional(),
	maxParallelToolCalls: z.number().int().positive().default(8),
	maxTokensPerTurn: z.number().positive().optional(),
	temperature: z.number().nonnegative().optional(),
	apiTimeoutMs: z.number().positive().default(180000),
	userFileContentLoader: z
		.function()
		.input([z.string()])
		.output(z.promise(z.string()))
		.optional(),
	toolContextMetadata: z.record(z.string(), z.unknown()).optional(),
	execution: z
		.object({
			maxConsecutiveMistakes: z.number().int().positive().optional(),
			reminderAfterIterations: z.number().nonnegative().optional(),
			reminderText: z.string().optional(),
			loopDetection: z
				.union([
					z.literal(false),
					z.object({
						softThreshold: z.number().int().positive().optional(),
						hardThreshold: z.number().int().positive().optional(),
					}),
				])
				.optional(),
		})
		.optional(),
	// 推理设置
	reasoningEffort: ReasoningEffortSchema.optional(),
	thinkingBudgetTokens: z.number().positive().optional(),
	thinking: z.boolean().optional(),

	// 回调
	onEvent: z
		.function()
		.input([z.custom<AgentEvent>()])
		.output(z.void())
		.optional(),
	hooks: z.custom<AgentHooks>().optional(),
	parentAgentId: z.string().optional(),
	extensions: z.array(z.custom<AgentExtension>()).optional(),
	hookErrorMode: z.enum(["ignore", "throw"]).default("ignore"),
	toolPolicies: z
		.record(
			z.string(),
			z.object({
				enabled: z.boolean().optional(),
				autoApprove: z.boolean().optional(),
			}),
		)
		.optional(),
	requestToolApproval: z
		.function()
		.input([
			z.object({
				sessionId: z.string(),
				agentId: z.string(),
				conversationId: z.string(),
				iteration: z.number(),
				toolCallId: z.string(),
				toolName: z.string(),
				input: z.unknown(),
				policy: z
					.object({
						enabled: z.boolean().optional(),
						autoApprove: z.boolean().optional(),
					})
					.default({}),
			}),
		])
		.output(
			z.union([
				z.object({
					approved: z.boolean(),
					reason: z.string().optional(),
				}),
				z.promise(
					z.object({
						approved: z.boolean(),
						reason: z.string().optional(),
					}),
				),
			]),
		)
		.optional(),
	onConsecutiveMistakeLimitReached: z
		.function()
		.input([
			z.object({
				iteration: z.number().int().positive(),
				consecutiveMistakes: z.number().int().positive(),
				maxConsecutiveMistakes: z.number().int().positive(),
				reason: z.enum([
					"api_error",
					"invalid_tool_call",
					"tool_execution_failed",
				]),
				details: z.string().optional(),
			}),
		])
		.output(
			z.union([
				z.object({
					action: z.literal("continue"),
					guidance: z.string().optional(),
				}),
				z.object({
					action: z.literal("stop"),
					reason: z.string().optional(),
				}),
				z.promise(
					z.union([
						z.object({
							action: z.literal("continue"),
							guidance: z.string().optional(),
						}),
						z.object({
							action: z.literal("stop"),
							reason: z.string().optional(),
						}),
					]),
				),
			]),
		)
		.optional(),
	logger: z.custom<BasicLogger>().optional(),
	extensionContext: z.custom<ExtensionContext>().optional(),

	// 取消
	abortSignal: z.custom<AbortSignal>().optional(),
});

// =============================================================================
// 内部类型
// =============================================================================

/**
 * 来自模型的待处理工具调用
 */
export interface PendingToolCall {
	id: string;
	name: string;
	input: unknown;
	signature?: string;
	review?: boolean;
}

/**
 * 循环一次迭代的处理后响应
 */
export interface ProcessedTurn {
	/** 模型的文本输出 */
	text: string;
	/** 推理/思考内容 */
	reasoning?: string;
	/** 模型请求的工具调用 */
	toolCalls: PendingToolCall[];
	/** 模型发射的无效或缺少必填字段的工具调用 */
	invalidToolCalls: Array<{
		id: string;
		name?: string;
		input?: unknown;
		reason: "missing_name" | "missing_arguments" | "invalid_arguments";
	}>;
	/** 此轮次的 token 用量 */
	usage: {
		inputTokens: number;
		outputTokens: number;
		cacheReadTokens?: number;
		cacheWriteTokens?: number;
		cost?: number;
	};
	/** 响应是否被截断 */
	truncated: boolean;
	/** 来自 API 的响应 ID */
	responseId?: string;
}
