import { estimateRequestInputTokens } from "@cline/shared";
import { resolveConnectionProviderConfig } from "../../services/llms/handler-factory";
import {
	captureCompactionBudgetEmergency,
	captureCompactionExecuted,
	captureCompactionSkipped,
	type TelemetryCompactionStrategy,
} from "../../services/telemetry/core-events";
import {
	createSessionCompactionState,
	projectSessionCompactionState,
	type SessionCompactionState,
} from "../../session/models/session-compaction";
import type {
	CoreCompactionConfig,
	CoreCompactionContext,
	CoreCompactionMode,
	CoreCompactionResult,
	CoreCompactionStrategy,
	CoreSessionConfig,
} from "../../types/config";
import type { ProviderConfig } from "../../types/provider-settings";
import { runAgenticCompaction } from "./agentic-compaction";
import { runBasicCompaction } from "./basic-compaction";
import {
	COMPACTION_TRIGGER_RATIO,
	createTokenEstimator,
	DEFAULT_MAX_INPUT_TOKENS,
	DEFAULT_PRESERVE_RECENT_TOKENS,
	DEFAULT_TARGET_RATIO,
	findLatestSummaryIndex,
	MAX_INPUT_UNDERESTIMATE_FACTOR,
	resolveEffectiveMaxInputTokens,
} from "./compaction-shared";

export interface ContextPipelinePrepareTurnInput {
	agentId: string;
	conversationId: string;
	parentAgentId: string | null;
	iteration: number;
	messages: CoreCompactionContext["messages"];
	apiMessages: CoreCompactionContext["messages"];
	abortSignal: AbortSignal;
	systemPrompt: string;
	tools: unknown[];
	model: CoreCompactionContext["model"];
	/**
	 * 当提供者以上一请求超出模型上下文窗口为由拒绝时，由运行时设置。
	 * 无视 token 估算触发条件强制压缩（估算刚被证明是错的），
	 * 并使用确定性的 basic 策略——恢复不得依赖另一次成功的 LLM 请求。
	 */
	overflowRecovery?: boolean;
	/**
	 * 本次运行中上一请求的提供者实际报告的输入 token 数，用作基于
	 * 字符估算的下限，使高密度内容仍能触发压缩。参见
	 * AgentPrepareTurnContext.previousRequestInputTokens。
	 */
	previousRequestInputTokens?: number;
	emitStatusNotice?: (
		message: string,
		metadata?: Record<string, unknown>,
	) => void;
}

export interface ContextPipelinePrepareTurnResult {
	messages: CoreCompactionContext["messages"];
	systemPrompt?: string;
}

export type ContextPipelinePrepareTurn = (
	context: ContextPipelinePrepareTurnInput,
) => Promise<ContextPipelinePrepareTurnResult | undefined>;

type EstimateMessageTokens = ReturnType<typeof createTokenEstimator>;

type BuiltinCompactionStrategyOptions = {
	context: CoreCompactionContext;
	providerConfig: ProviderConfig;
	compaction: CoreCompactionConfig | undefined;
	estimateMessageTokens: EstimateMessageTokens;
	logger: Pick<CoreSessionConfig, "logger">["logger"];
};

type BuiltinCompactionStrategyRunner = (
	options: BuiltinCompactionStrategyOptions,
) =>
	| Promise<CoreCompactionResult | undefined>
	| CoreCompactionResult
	| undefined;

export interface ContextCompactionPrepareTurnOptions {
	mode?: CoreCompactionMode;
	manualTargetRatio?: number;
	/** 叠加在 `config.compaction` 之上的覆盖项。 */
	compaction?: Partial<CoreCompactionConfig>;
}

const LONG_CONVERSATION_TARGET_RATIO = 0.5;

function isCompactionCancellation(
	error: unknown,
	abortSignal: AbortSignal,
): boolean {
	if (abortSignal.aborted) {
		return true;
	}
	return (
		error instanceof Error &&
		(error.name === "AbortError" || error.name === "AgentRuntimeAbortError")
	);
}

function describeCompactionError(error: unknown): Record<string, unknown> {
	return error instanceof Error
		? { errorName: error.name, errorMessage: error.message }
		: { errorMessage: String(error) };
}

function safeJsonSize(value: unknown): number {
	try {
		return JSON.stringify(value).length;
	} catch {
		return String(value).length;
	}
}

function summarizeToolResults(messages: CoreCompactionContext["messages"]): {
	toolResultCount: number;
	toolResultSerializedChars: number;
	maxToolResultSerializedChars: number;
} {
	let toolResultCount = 0;
	let toolResultSerializedChars = 0;
	let maxToolResultSerializedChars = 0;
	for (const message of messages) {
		if (!Array.isArray(message.content)) {
			continue;
		}
		for (const block of message.content) {
			if (block.type !== "tool_result") {
				continue;
			}
			const size = safeJsonSize(block.content);
			toolResultCount += 1;
			toolResultSerializedChars += size;
			maxToolResultSerializedChars = Math.max(
				maxToolResultSerializedChars,
				size,
			);
		}
	}
	return {
		toolResultCount,
		toolResultSerializedChars,
		maxToolResultSerializedChars,
	};
}

const BUILTIN_COMPACTION_STRATEGIES = {
	basic: ({ context, estimateMessageTokens, logger }) =>
		runBasicCompaction({
			context,
			estimateMessageTokens,
			logger,
		}),
	agentic: ({
		context,
		providerConfig,
		compaction,
		estimateMessageTokens,
		logger,
	}) =>
		runAgenticCompaction({
			context,
			providerConfig,
			summarizer: compaction?.summarizer,
			preserveRecentTokens: Math.min(
				compaction?.preserveRecentTokens ?? DEFAULT_PRESERVE_RECENT_TOKENS,
				context.budget.messages.targetTokens,
			),
			estimateMessageTokens,
			logger,
		}),
} satisfies Record<CoreCompactionStrategy, BuiltinCompactionStrategyRunner>;

function resolveManualMessageTargetTokens(input: {
	messageInputTokens: number;
	messageTriggerTokens: number;
	manualTargetRatio: number | undefined;
}): number {
	const ratio =
		typeof input.manualTargetRatio === "number" &&
		Number.isFinite(input.manualTargetRatio)
			? input.manualTargetRatio
			: 0.5;
	const targetRatio = Math.min(0.95, Math.max(0.05, ratio));
	return Math.max(
		1,
		Math.floor(
			Math.min(
				input.messageTriggerTokens,
				input.messageInputTokens * targetRatio,
			),
		),
	);
}

function resolveAutoRequestTargetTokens(input: {
	maxInputTokens: number;
	modelMaxTokens?: number;
	triggerTokens: number;
	messagePairCount: number;
}): number {
	const targetTokens =
		input.messagePairCount >= 5 &&
		typeof input.modelMaxTokens === "number" &&
		Number.isFinite(input.modelMaxTokens) &&
		input.modelMaxTokens < input.maxInputTokens
			? Math.floor(input.maxInputTokens * LONG_CONVERSATION_TARGET_RATIO)
			: Math.floor(input.triggerTokens * DEFAULT_TARGET_RATIO);
	const triggerCeiling = Math.max(1, input.triggerTokens - 1);
	return Math.max(
		1,
		Math.min(targetTokens, input.maxInputTokens, triggerCeiling),
	);
}

function translateRequestBudgetToMessages(
	requestTokens: number,
	overheadTokens: number,
): number {
	return Math.max(1, Math.floor(requestTokens - overheadTokens));
}

function countUserAssistantPairs(
	messages: CoreCompactionContext["messages"],
): number {
	let pairs = 0;
	let hasPendingUser = false;
	for (const message of messages) {
		if (message.role === "user") {
			hasPendingUser = true;
		} else if (message.role === "assistant" && hasPendingUser) {
			pairs += 1;
			hasPendingUser = false;
		}
	}
	return pairs;
}

/**
 * 构建 agent 运行时使用的 `prepareTurn` 回调，以便在每个模型请求前
 * 压缩转录。
 *
 * 遥测：压缩成功时发出 `task.compaction_executed`，配置的策略返回
 * `undefined` 时发出 `task.compaction_skipped`。遥测以 `config.sessionId`
 * 为键（回退到每轮的 `conversationId`），并标记 `provider` / `modelId`。
 *
 * 已知缺口：通过插件 `registerMessageBuilder()` 或 `beforeModel` 运行时
 * hook 执行的压缩完全绕过了此包装器，因此不发出压缩遥测。
 * 若也需覆盖那里，插件/hook 管道必须单独插桩。
 */
export function createContextCompactionPrepareTurn(
	config: Pick<
		CoreSessionConfig,
		| "providerConfig"
		| "providerId"
		| "modelId"
		| "apiKey"
		| "baseUrl"
		| "headers"
		| "compaction"
		| "logger"
		| "telemetry"
		| "sessionId"
	>,
	options: ContextCompactionPrepareTurnOptions = {},
):
	| ((
			context: ContextPipelinePrepareTurnInput,
	  ) => Promise<ContextPipelinePrepareTurnResult | undefined>)
	| undefined {
	const userCompaction: CoreCompactionConfig = {
		...config.compaction,
		...options.compaction,
	};
	if (userCompaction.enabled !== true) {
		return undefined;
	}

	const estimateMessageTokens = createTokenEstimator();
	const strategy = userCompaction?.strategy ?? "agentic";
	const runBuiltinStrategy = BUILTIN_COMPACTION_STRATEGIES[strategy];
	const mode = options.mode ?? "auto";
	const telemetryStrategy: TelemetryCompactionStrategy = userCompaction?.compact
		? "custom"
		: strategy;

	return async (context) => {
		const effectiveMode: CoreCompactionMode = context.overflowRecovery
			? "overflow_recovery"
			: mode;
		const apiMessageTokens = context.apiMessages.reduce(
			(total: number, message) => total + estimateMessageTokens(message),
			0,
		);
		const requestInputTokens = estimateRequestInputTokens({
			systemPrompt: context.systemPrompt,
			messages: context.apiMessages,
			tools: context.tools,
		});
		const messageInputTokens = context.messages.reduce(
			(total: number, message) => total + estimateMessageTokens(message),
			0,
		);
		const requestOverheadTokens = Math.max(
			0,
			requestInputTokens - apiMessageTokens,
		);
		const rawMaxInputTokens =
			resolveEffectiveMaxInputTokens({
				maxInputTokens: context.model.info?.maxInputTokens,
				contextWindow: context.model.info?.contextWindow,
			}) ?? DEFAULT_MAX_INPUT_TOKENS;
		// 基于字符的估算会低估高密度内容（反汇编、图像转储、压缩后的
		// 源码）。当提供者对上一请求的实际计数已经超过我们对（更大的）
		// 当前转录的估算时，估算器明显在低估，因此按该比率
		// 整体缩小预算。缩小预算而非仅调整触发线，能让所有下游
		// 数字——触发线、目标值与投影的消息成本——保持在
		// 同一估算单位，同时仍对应提供者的真实上限；只抬高触发线
		// 会启动一次压缩，随后保留过多内容并仍然溢出。
		//
		// 刻意保守：它从不放宽预算，只在有低估的直接证据时启用，
		// 且有上限，使极小的估算无法压垮预算。
		const actualPreviousInputTokens =
			typeof context.previousRequestInputTokens === "number" &&
			context.previousRequestInputTokens > 0
				? context.previousRequestInputTokens
				: 0;
		const underestimateFactor =
			actualPreviousInputTokens > 0 && requestInputTokens > 0
				? Math.min(
						MAX_INPUT_UNDERESTIMATE_FACTOR,
						Math.max(1, actualPreviousInputTokens / requestInputTokens),
					)
				: 1;
		const maxInputTokens = rawMaxInputTokens / underestimateFactor;
		const requestTriggerTokens = maxInputTokens * COMPACTION_TRIGGER_RATIO;
		const messageTriggerTokens = translateRequestBudgetToMessages(
			requestTriggerTokens,
			requestOverheadTokens,
		);
		// 等价于将提供者的实际计数与未缩放的触发线比较，
		// 因为上面的预算已携带该比率。
		const shouldCompact = requestInputTokens >= requestTriggerTokens;
		config.logger?.debug("Context compaction diagnostics", {
			mode: effectiveMode,
			strategy,
			iteration: context.iteration,
			providerId: config.providerId,
			modelId: config.modelId,
			requestInputTokens,
			apiMessageTokens,
			messageInputTokens,
			requestOverheadTokens,
			maxInputTokens,
			rawMaxInputTokens,
			actualPreviousInputTokens,
			underestimateFactor,
			requestTriggerTokens,
			messageTriggerTokens,
			thresholdRatio: COMPACTION_TRIGGER_RATIO,
			shouldCompact,
			messageCount: context.messages.length,
			apiMessageCount: context.apiMessages.length,
			apiMessagesJsonChars: safeJsonSize(context.apiMessages),
			...summarizeToolResults(context.apiMessages),
		});
		if (effectiveMode === "auto" && !shouldCompact) {
			return undefined;
		}
		let requestTargetTokens: number;
		let messageTargetTokens: number;
		if (effectiveMode === "auto") {
			requestTargetTokens = resolveAutoRequestTargetTokens({
				maxInputTokens,
				modelMaxTokens: context.model.info?.maxTokens,
				triggerTokens: requestTriggerTokens,
				messagePairCount: countUserAssistantPairs(context.messages),
			});
			messageTargetTokens = translateRequestBudgetToMessages(
				requestTargetTokens,
				requestOverheadTokens,
			);
		} else {
			messageTargetTokens = resolveManualMessageTargetTokens({
				messageInputTokens,
				messageTriggerTokens,
				manualTargetRatio: options.manualTargetRatio,
			});
			requestTargetTokens = requestOverheadTokens + messageTargetTokens;
		}

		const compactionContext = {
			agentId: context.agentId,
			conversationId: context.conversationId,
			parentAgentId: context.parentAgentId,
			iteration: context.iteration,
			messages: context.messages,
			model: context.model,
			mode: effectiveMode,
			abortSignal: context.abortSignal,
			budget: {
				request: {
					inputTokens: requestInputTokens,
					maxInputTokens,
					triggerTokens: requestTriggerTokens,
					targetTokens: requestTargetTokens,
					overheadTokens: requestOverheadTokens,
					thresholdRatio: COMPACTION_TRIGGER_RATIO,
					utilizationRatio:
						maxInputTokens > 0 ? requestInputTokens / maxInputTokens : 0,
				},
				messages: {
					inputTokens: messageInputTokens,
					triggerTokens: messageTriggerTokens,
					targetTokens: messageTargetTokens,
				},
			},
		};

		const statusReason =
			effectiveMode === "manual"
				? "manual_compaction"
				: effectiveMode === "overflow_recovery"
					? "overflow_recovery_compaction"
					: "auto_compaction";
		const noticePrefix =
			effectiveMode === "manual"
				? ""
				: effectiveMode === "overflow_recovery"
					? "overflow-recovery-"
					: "auto-";
		context.emitStatusNotice?.(`${noticePrefix}compacting`, {
			kind: statusReason,
			reason: statusReason,
			phase: "started",
			iteration: context.iteration,
			triggerTokens: requestTriggerTokens,
			targetTokens: requestTargetTokens,
			maxInputTokens,
			messageTargetTokens,
		});

		const beforeMessageCount = context.messages.length;
		const startedAt = Date.now();

		const builtinOptions = {
			context: compactionContext,
			// 每轮从实时会话配置解析，与主请求优先级相同，
			// 因此摘要器绝不会发送宿主机此后已刷新或替换的凭据。
			providerConfig: {
				...resolveConnectionProviderConfig(config),
				abortSignal: context.abortSignal,
			},
			compaction: userCompaction,
			estimateMessageTokens,
			logger: config.logger,
		};
		let executedStrategy = telemetryStrategy;
		let result: CoreCompactionResult | undefined;
		if (effectiveMode === "overflow_recovery") {
			// 提供者已经拒绝了请求，因此恢复必须确定性地结束：
			// agentic 策略自身的摘要器调用可能溢出同一个窗口（其输入
			// 预算信任的正是刚刚低估的那个估算器）。自定义压缩器有优先
			// 机会——它看到模式 "overflow_recovery" 并拥有自己的转录
			// 不变量——但其结果要与 basic 压缩同样的门槛：严格小于输入
			//（运行时拒绝用未变小的请求重试）且在恢复 token 目标之内。
			// 微小的缩小会把本次运行唯一的重试浪费在仍然无法容纳的请求
			// 上。抛出、拒绝或结果不充分时，basic 压缩会运行，
			// 使恢复绝不依赖另一次成功的 LLM 请求。
			if (userCompaction?.compact) {
				try {
					result = await userCompaction.compact(compactionContext);
				} catch (error) {
					if (isCompactionCancellation(error, context.abortSignal)) {
						throw error;
					}
					config.logger?.log(
						"Custom compaction failed during overflow recovery; falling back to basic compaction",
						{
							severity: "warn",
							...describeCompactionError(error),
						},
					);
					result = undefined;
				}
				if (result?.messages) {
					const customMessageTokens = result.messages.reduce(
						(total: number, message) => total + estimateMessageTokens(message),
						0,
					);
					// 完整的接受门槛，覆盖各种退化情况：非空转录（空转录
					// 会抹掉正在重试的请求）、严格小于输入（运行时拒绝
					// 未变小的重试），且在恢复 token 目标之内（微小的缩小
					// 会把本次运行唯一的重试浪费在仍然无法容纳的请求上）。
					// 两个大小比较都使用 token 估算器而非序列化长度，
					// 从而与目标使用相同的单位。
					const acceptable =
						result.messages.length > 0 &&
						customMessageTokens < messageInputTokens &&
						customMessageTokens <= messageTargetTokens;
					if (!acceptable) {
						config.logger?.log(
							"Custom compaction did not produce an acceptable overflow-recovery transcript; falling back to basic compaction",
							{
								severity: "warn",
								customMessageCount: result.messages.length,
								customMessageTokens,
								messageTargetTokens,
							},
						);
						result = undefined;
					}
				}
			}
			if (!result?.messages) {
				executedStrategy = "basic";
				result = await BUILTIN_COMPACTION_STRATEGIES.basic(builtinOptions);
			}
		} else if (userCompaction?.compact) {
			result = await userCompaction.compact(compactionContext);
		} else {
			try {
				result = await runBuiltinStrategy(builtinOptions);
			} catch (error) {
				if (
					strategy !== "agentic" ||
					isCompactionCancellation(error, context.abortSignal)
				) {
					throw error;
				}
				config.logger?.log(
					"Agentic compaction failed; falling back to basic compaction",
					{
						severity: "warn",
						...describeCompactionError(error),
					},
				);
				executedStrategy = "basic";
				result = await BUILTIN_COMPACTION_STRATEGIES.basic(builtinOptions);
			}
		}

		const durationMs = Date.now() - startedAt;
		// 遥测身份：浮现传入 prepareTurn 的 agent/会话，使多 agent 运行
		// 能正确归属压缩记录。`sessionId` 是宿主拥有的会话 id（ulid）；
		// 未提供 sessionId 时（例如临时调用方）回退到 conversation id。
		const telemetryUlid = config.sessionId ?? context.conversationId;
		const telemetryIdentity = {
			agentId: context.agentId,
			conversationId: context.conversationId,
			parentAgentId: context.parentAgentId ?? undefined,
		};

		if (result?.messages) {
			const afterMessageTokens = result.messages.reduce(
				(total: number, message) => total + estimateMessageTokens(message),
				0,
			);
			const afterRequestTokens = requestOverheadTokens + afterMessageTokens;
			config.logger?.log("Context compaction completed", {
				severity: "info",
				strategy: executedStrategy,
				maxInputTokens,
				messageInputTokens,
				apiInputTokens: apiMessageTokens,
				requestInputTokens,
				requestOverheadTokens,
				afterMessageTokens,
				afterRequestTokens,
				tokensSaved: requestInputTokens - afterRequestTokens,
				utilizationBefore: `${((requestInputTokens / maxInputTokens) * 100).toFixed(1)}%`,
				utilizationAfter: `${((afterRequestTokens / maxInputTokens) * 100).toFixed(1)}%`,
				thresholdTrigger: `${(COMPACTION_TRIGGER_RATIO * 100).toFixed(1)}%`,
				messagesBefore: beforeMessageCount,
				messagesAfter: result.messages.length,
				messagesRemoved: beforeMessageCount - result.messages.length,
			} as Record<string, unknown>);
			context.emitStatusNotice?.(`${noticePrefix}compacted`, {
				kind: statusReason,
				reason: statusReason,
				phase: "completed",
				iteration: context.iteration,
				tokensBefore: requestInputTokens,
				tokensAfter: afterRequestTokens,
				messagesBefore: beforeMessageCount,
				messagesAfter: result.messages.length,
				maxInputTokens,
			});
			captureCompactionExecuted(config.telemetry, {
				ulid: telemetryUlid,
				strategy: executedStrategy,
				mode: effectiveMode,
				messagesBefore: beforeMessageCount,
				messagesAfter: result.messages.length,
				messagesRemoved: beforeMessageCount - result.messages.length,
				tokensBefore: requestInputTokens,
				tokensAfter: afterRequestTokens,
				tokensSaved: requestInputTokens - afterRequestTokens,
				triggerTokens: requestTriggerTokens,
				maxInputTokens,
				thresholdRatio: COMPACTION_TRIGGER_RATIO,
				durationMs,
				// 与其他 TASK 遥测辅助函数（如 captureTaskCompleted、
				// captureToolUsage）使用的字段名保持一致。
				provider: config.providerId,
				modelId: config.modelId,
				...telemetryIdentity,
			});
			if (
				result.budget &&
				(result.budget.actionCount > 0 || result.budget.warningCount > 0)
			) {
				captureCompactionBudgetEmergency(config.telemetry, {
					ulid: telemetryUlid,
					strategy: executedStrategy,
					mode: effectiveMode,
					policyIntent: result.budget.policyIntent,
					actionCount: result.budget.actionCount,
					warningCount: result.budget.warningCount,
					liveTailHandling: result.budget.liveTailHandling,
					provider: config.providerId,
					modelId: config.modelId,
					...telemetryIdentity,
				});
				context.emitStatusNotice?.("compaction-budget-adjusted", {
					kind: "compaction_budget_emergency",
					reason: "compaction_budget_emergency",
					iteration: context.iteration,
					policyIntent: result.budget.policyIntent,
					actionCount: result.budget.actionCount,
					warningCount: result.budget.warningCount,
				});
			}
		} else {
			context.emitStatusNotice?.(`${noticePrefix}compaction-skipped`, {
				kind: statusReason,
				reason: statusReason,
				phase: "skipped",
				iteration: context.iteration,
				maxInputTokens,
			});
			captureCompactionSkipped(config.telemetry, {
				ulid: telemetryUlid,
				strategy: executedStrategy,
				mode: effectiveMode,
				reason: "no_result",
				tokensBefore: requestInputTokens,
				triggerTokens: requestTriggerTokens,
				maxInputTokens,
				thresholdRatio: COMPACTION_TRIGGER_RATIO,
				durationMs,
				provider: config.providerId,
				modelId: config.modelId,
				...telemetryIdentity,
			});
		}

		return result;
	};
}

/**
 * 用于恢复从其他编码 agent 导入的会话的压缩策略。
 * 导入的转录逐字保留了那个 agent 的工具名与输入 schema，
 * 继续该会话的模型可能会尝试调用这些工具，因此在模型请求之前，
 * 第一轮会把整段外来历史折叠为摘要（手动模式、agentic 策略，
 * 除新提示词外不保留任何内容）。无论会话的自动压缩设置如何都会运行；
 * 其状态通知带有 `importedFrom` 标签，便于客户端标注等待原因；
 * 失败时回退到原始转录。每次会话启动只尝试一次（中止的尝试不计入），
 * 且一旦工作上下文已经以压缩摘要开头（续接的 sidecar 即以此方式呈现）
 * 便不再介入；其余每一轮都交给 `next`，即会话正常的压缩（若有）。
 */
export function createImportedHistoryCompactionPrepareTurn(input: {
	config: Parameters<typeof createContextCompactionPrepareTurn>[0];
	/** 会话 `importedFrom` 元数据中的来源工具 id。 */
	importedFrom: string;
	next?: ContextPipelinePrepareTurn;
}): ContextPipelinePrepareTurn {
	// 透传实时配置（而非副本），使摘要使用恢复轮次上
	// 当前生效的凭据与模型。
	const summarize = createContextCompactionPrepareTurn(input.config, {
		mode: "manual",
		compaction: { enabled: true, strategy: "agentic", preserveRecentTokens: 0 },
	});
	let pending = summarize !== undefined;
	return async (context) => {
		if (pending && summarize && findLatestSummaryIndex(context.messages) < 0) {
			try {
				const result = await summarize({
					...context,
					emitStatusNotice: (message, metadata) =>
						context.emitStatusNotice?.(message, {
							...metadata,
							importedFrom: input.importedFrom,
						}),
				});
				pending = false;
				if (result?.messages) return result;
			} catch (error) {
				if (context.abortSignal.aborted) throw error;
				pending = false;
				input.config.logger?.log(
					"Failed to summarize imported session on resume; continuing with the raw transcript",
					{
						severity: "warn",
						sessionId: input.config.sessionId,
						...describeCompactionError(error),
					},
				);
			}
		}
		return input.next?.(context);
	};
}

export function createCompactionStateAwarePrepareTurn(input: {
	compact?: ContextPipelinePrepareTurn;
	getState?: () => SessionCompactionState | undefined;
	/**
	 * 持久化新计算出的压缩状态。`sourceMessages` 是该状态的源前缀哈希
	 * 所依据的精确规范消息；宿主必须针对这些消息校验投影，
	 * 而不是另行推导的转录——后者在轮次进行中可能合法地不同，
	 * 从而错误地拒绝写入。
	 */
	saveState?: (
		state: SessionCompactionState,
		sourceMessages: CoreCompactionContext["messages"],
	) => void | Promise<void>;
}): ContextPipelinePrepareTurn {
	return async (context) => {
		const existingState = input.getState?.();
		const projectedMessages = existingState
			? projectSessionCompactionState(existingState, context.messages)
			: undefined;
		if (existingState && projectedMessages) {
			// 重新压缩有意从压缩投影加上规范尾部开始。这样自动轮次保持有界，
			// 无需每轮都重建全转录摘要；需要从规范历史生成全新摘要时，
			// 应走手动 `/compact` 路径。
			const result = input.compact
				? await input.compact({
						...context,
						messages: projectedMessages,
						apiMessages: projectedMessages,
					})
				: undefined;
			if (result?.messages) {
				const systemPrompt = result.systemPrompt ?? existingState.system_prompt;
				const nextState = createSessionCompactionState({
					sourceMessages: context.messages,
					compactedMessages: result.messages,
					conversationId: context.conversationId,
					systemPrompt,
				});
				await input.saveState?.(nextState, context.messages);
				return {
					...result,
					...(systemPrompt !== undefined ? { systemPrompt } : {}),
				};
			}
			return {
				messages: projectedMessages,
				...(result?.systemPrompt !== undefined
					? { systemPrompt: result.systemPrompt }
					: existingState.system_prompt !== undefined
						? { systemPrompt: existingState.system_prompt }
						: {}),
			};
		}
		const result = input.compact ? await input.compact(context) : undefined;
		if (result?.messages) {
			const nextState = createSessionCompactionState({
				sourceMessages: context.messages,
				compactedMessages: result.messages,
				conversationId: context.conversationId,
				systemPrompt: result.systemPrompt,
			});
			await input.saveState?.(nextState, context.messages);
		}
		return result;
	};
}
