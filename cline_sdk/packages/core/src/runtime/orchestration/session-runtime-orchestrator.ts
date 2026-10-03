/**
 * 每会话的 `SessionRuntime` 编排器。
 *
 * 拥有一个逻辑 agent 会话的所有跨轮次状态：
 *
 *   - `ConversationStore`      — 消息记录 + 会话启动门
 *   - `MistakeTracker`         — 每会话连续错误计数器
 *   - `LoopDetectionTracker`   — 每会话重复工具调用检测器
 *   - `MessageBuilder`         — provider 消息组装缓存
 *   - `AgentRuntimeHooks`      — 来自配置/扩展的运行时原生 hooks
 *   - `RuntimeEventAdapter`    — 每轮有状态的 `AgentRuntimeEvent`
 *                                → 遗留 `AgentEvent` 转换器
 *   - listener registry        — 宿主订阅者接收遗留 `AgentEvent`
 *   - pending tool set, abort  — 每轮生命周期内务
 *
 * 每次运行都会通过
 * `createAgentRuntime(createAgentRuntimeConfig({...}))` 实例化一个全新的 `AgentRuntime`。
 * 所有会话级状态都比任何单个 `AgentRuntime` 更长寿，使
 * OAuth 重试和运行重放成为可能。
 */

import type { AgentRuntime } from "@cline/agents";
import { createAgentRuntime } from "@cline/agents";
import {
	type AgentConfig,
	type AgentEvent,
	type AgentExtension,
	type AgentExtensionRegistry,
	type AgentExtensionRule,
	type AgentFinishReason,
	type AgentMessage,
	type AgentResult,
	type AgentRunResult,
	type AgentRuntimeEvent,
	type AgentRuntimeHooks,
	type AgentRuntimePrepareTurnContext,
	type AgentTool,
	type BasicLogger,
	type ContributionRegistry,
	createContributionRegistry,
	type ITelemetryService,
	isLikelyAuthError,
	type LegacyAgentUsage,
	type LoopDetectionConfig,
	type Message,
	type MessageWithMetadata,
	type ModelInfo,
	mergeModelOptions,
	modelSupportsImageInput,
	modelSupportsToolCalling,
	type ToolCallRecord,
	usesImageGenerationOperation,
} from "@cline/shared";
import { filterDisabledTools } from "../../services/global-settings";
import {
	createAgentModelFromConfig,
	resolveKnownModelsFromConfig,
} from "../../services/llms/handler-factory";
import {
	captureAuthRunRetry,
	captureMistakeLimitReached,
	captureSessionErrorRecorded,
} from "../../services/telemetry/core-events";
import {
	getMessageBuilderOptionsFromEnv,
	MessageBuilder,
} from "../../session/services/message-builder";
import { ConversationStore } from "../../session/stores/conversation-store";
import {
	agentMessagesToMessages,
	agentMessagesToMessagesWithMetadata,
	messagesToAgentMessages,
} from "../config/agent-message-codec";
import { createAgentRuntimeConfig } from "../config/agent-runtime-config-builder";
import {
	type ConnectionUpdate,
	normalizeConnectionUpdate,
} from "../config/connection-update";
import { LoopDetectionTracker } from "../safety/loop-detection";
import { MistakeTracker } from "../safety/mistake-tracker";
import { RuntimeEventAdapter } from "./runtime-event-adapter";

export const SESSION_RUN_IN_PROGRESS_ERROR_CODE = "session_run_in_progress";

/**
 * 会话在其某个运行仍在进行中且未请求中止的情况下被要求关闭。
 *
 * 携带一个 code，使调用方在其穿过 hub 的 JSON 边界后仍能结构化地识别它，
 * 因为 `Error` 到达时只是一个裸消息。Connectors 用它
 * 区分"该线程的会话不可用"与真正的运行失败，
 * 并通过启动全新会话来恢复，而不是卡死线程。
 */
export class SessionRunInProgressError extends Error {
	readonly code = SESSION_RUN_IN_PROGRESS_ERROR_CODE;

	constructor(readonly agentId?: string) {
		super(
			`SessionRuntime.shutdown called while a run is in progress${
				agentId ? ` (agentId=${agentId})` : ""
			}`,
		);
		this.name = "SessionRunInProgressError";
	}
}

function formatToolResultError(output: unknown): string {
	if (typeof output === "string") {
		return output;
	}
	if (output instanceof Error) {
		return output.message;
	}
	try {
		return JSON.stringify(output);
	} catch {
		return String(output);
	}
}

async function resolveRuleContent(
	rule: AgentExtensionRule,
): Promise<string | undefined> {
	const content =
		typeof rule.content === "function" ? await rule.content() : rule.content;
	const trimmed = content.trim();
	return trimmed.length > 0 ? trimmed : undefined;
}

function mergeSystemPromptRules(
	systemPrompt: string,
	rules: ReadonlyArray<string>,
): string {
	const base = systemPrompt.trim();
	const additional = rules
		.map((rule) => rule.trim())
		.filter(Boolean)
		.join("\n\n");
	if (base && additional) {
		return `${base}\n\n${additional}`;
	}
	return base || additional;
}

function isToolEnabledByPolicies(
	toolName: string,
	toolPolicies: AgentConfig["toolPolicies"],
): boolean {
	const globalPolicy = toolPolicies?.["*"] ?? {};
	const toolPolicy = toolPolicies?.[toolName] ?? {};
	return (
		{
			...globalPolicy,
			...toolPolicy,
		}.enabled !== false
	);
}

function filterToolsByPolicies(
	tools: AgentTool[],
	toolPolicies: AgentConfig["toolPolicies"],
): AgentTool[] {
	return tools.filter((tool) =>
		isToolEnabledByPolicies(tool.name, toolPolicies),
	);
}

function filterAvailableExtensionTools(
	tools: AgentTool[],
	toolPolicies: AgentConfig["toolPolicies"],
): AgentTool[] {
	return filterDisabledTools(filterToolsByPolicies(tools, toolPolicies));
}

function mergeRuntimeHooks(
	layers: Array<Partial<AgentRuntimeHooks> | undefined>,
): Partial<AgentRuntimeHooks> {
	const hooks = layers.filter(
		(layer): layer is Partial<AgentRuntimeHooks> => layer !== undefined,
	);
	if (hooks.length === 0) {
		return {};
	}

	return {
		beforeRun: async (ctx) => {
			let aggregate:
				| Awaited<ReturnType<NonNullable<AgentRuntimeHooks["beforeRun"]>>>
				| undefined;
			for (const hook of hooks) {
				const result = await hook.beforeRun?.(ctx);
				if (!result) continue;
				if (result.stop) return result;
				const appendContext = [aggregate?.appendContext, result.appendContext]
					.filter((value): value is string => Boolean(value?.trim()))
					.join("\n\n");
				aggregate = {
					...aggregate,
					...result,
					appendContext: appendContext || undefined,
				};
			}
			return aggregate;
		},
		afterRun: async (ctx) => {
			for (const hook of hooks) {
				await hook.afterRun?.(ctx);
			}
		},
		beforeModel: async (ctx) => {
			let request = ctx.request;
			let aggregate:
				| Awaited<ReturnType<NonNullable<AgentRuntimeHooks["beforeModel"]>>>
				| undefined;
			for (const hook of hooks) {
				const result = await hook.beforeModel?.({ ...ctx, request });
				if (!result) continue;
				if (result.stop) return result;
				aggregate = {
					...aggregate,
					...result,
					options: mergeModelOptions(aggregate?.options, result.options),
				};
				request = {
					...request,
					...(result.messages ? { messages: result.messages } : {}),
					...(result.tools ? { tools: result.tools } : {}),
					...(result.options
						? { options: mergeModelOptions(request.options, result.options) }
						: {}),
				};
			}
			return aggregate;
		},
		afterModel: async (ctx) => {
			for (const hook of hooks) {
				const result = await hook.afterModel?.(ctx);
				if (result?.stop) return result;
			}
			return undefined;
		},
		beforeTool: async (ctx) => {
			let input = ctx.input;
			let aggregate:
				| Awaited<ReturnType<NonNullable<AgentRuntimeHooks["beforeTool"]>>>
				| undefined;
			for (const hook of hooks) {
				const result = await hook.beforeTool?.({ ...ctx, input });
				if (!result) continue;
				if (result.stop || result.skip) return result;
				aggregate = { ...aggregate, ...result };
				if (Object.hasOwn(result, "input")) {
					input = result.input;
				}
			}
			return aggregate;
		},
		afterTool: async (ctx) => {
			let result = ctx.result;
			let aggregate:
				| Awaited<ReturnType<NonNullable<AgentRuntimeHooks["afterTool"]>>>
				| undefined;
			for (const hook of hooks) {
				const next = await hook.afterTool?.({ ...ctx, result });
				if (!next) continue;
				if (next.stop) return next;
				aggregate = { ...aggregate, ...next };
				if (next.result) {
					result = next.result;
				}
			}
			return aggregate;
		},
		onEvent: async (event) => {
			for (const hook of hooks) {
				await hook.onEvent?.(event);
			}
		},
	};
}

// =============================================================================
// 公共类型
// =============================================================================

/**
 * 为会话运行时产生的每个遗留 `AgentEvent` 调用的监听器。
 * 使用 `subscribeEvents(listener)`——它返回一个
 * `unsubscribe` 函数。
 */
export type SessionEventListener = (event: AgentEvent) => void;

/** 会话编排器所需的宿主侧依赖子集。 */
export interface SessionRuntimeOrchestratorDeps {
	readonly logger?: BasicLogger;
	readonly telemetry?: ITelemetryService;
	/**
	 * 测试钩子：覆盖 `AgentRuntime` 工厂。生产
	 * 调用方将其留为 undefined，得到真实的 `createAgentRuntime`。
	 */
	readonly createAgentRuntimeImpl?: (
		config: Parameters<typeof createAgentRuntime>[0],
	) => AgentRuntime;
}

/** 通过 `updateConnection` 应用的连接覆盖。 */
export type ConnectionOverrides = ConnectionUpdate;

// =============================================================================
// SessionRuntime 编排器
// =============================================================================

/**
 * 每会话编排器。每个 agent 会话构造一次；重复调用
 * `run` / `continue`。该类匹配面向运行时的
 * 会话表面的子集。
 */
export class SessionRuntime {
	private config: AgentConfig;
	private readonly agentId: string;
	private readonly parentAgentId?: string;
	private readonly logger?: BasicLogger;
	// §3.4.4 遥测对齐。当前由 MistakeTracker 的
	// `onLimitTelemetry` 钩子消费（task.mistake_limit_reached）；其他
	// 大多数运行时遥测从 agent 事件流在宿主侧发出
	// （services/agent-events.ts）。
	readonly telemetry?: ITelemetryService;
	private readonly conversation: ConversationStore;
	private pendingTerminalError:
		| Extract<AgentEvent, { type: "error" }>
		| undefined;
	private readonly mistakeTracker: MistakeTracker;
	private readonly loopTracker: LoopDetectionTracker;
	/**
	 * 构造时 `execution.loopDetection === false`，
	 * 循环检查被完全跳过——tracker 仍
	 * 为 API 兼容而存在，但从不被喂入数据。
	 */
	private readonly loopDetectionDisabled: boolean;
	// 宿主拥有的 provider 请求准备。紧接在
	// 模型调用之前运行，使每次循环迭代都看到扩展
	// 消息构建器与 API 安全归一化。
	readonly messageBuilder: MessageBuilder;
	/**
	 * 托管扩展提供的工具、
	 * 命令、消息构建器和 provider 的 contribution registry。在首次
	 * 运行时惰性初始化（与遗留 `Agent.ensureExtensionsInitialized`
	 * 在 `packages/agents/src/agent.ts:1122-1147` 对齐）。
	 */
	private readonly contributionRegistry: ContributionRegistry<
		AgentExtension,
		AgentTool,
		Message[]
	>;
	private extensionsInitialized = false;
	private readonly listeners = new Set<SessionEventListener>();
	private readonly createAgentRuntimeImpl: (
		config: Parameters<typeof createAgentRuntime>[0],
	) => AgentRuntime;

	/** 活动运行的稳定 run id。 */
	private activeRunId: string | null = null;
	/** 运行进行中时为 true。`canStartRun()` 为其取反。 */
	private running = false;
	/** 活动运行已请求 `abort()` 后为 true。 */
	private abortRequested = false;
	/** 为活动运行请求的最后中止原因。 */
	private abortReason: string | undefined;
	/** 当前运行的 `AgentRuntime` 引用，以便 `abort` 转发。 */
	private activeRuntime: AgentRuntime | null = null;
	/** 当前运行返回的 promise，以便 shutdown 等待其排空。 */
	private activeRunPromise: Promise<AgentResult> | null = null;
	/** 每轮 `Agent → AgentEvent` adapter；每次运行 `reset()`。 */
	private readonly eventAdapter = new RuntimeEventAdapter();
	/** 会话关闭门——拒绝迟到的运行。 */
	private shutdownCalled = false;
	/** `AgentResult.toolCalls` 的工具调用记录运行中计数。 */
	private currentRunToolCalls: ToolCallRecord[] = [];
	/** 当前运行聚合的使用量。 */
	private currentRunUsage: LegacyAgentUsage = {
		inputTokens: 0,
		outputTokens: 0,
	};
	/** `ToolCallRecord.durationMs` 的工具启动时间戳。 */
	private toolStartedAt = new Map<string, Date>();
	/** `ToolCallRecord.input` 的工具调用输入快照。 */
	private toolInputs = new Map<string, unknown>();
	/**
	 * MistakeTracker 接线使用的每轮工具结果计数器。
	 * 在每个 `turn-started` 事件时重置；在 `turn-finished` 时消费，
	 * 用于在每个工具调用都出错且没有
	 * 成功调用落地时喂给 `mistakeTracker.record`。与遗留 `agent.ts` 工具失败
	 * 错误喂入路径一致（§3.4.6 + pre-Step-9 oracle lines 972-997）。
	 */
	private currentTurnSuccessfulTools = 0;
	private currentTurnFailedTools = 0;
	private currentTurnFailureDetails: string[] = [];
	/**
	 * `MistakeTracker.record(...)` + 从同步 `handleRuntimeEvent` 流
	 * 触发的循环检测副作用的串行队列。tracker 的
	 * `record()` 是异步的，而运行时事件流是
	 * 同步的，因此我们将 tracker 工作链接到 promise 上，
	 * 并在 `executeRun` 中返回 `AgentResult` 之前 await 它。
	 */
	private activeTrackerWork: Promise<void> = Promise.resolve();
	/** tracker 逻辑已为活动运行发出中止时为 true。 */
	private trackerAbortInFlight = false;
	private readonly handleExternalAbort = (): void => {
		this.abort(this.config.abortSignal?.reason);
	};

	constructor(config: AgentConfig, deps: SessionRuntimeOrchestratorDeps = {}) {
		this.config = config;
		this.agentId = `agent_${Date.now()}_${Math.random()
			.toString(36)
			.slice(2, 8)}`;
		this.parentAgentId = config.parentAgentId;
		this.logger = deps.logger ?? config.logger;
		this.telemetry = deps.telemetry ?? config.telemetry;
		this.createAgentRuntimeImpl =
			deps.createAgentRuntimeImpl ?? createAgentRuntime;

		this.conversation = new ConversationStore(config.initialMessages);
		this.messageBuilder = new MessageBuilder(getMessageBuilderOptionsFromEnv());
		this.contributionRegistry = createContributionRegistry<
			AgentExtension,
			AgentTool,
			Message[]
		>({
			extensions: config.extensions ? [...config.extensions] : [],
			setupContext: {
				session: config.extensionContext?.session,
				client: config.extensionContext?.client,
				user: config.extensionContext?.user,
				workspaceInfo: config.extensionContext?.workspace,
				automation: config.extensionContext?.automation,
				logger: config.extensionContext?.logger ?? this.logger,
				telemetry: config.extensionContext?.telemetry ?? this.telemetry,
			},
		});
		// 急切地 resolve + validate，使 `getExtensionRegistry()`
		// 在首次运行前即可调用（与遗留
		// `Agent` 构造函数在 packages/agents/src/agent.ts:158-159 的行为对齐）。
		// `setup()` 推迟到首次运行时的 `ensureExtensionsInitialized`，
		// 使异步扩展设置不会阻塞构造函数
		// 的执行。
		this.contributionRegistry.resolve();
		this.contributionRegistry.validate();

		const maxMistakes = config.execution?.maxConsecutiveMistakes ?? 6;
		this.mistakeTracker = new MistakeTracker({
			maxConsecutiveMistakes: maxMistakes,
			onLimitReached: config.onConsecutiveMistakeLimitReached,
			onLimitTelemetry: (context) => {
				// 在触发时从 `this.config` 读取连接字段，使
				// 会话中途的 `updateConnection` 反映在事件中。
				captureMistakeLimitReached(this.telemetry, {
					ulid: this.config.sessionId ?? this.conversation.getConversationId(),
					model: this.config.modelId,
					provider: this.config.providerId,
					reason: context.reason,
					consecutiveMistakes: context.consecutiveMistakes,
					maxConsecutiveMistakes: context.maxConsecutiveMistakes,
					agentId: this.agentId,
					conversationId: this.conversation.getConversationId(),
					parentAgentId: this.parentAgentId,
					isSubagent: Boolean(this.parentAgentId),
				});
			},
			emit: (event) => this.emitLegacyEvent(event),
			log: (level, message, metadata) =>
				leveledLog(this.logger, level, message, metadata),
			agentId: this.agentId,
			getConversationId: () => this.conversation.getConversationId(),
			getActiveRunId: () => this.activeRunId ?? "",
			appendRecoveryNotice: (message, _reason) => {
				this.conversation.appendMessage({
					role: "user",
					content: [{ type: "text", text: message }],
				});
			},
		});
		const loopDetectionInput = config.execution?.loopDetection;
		this.loopDetectionDisabled = loopDetectionInput === false;
		const loopConfig: Partial<LoopDetectionConfig> | undefined =
			loopDetectionInput === false || loopDetectionInput === undefined
				? undefined
				: loopDetectionInput;
		this.loopTracker = new LoopDetectionTracker(loopConfig);
	}

	// -------------------------------------------------------------------
	// 访问器与状态变更器
	// -------------------------------------------------------------------

	getAgentId(): string {
		return this.agentId;
	}

	getConversationId(): string {
		return this.conversation.getConversationId();
	}

	getMessages(): MessageWithMetadata[] {
		return this.conversation.getMessages();
	}

	/** 当前无活动运行且会话未关闭时为 true。 */
	canStartRun(): boolean {
		return !this.running && !this.activeRunPromise && !this.shutdownCalled;
	}

	/**
	 * contribution registry 的快照（工具、命令及其他
	 * 扩展贡献）。
	 *
	 * 在首次运行前，registry 处于 `validate` 阶段：
	 * 扩展已通过验证，但其 `setup()` 回调尚未
	 * 运行，因此快照仅反映急切声明的
	 * 贡献。首次 `run()`/`continue()` 之后，
	 * registry 完成初始化（§`ensureExtensionsInitialized`），
	 * 快照将反映扩展通过
	 * `api.registerTool` / `registerCommand` / `registerMessageBuilder`
	 * / `registerProvider` / `registerAutomationEventType` 注册的一切。
	 */
	getExtensionRegistry(): AgentExtensionRegistry<AgentTool, Message[]> {
		return this.contributionRegistry.getRegistrySnapshot();
	}

	/** 将附加工具追加到每个后续轮次的运行时配置。 */
	addTools(tools: AgentTool[]): void {
		if (tools.length === 0) {
			return;
		}
		const existing = new Set(this.config.tools.map((tool) => tool.name));
		const merged = [...this.config.tools];
		for (const tool of tools) {
			if (!existing.has(tool.name)) {
				merged.push(tool);
				existing.add(tool.name);
			}
		}
		this.config = { ...this.config, tools: merged };
	}

	/** 为后续运行变更 provider / reasoning 字段。 */
	updateConnection(overrides: ConnectionOverrides): void {
		const updates = normalizeConnectionUpdate(overrides);
		const next: AgentConfig = { ...this.config };
		if (updates.providerId !== undefined) next.providerId = updates.providerId;
		if (updates.modelId !== undefined) next.modelId = updates.modelId;
		if (updates.apiKey !== undefined) next.apiKey = updates.apiKey;
		if (updates.baseUrl !== undefined) next.baseUrl = updates.baseUrl;
		if (updates.headers !== undefined) next.headers = updates.headers;
		if (updates.providerConfig !== undefined)
			next.providerConfig = updates.providerConfig;
		if (Object.hasOwn(updates, "reasoningEffort")) {
			next.reasoningEffort = updates.reasoningEffort ?? undefined;
		}
		if (Object.hasOwn(updates, "thinkingBudgetTokens")) {
			next.thinkingBudgetTokens = updates.thinkingBudgetTokens ?? undefined;
		}
		if (Object.hasOwn(updates, "thinking")) {
			next.thinking = updates.thinking ?? undefined;
			if (updates.thinking === false || updates.thinking === null) {
				next.reasoningEffort = undefined;
				next.thinkingBudgetTokens = undefined;
			}
		}
		this.config = next;
	}

	clearHistory(): void {
		this.conversation.clearHistory();
		this.resetConversationBoundaryTrackers();
	}

	restore(messages: readonly MessageWithMetadata[]): void {
		this.conversation.restore(messages);
		this.resetConversationBoundaryTrackers();
	}

	private resetConversationBoundaryTrackers(): void {
		this.messageBuilder.resetConversationState();
		this.mistakeTracker.reset();
		this.loopTracker.reset();
	}

	// -------------------------------------------------------------------
	// 事件订阅（遗留形状）
	// -------------------------------------------------------------------

	/**
	 * 订阅**遗留** `AgentEvent`。会话运行时在扇出前
	 * 通过 `RuntimeEventAdapter` 转换新的
	 * `AgentRuntimeEvent` 流，因此消费者看到
	 * 交换前的形状。
	 */
	subscribeEvents(listener: SessionEventListener): () => void {
		this.listeners.add(listener);
		return () => {
			this.listeners.delete(listener);
		};
	}

	// -------------------------------------------------------------------
	// 中止 / 关闭
	// -------------------------------------------------------------------

	notifyPendingUserMessage(): void {
		this.activeRuntime?.notifyPendingUserMessage();
	}

	abort(reason?: unknown): void {
		const message =
			typeof reason === "string"
				? reason
				: reason instanceof Error
					? reason.message
					: reason === undefined
						? undefined
						: String(reason);
		this.abortRequested = true;
		this.abortReason = message;
		if (this.activeRunPromise) {
			/**
			 * 为什么 hub 模式下需要这段逻辑：
			 *
			 * TUI 与运行时并不总是在同一个进程中。在 hub
			 * 模式下，可见的 TUI 通过 websocket 与共享 daemon 通信。当
			 * 用户发送提示词时，TUI 向 daemon 发送一条"启动本次运行"命令。
			 * daemon 启动 AgentRuntime，并将该运行的 promise
			 * 保存为 `activeRunPromise`。
			 *
			 * 如果用户按下 Escape，TUI 会向 daemon 发送另一条"取消
			 * 当前运行"命令。取消运行意味着中止 AgentRuntime。
			 * 这理应中断 provider 流或其他任何进行中的
			 * 异步工作，而这类中断在 JavaScript 中的
			 * 正常表现就是一个被拒绝的 promise。该拒绝本身不是 bug，
			 * 它是用户说"停止这个
			 * 请求"的预期结果。
			 *
			 * 关键细节在于：该拒绝已经被启动该运行的
			 * 代码路径处理了。最初的"启动本次运行"命令
			 * 仍在 await `sessionHost.send(...)`，正是那个 await
			 * 最终应将运行结果或运行错误转换为
			 * 客户端的回复/事件。
			 *
			 * 我们遇到的真正问题是 daemon 进程内部的时序缺口。
			 * 独立的取消命令可能在最初启动命令
			 * 仍在别处等待时调用 `activeRuntime.abort(message)`。abort 会
			 * 立即使 `activeRunPromise` 拒绝。如果运行时在最初启动命令
			 * 观察到该拒绝之前就报告了它，Node/Bun 可能
			 * 短暂地将其归类为 `unhandledRejection`。
			 *
			 * 在 hub daemon 中，`unhandledRejection` 是致命的。通常这是
			 * 正确的策略，因为真正未处理的错误不应被忽略。但
			 * 对于这条取消路径，它意味着 Escape 可能杀死 daemon，即使
			 * 该运行错误是预期之内的、且最初启动命令
			 * 仍负责处理它。daemon 死亡后，下一条
			 * 提示词看起来开始加载，随后却无声地卡住，因为
			 * TUI 正在与一个已死的运行时进程对话。
			 *
			 * 这个 `.catch()` 不是真正的应用级错误处理。它
			 * 只是在触发 abort 之前附加的本地安全观察器，使
			 * daemon 不会把预期内的取消拒绝误认为
			 * 进程崩溃。我们不替换 `activeRunPromise`、不 await 这个 catch，
			 * 也不把拒绝转换为成功。最初的启动命令，以及
			 * 任何其他 await `run()` / `continue()` 的调用方，仍会收到
			 * 与没有这个观察器时相同的结果或错误。
			 */
			void this.activeRunPromise.catch(() => {});
		}
		this.activeRuntime?.abort(message);
	}

	/** 在任何活动运行排空后关闭会话。 */
	async shutdown(_reason?: string, _timeoutMs?: number): Promise<void> {
		if (this.running) {
			if (!this.abortRequested || !this.activeRunPromise) {
				throw new SessionRunInProgressError(this.agentId);
			}
			await this.activeRunPromise;
		}
		if (this.shutdownCalled) {
			return;
		}
		this.shutdownCalled = true;
	}

	// -------------------------------------------------------------------
	// 运行 / 继续
	// -------------------------------------------------------------------

	run(
		userMessage: string,
		userImages?: string[],
		userFiles?: string[],
	): Promise<AgentResult> {
		const rejection = this.getRunAdmissionError();
		if (rejection) return Promise.reject(rejection);
		this.conversation.resetForRun();
		this.resetConversationBoundaryTrackers();
		return this.executeRun({
			userMessage,
			userImages,
			userFiles,
			isContinue: false,
		});
	}

	continue(
		userMessage?: string,
		userImages?: string[],
		userFiles?: string[],
	): Promise<AgentResult> {
		const rejection = this.getRunAdmissionError();
		if (rejection) return Promise.reject(rejection);
		return this.executeRun({
			userMessage,
			userImages,
			userFiles,
			isContinue: true,
		});
	}

	// -------------------------------------------------------------------
	// 私有实现
	// -------------------------------------------------------------------

	private async composeSystemPrompt(
		availableToolNames: ReadonlySet<string>,
	): Promise<string> {
		const rules: string[] = [];
		for (const rule of this.contributionRegistry.getRegisteredRules()) {
			if (
				rule.whenToolAvailable &&
				!availableToolNames.has(rule.whenToolAvailable)
			) {
				continue;
			}
			const content = await resolveRuleContent(rule);
			if (content) {
				rules.push(content);
			}
		}
		return mergeSystemPromptRules(this.config.systemPrompt, rules);
	}

	private getRunAdmissionError(): Error | undefined {
		if (this.shutdownCalled)
			return new Error(
				`SessionRuntime.run called after shutdown (agentId=${this.agentId})`,
			);
		if (this.running || this.activeRunPromise)
			return new Error(
				`SessionRuntime state is "running"; call canStartRun() first (agentId=${this.agentId})`,
			);
		return undefined;
	}

	private executeRun(input: {
		userMessage?: string;
		userImages?: string[];
		userFiles?: string[];
		isContinue: boolean;
	}): Promise<AgentResult> {
		let activePromise!: Promise<AgentResult>;
		activePromise = this.executeRunWithAuthRetry(input)
			.then(
				(result) => {
					if (result.finishReason === "error") {
						this.recordTerminalError(result.text, "result");
						return { ...result, messages: this.conversation.getMessages() };
					}
					this.pendingTerminalError = undefined;
					return result;
				},
				(error: unknown) => {
					this.recordTerminalError(
						error instanceof Error ? error.message : String(error),
						"thrown",
					);
					throw error;
				},
			)
			.finally(() => {
				if (this.activeRunPromise === activePromise) {
					this.activeRunPromise = null;
				}
			});
		this.activeRunPromise = activePromise;
		return activePromise;
	}

	private recordTerminalError(
		message: string,
		source: "result" | "thrown",
	): void {
		this.conversation.appendMessage({
			id: `error_${crypto.randomUUID()}`,
			role: "assistant",
			content: [{ type: "text", text: message }],
			ts: Date.now(),
			metadata: { displayOnly: true, displayRole: "error" },
			modelInfo: { id: this.config.modelId, provider: this.config.providerId },
		});
		const event = this.pendingTerminalError;
		this.pendingTerminalError = undefined;
		this.emitLegacyEvent(
			event?.error.message === message
				? event
				: {
						type: "error",
						error: new Error(message),
						recoverable: false,
						iteration: 0,
					},
		);
		// 统计终端可见失败时，不收集 provider 错误文本、
		// 提示词、凭据或记录内容。
		try {
			captureSessionErrorRecorded(this.telemetry, {
				sessionId: this.config.sessionId,
				provider: this.config.providerId,
				model: this.config.modelId,
				source,
			});
		} catch {
			// 遥测不得阻止记录被返回/保存。
		}
	}

	/**
	 * 当运行以类鉴权错误失败、且宿主通过
	 * `config.onAuthError` 刷新了凭据时，重试一次运行。失败尝试的
	 * 轨迹已持久化到 conversation store，因此重试
	 * 从流中断处继续，而不是重放整个运行。
	 */
	private async executeRunWithAuthRetry(input: {
		userMessage?: string;
		userImages?: string[];
		userFiles?: string[];
		isContinue: boolean;
	}): Promise<AgentResult> {
		const result = await this.executeRunInternal(input);
		if (
			result.finishReason !== "error" ||
			!this.config.onAuthError ||
			!isLikelyAuthError(result.text)
		) {
			return result;
		}
		const refreshed = await this.config.onAuthError().catch(() => false);
		if (!refreshed) {
			return result;
		}
		const retryResult = await this.executeRunInternal({ isContinue: true });
		captureAuthRunRetry(this.telemetry, this.config.providerId, {
			recovered: retryResult.finishReason !== "error",
		});
		return retryResult;
	}

	private async executeRunInternal(input: {
		userMessage?: string;
		userImages?: string[];
		userFiles?: string[];
		isContinue: boolean;
	}): Promise<AgentResult> {
		if (this.shutdownCalled) {
			throw new Error(
				`SessionRuntime.run called after shutdown (agentId=${this.agentId})`,
			);
		}
		if (this.running) {
			throw new Error(
				`SessionRuntime state is "running"; call canStartRun() first (agentId=${this.agentId})`,
			);
		}
		this.running = true;
		this.abortRequested = false;
		this.abortReason = undefined;
		this.activeRunId = `run_${Date.now()}_${Math.random()
			.toString(36)
			.slice(2, 8)}`;
		// 在首次运行时、运行时构建之前，
		// 惰性初始化 contribution-registry 扩展。
		await this.ensureExtensionsInitialized();
		this.eventAdapter.reset();
		this.currentRunToolCalls = [];
		this.currentRunUsage = { inputTokens: 0, outputTokens: 0 };
		this.toolStartedAt.clear();
		this.toolInputs.clear();
		this.currentTurnSuccessfulTools = 0;
		this.currentTurnFailedTools = 0;
		this.currentTurnFailureDetails = [];
		this.activeTrackerWork = Promise.resolve();
		this.trackerAbortInFlight = false;

		const startedAt = new Date();
		const effectiveUserMessage = input.userMessage;

		// 将用户轮次（如有）追加到 conversation store。这
		// 必须发生在下方快照 `initialMessages` 之前，使
		// 运行时将用户消息视为其种子的一部分——随后我们
		// 向 `runtime.run()` 传入空输入，使运行时不会
		// 第二次追加该消息（AgentRuntime.execute 将
		// 假值输入视为"无额外消息"，参见
		// packages/agents/src/agent-runtime.ts 的 normalizeInput 路径）。
		if (effectiveUserMessage !== undefined) {
			const content = await buildUserTurnContent(
				effectiveUserMessage,
				input.userImages,
				input.userFiles,
				this.config.userFileContentLoader,
			);
			this.conversation.appendMessage({
				id: crypto.randomUUID(),
				role: "user",
				content,
			});
		}

		// 为本轮次构建 AgentRuntime。
		const agentModel = createAgentModelFromConfig(
			this.config,
			this.logger,
			this.telemetry,
		);
		// 将扩展贡献的工具与配置声明的工具合并用于
		// 本轮次。扩展在 `setup()` 期间通过
		// `api.registerTool` 注册工具——与 pre-Step-9 `agent.ts:1140-1146`
		// 处的遗留 `Agent.ensureExtensionsInitialized` 一致，
		// 后者将 `this.contributionRegistry.getRegisteredTools()`
		// 合并进 `this.config.tools`。按名称去重，使配置工具
		// 优先于同名的扩展工具（遗留行为：
		// `validateTools` 拒绝重复项；这里我们偏好
		// 显式声明的配置工具）。
		const extensionToolsByName = new Map<string, AgentTool>();
		for (const tool of this.contributionRegistry.getRegisteredTools()) {
			extensionToolsByName.set(tool.name, tool);
		}
		const extensionTools = filterAvailableExtensionTools(
			[...extensionToolsByName.values()],
			this.config.toolPolicies,
		);
		const mergedToolsByName = new Map<string, AgentTool>();
		for (const tool of extensionTools) {
			mergedToolsByName.set(tool.name, tool);
		}
		for (const tool of this.config.tools) {
			mergedToolsByName.set(tool.name, tool);
		}
		const conversationId = this.conversation.getConversationId();
		const modelInfo = tryGetModelInfo(this.config);
		const dedicatedImageGeneration = usesImageGenerationOperation(
			modelInfo ?? {},
		);
		const toolCallingDisabled =
			dedicatedImageGeneration || !modelSupportsToolCalling(modelInfo ?? {});
		const availableTools = filterAvailableExtensionTools(
			Array.from(mergedToolsByName.values()),
			this.config.toolPolicies,
		);
		const tools = toolCallingDisabled ? [] : availableTools;
		const systemPrompt = await this.composeSystemPrompt(
			new Set(tools.map((tool) => tool.name)),
		);
		// 用完整的先前记录（包括刚追加的用户消息）为
		// initialMessages 播种，使多轮历史跨运行
		// 保留。修复 P1 #1：先前的轮次被静默
		// 丢失，因为 `createAgentRuntimeConfig` 未收到种子，
		// 而下游的 `replaceMessages(runResult.messages)` 会用仅含
		// 当前轮次的轨迹覆盖会话。
		const initialMessages = messagesToAgentMessages(
			this.conversation.getMessages(),
		);
		const runtimeConfig = createAgentRuntimeConfig({
			agentConfig: this.config,
			sessionId: this.config.sessionId,
			agentId: this.agentId,
			conversationId,
			parentAgentId: this.parentAgentId,
			model: agentModel,
			logger: this.logger,
			telemetry: this.telemetry,
			tools,
			toolContextMetadata: {
				modelSupportsImages: modelSupportsImageInput(modelInfo ?? {}),
				...this.config.toolContextMetadata,
			},
			hooks: this.createRuntimeHooks(),
			prepareTurn: this.createRuntimePrepareTurn(modelInfo, tools),
			initialMessages,
			completionPolicy: toolCallingDisabled ? null : undefined,
			systemPrompt,
		});
		const runtime = this.createAgentRuntimeImpl(runtimeConfig);
		this.activeRuntime = runtime;

		// 订阅运行时事件；将遗留事件扇出到监听器，
		// 并为工具调用记录 / 使用量保留私有簿记。
		const unsubscribe = runtime.subscribe((event: AgentRuntimeEvent) => {
			// AgentRuntime 在 run-started 之前不接受 abort()。保留启动期间
			// 请求的中止，并在那个既有的生命周期边界转发它，
			// 而不是新增第二条初始化取消路径。
			if (event.type === "run-started" && this.abortRequested) {
				runtime.abort(this.abortReason);
			}
			this.handleRuntimeEvent(event);
		});
		if (this.config.abortSignal) {
			if (this.config.abortSignal.aborted) {
				this.handleExternalAbort();
			} else {
				this.config.abortSignal.addEventListener(
					"abort",
					this.handleExternalAbort,
					{ once: true },
				);
			}
		}

		let runResult: AgentRunResult | undefined;
		let thrownError: Error | undefined;
		try {
			// 传入空输入，使 AgentRuntime 不会重复我们
			// 已通过 `initialMessages` 播种的用户消息。
			// 运行时的 `normalizeInput` 将 `""`/`undefined` 视为
			// "无额外消息"。
			if (input.isContinue) {
				runResult = await runtime.continue(undefined);
			} else {
				runResult = await runtime.run("");
			}
		} catch (error) {
			thrownError = error instanceof Error ? error : new Error(String(error));
		} finally {
			unsubscribe();
			this.config.abortSignal?.removeEventListener(
				"abort",
				this.handleExternalAbort,
			);
			// 在清理状态之前排空所有进行中的 tracker 工作（从
			// handleRuntimeEvent 排队的 mistake/loop 副作用），使
			// 迟到的中止在需要时仍能到达运行时。
			try {
				await this.activeTrackerWork;
			} catch (error) {
				this.logger?.error?.(
					"SessionRuntime tracker work failed during drain",
					{ agentId: this.agentId, error },
				);
			}
			this.activeRuntime = null;
			this.running = false;
			this.abortRequested = false;
			this.abortReason = undefined;
		}

		// 将运行时的消息轨迹持久化回 conversation
		// store，使后续轮次能看到助手输出。运行时状态
		// 已用完整记录播种，因此 `runResult.messages`
		// 就是完整的新记录（种子 + 新产生的轮次）。
		if (runResult && runResult.messages.length > 0) {
			const replacement = agentMessagesToMessagesWithMetadata(
				runResult.messages,
			);
			this.conversation.replaceMessages(replacement);
		}

		const endedAt = new Date();
		try {
			return this.buildLegacyResult({
				runResult,
				thrownError,
				startedAt,
				endedAt,
			});
		} finally {
			this.activeRunId = null;
		}
	}

	/**
	 * 每会话初始化一次 contribution registry。运行
	 * 扩展的 `setup()` 回调，使其可以 `registerTool`、
	 * `registerCommand`、`registerMessageBuilder` 和
	 * `registerProvider`。与 pre-Step-9 `agent.ts:1122-1147`
	 * 处的遗留 `Agent.ensureExtensionsInitialized` 一致：
	 *
	 *   - 当 `hookErrorMode === "throw"` 时，setup 失败会向上传播；
	 *   - 否则 setup 失败会通过遗留事件通道发出
	 *     可恢复的 `error` 事件，并使 registry
	 *     停留在部分初始化状态。
	 *
	 * 幂等：registry 激活后，
	 * 后续调用均为 no-op。
	 */
	private async ensureExtensionsInitialized(): Promise<void> {
		if (this.extensionsInitialized) {
			return;
		}
		try {
			await this.contributionRegistry.initialize({
				tolerateSetupErrors: this.config.hookErrorMode !== "throw",
			});
		} catch (error) {
			if (this.config.hookErrorMode === "throw") {
				throw error;
			}
			this.emitLegacyEvent({
				type: "error",
				error: error instanceof Error ? error : new Error(String(error)),
				recoverable: true,
				iteration: 0,
			});
		}
		this.extensionsInitialized = true;
	}

	private createRuntimeHooks(): Partial<AgentRuntimeHooks> {
		const hooks = mergeRuntimeHooks([
			this.config.hooks,
			...this.contributionRegistry
				.getValidatedExtensions()
				.map((extension) => extension.hooks),
		]);
		return {
			...hooks,
			beforeModel: async (ctx) => {
				const control = await hooks.beforeModel?.(ctx);
				if (control?.stop) {
					return control;
				}
				const messages = control?.messages ?? ctx.request.messages;
				const preparedMessages =
					await this.prepareMessagesForModelRequest(messages);
				return {
					...control,
					messages: preparedMessages,
				};
			},
		};
	}

	private createRuntimePrepareTurn(
		modelInfo: ModelInfo | undefined,
		tools: AgentTool[],
	):
		| ((context: AgentRuntimePrepareTurnContext) => Promise<
				| {
						messages?: readonly AgentMessage[];
						systemPrompt?: string;
				  }
				| undefined
		  >)
		| undefined {
		const prepareTurn = this.config.prepareTurn;
		if (!prepareTurn) {
			return undefined;
		}

		return async (context) => {
			const messages = agentMessagesToMessagesWithMetadata(context.messages);
			const apiMessages = await this.prepareProviderMessagesForApi(messages);
			const result = await prepareTurn({
				agentId: context.agentId,
				conversationId:
					context.conversationId ?? this.conversation.getConversationId(),
				parentAgentId: context.parentAgentId ?? null,
				iteration: context.iteration,
				messages,
				apiMessages,
				abortSignal: context.signal ?? new AbortController().signal,
				systemPrompt: context.systemPrompt ?? "",
				tools,
				model: {
					id: this.config.modelId,
					provider: this.config.providerId,
					info: modelInfo,
				},
				overflowRecovery: context.overflowRecovery,
				previousRequestInputTokens: context.previousRequestInputTokens,
				emitStatusNotice: context.emitStatusNotice,
			});
			if (!result) {
				return undefined;
			}
			return {
				...(result.messages
					? { messages: messagesToAgentMessages(result.messages) }
					: {}),
				...(result.systemPrompt !== undefined
					? { systemPrompt: result.systemPrompt }
					: {}),
			};
		};
	}

	private async prepareMessagesForModelRequest(
		messages: readonly AgentMessage[],
	): Promise<AgentMessage[]> {
		const providerMessages = await this.prepareProviderMessagesForApi(
			agentMessagesToMessages(messages),
		);
		return messagesToAgentMessages(providerMessages);
	}

	private async prepareProviderMessagesForApi(
		messages: MessageWithMetadata[],
	): Promise<MessageWithMetadata[]> {
		let providerMessages = messages;
		const messageBuilders =
			this.contributionRegistry.getRegistrySnapshot().messageBuilder;
		for (const builder of messageBuilders) {
			providerMessages = await builder.build(providerMessages);
		}
		return this.messageBuilder.buildForApi(providerMessages);
	}

	private handleRuntimeEvent(event: AgentRuntimeEvent): void {
		// 在转换前跟踪工具调用记录，使计时数据
		// 通过 `AgentResult.toolCalls` 对观察者可用。
		switch (event.type) {
			case "message-added":
			case "assistant-message": {
				this.syncConversationFromRuntimeMessage(event.snapshot.messages, [
					event.message,
				]);
				break;
			}
			case "turn-started": {
				// 重置 MistakeTracker 接线使用的每轮工具结果
				// 计数器。与 pre-Step-9 agent.ts 一致，
				// 后者累加每次迭代的成功/失败计数，
				// 并在轮次边界将其喂给 recordMistake。
				this.currentTurnSuccessfulTools = 0;
				this.currentTurnFailedTools = 0;
				this.currentTurnFailureDetails = [];
				break;
			}
			case "tool-started": {
				this.toolStartedAt.set(event.toolCall.toolCallId, new Date());
				this.toolInputs.set(event.toolCall.toolCallId, event.toolCall.input);
				if (event.toolCall.execution) {
					break;
				}
				// 循环检测检查：连续相同的
				// 工具调用签名会触发 tracker。对"soft"
				// 裁决我们附加恢复提示；对"hard"
				// 裁决我们喂给 mistake tracker，并带上
				// forceAtLimit:true 然后中止。与 pre-Step-9
				// agent.ts L917-954 一致。
				this.inspectLoopForToolCall(
					event.toolCall.toolName,
					event.toolCall.input,
					event.iteration,
				);
				break;
			}
			case "tool-finished": {
				const startedAt = this.toolStartedAt.get(event.toolCall.toolCallId);
				const endedAt = new Date();
				const input = this.toolInputs.get(event.toolCall.toolCallId);
				this.toolStartedAt.delete(event.toolCall.toolCallId);
				this.toolInputs.delete(event.toolCall.toolCallId);
				const resultPart = event.message.content.find(
					(part) => part.type === "tool-result",
				);
				const isError =
					resultPart?.type === "tool-result" && resultPart.isError === true;
				const errorText = isError
					? formatToolResultError(
							resultPart?.type === "tool-result"
								? resultPart.output
								: undefined,
						)
					: undefined;
				const record: ToolCallRecord = {
					id: event.toolCall.toolCallId,
					name: event.toolCall.toolName,
					execution: event.toolCall.execution,
					input,
					output:
						resultPart?.type === "tool-result" ? resultPart.output : undefined,
					error: errorText,
					durationMs:
						startedAt === undefined
							? 0
							: endedAt.getTime() - startedAt.getTime(),
					startedAt: startedAt ?? endedAt,
					endedAt,
				};
				this.currentRunToolCalls.push(record);
				if (event.toolCall.execution) {
					break;
				}
				// MistakeTracker 的每轮成功/失败簿记。
				if (isError) {
					this.currentTurnFailedTools += 1;
					if (errorText) {
						this.currentTurnFailureDetails.push(
							`[${event.toolCall.toolName}] ${errorText}`,
						);
					}
				} else {
					this.currentTurnSuccessfulTools += 1;
				}
				break;
			}
			case "turn-finished": {
				// 轮次结束时的 mistake 评估：与遗留行为一致
				// （pre-Step-9 agent.ts L972-997）。当部分工具调用失败且
				// 该轮次没有成功的工具调用时，记录一次 mistake；
				// 在有产出的轮次重置。
				const failed = this.currentTurnFailedTools;
				const succeeded = this.currentTurnSuccessfulTools;
				if (failed > 0 && succeeded === 0) {
					const details = this.currentTurnFailureDetails.join("; ");
					this.enqueueMistakeRecord({
						iteration: event.iteration,
						reason: "tool_execution_failed",
						details: `${failed} tool call(s) failed${
							details ? `: ${details}` : ""
						}`,
					});
				} else if (succeeded > 0) {
					// 有产出的轮次——重置 tracker，使暂时性
					// 失败不会跨不相关的轮次累积。
					this.mistakeTracker.reset();
				}
				break;
			}
			case "usage-updated": {
				this.currentRunUsage = {
					inputTokens: event.usage.inputTokens,
					outputTokens: event.usage.outputTokens,
					cacheReadTokens:
						event.usage.cacheReadTokens > 0
							? event.usage.cacheReadTokens
							: undefined,
					cacheWriteTokens:
						event.usage.cacheWriteTokens > 0
							? event.usage.cacheWriteTokens
							: undefined,
					totalCost: event.usage.totalCost,
				};
				break;
			}
			default:
				break;
		}
		for (const legacy of this.eventAdapter.translate(event)) {
			if (legacy.type === "error" && !legacy.recoverable) {
				// 鉴权重试是内部尝试，不是终端公开失败。
				this.pendingTerminalError = legacy;
				continue;
			}
			this.emitLegacyEvent(legacy);
		}
	}

	private syncConversationFromRuntimeMessage(
		snapshotMessages: readonly AgentMessage[],
		fallbackMessages: readonly AgentMessage[],
	): void {
		if (snapshotMessages.length > 0) {
			this.conversation.replaceMessages(
				agentMessagesToMessagesWithMetadata(snapshotMessages),
			);
			return;
		}
		if (fallbackMessages.length === 0) return;
		const existingIds = new Set(
			this.conversation
				.getMessages()
				.map((message) => message.id)
				.filter((id): id is string => typeof id === "string"),
		);
		const newMessages = agentMessagesToMessagesWithMetadata(
			fallbackMessages,
		).filter((message) => !message.id || !existingIds.has(message.id));
		if (newMessages.length === 0) return;
		this.conversation.replaceMessages([
			...this.conversation.getMessages(),
			...newMessages,
		]);
	}

	private emitLegacyEvent(event: AgentEvent): void {
		for (const listener of this.listeners) {
			try {
				listener(event);
			} catch (error) {
				this.logger?.error?.("SessionRuntime event listener threw", {
					agentId: this.agentId,
					error,
				});
			}
		}
	}

	/**
	 * 向 `LoopDetectionTracker` 喂入工具调用，并对其
	 * 返回的裁决做出反应。与 pre-Step-9 agent.ts L917-954 一致：
	 *
	 *   - `"soft"`  → 附加恢复提示，告诉模型
	 *                 更换方法；
	 *   - `"hard"`  → 喂给 `MistakeTracker.record`，带上
	 *                 `forceAtLimit:true`。当 tracker 返回
	 *                 `action: "stop"` 时，附加停止提示并
	 *                 中止活动运行时。
	 */
	private inspectLoopForToolCall(
		toolName: string,
		input: unknown,
		iteration: number,
	): void {
		if (this.trackerAbortInFlight || this.loopDetectionDisabled) {
			return;
		}
		const verdict = this.loopTracker.inspect({ name: toolName, input });
		if (verdict.kind === "ok") {
			return;
		}
		if (verdict.kind === "soft") {
			if (verdict.message) {
				this.conversation.appendMessage({
					role: "user",
					content: [{ type: "text", text: verdict.message }],
				});
			}
			return;
		}
		// 硬升级。
		this.enqueueMistakeRecord({
			iteration,
			reason: "tool_execution_failed",
			forceAtLimit: true,
			details:
				verdict.message ??
				`Detected repeated tool calls to \`${toolName}\`; stopping to avoid a loop.`,
		});
	}

	/**
	 * 将 mistake-record 排入串行 tracker 工作链。运行时
	 * 事件流是同步的，而 `MistakeTracker.record`
	 * 是异步的——链接到共享 promise 上可保持顺序
	 * （与遗留行为一致），并让 `executeRun` 在返回
	 * `AgentResult` 之前 await 排空完成。
	 *
	 * 当 tracker 返回 `action: "stop"` 时，向会话追加停止提示
	 * 并中止活动运行时，使运行以
	 * `finishReason: "aborted"` 结束。
	 */
	private enqueueMistakeRecord(input: {
		iteration: number;
		reason: "api_error" | "invalid_tool_call" | "tool_execution_failed";
		details?: string;
		forceAtLimit?: boolean;
	}): void {
		if (this.trackerAbortInFlight) {
			return;
		}
		this.activeTrackerWork = this.activeTrackerWork.then(async () => {
			if (this.trackerAbortInFlight) {
				return;
			}
			const outcome = await this.mistakeTracker.record(input);
			if (outcome.action === "stop") {
				this.trackerAbortInFlight = true;
				this.conversation.appendMessage({
					role: "user",
					content: [{ type: "text", text: outcome.message }],
				});
				this.activeRuntime?.abort(outcome.reason ?? outcome.message);
			}
		});
	}

	private buildLegacyResult(input: {
		runResult: AgentRunResult | undefined;
		thrownError: Error | undefined;
		startedAt: Date;
		endedAt: Date;
	}): AgentResult {
		const { runResult, thrownError, startedAt, endedAt } = input;
		const durationMs = endedAt.getTime() - startedAt.getTime();
		const finishReason: AgentFinishReason = thrownError
			? "error"
			: deriveFinishReason(runResult);
		const text =
			(runResult?.status === "failed" ? runResult.error?.message : undefined) ||
			runResult?.outputText ||
			"";
		const usage: LegacyAgentUsage = runResult
			? {
					inputTokens: runResult.usage.inputTokens,
					outputTokens: runResult.usage.outputTokens,
					cacheReadTokens:
						runResult.usage.cacheReadTokens > 0
							? runResult.usage.cacheReadTokens
							: undefined,
					cacheWriteTokens:
						runResult.usage.cacheWriteTokens > 0
							? runResult.usage.cacheWriteTokens
							: undefined,
					totalCost: runResult.usage.totalCost,
				}
			: this.currentRunUsage;
		const messages = this.conversation.getMessages();
		const modelInfo = tryGetModelInfo(this.config);
		if (thrownError) {
			throw thrownError;
		}
		return {
			text,
			usage,
			messages,
			toolCalls: this.currentRunToolCalls,
			iterations: runResult?.iterations ?? 0,
			finishReason,
			model: {
				id: this.config.modelId,
				provider: this.config.providerId,
				info: modelInfo,
			},
			startedAt,
			endedAt,
			durationMs,
		};
	}
}

// =============================================================================
// 模块级辅助函数
// =============================================================================

function leveledLog(
	logger: BasicLogger | undefined,
	level: "debug" | "info" | "warn" | "error",
	message: string,
	metadata?: Record<string, unknown>,
): void {
	if (!logger) {
		return;
	}
	if (level === "debug") {
		logger.debug(message, metadata);
		return;
	}
	if (level === "error" && logger.error) {
		logger.error(message, metadata);
		return;
	}
	const severity: "info" | "warn" | "error" =
		level === "warn" ? "warn" : level === "error" ? "error" : "info";
	logger.log(message, { ...metadata, severity });
}

function deriveFinishReason(
	runResult: AgentRunResult | undefined,
): AgentFinishReason {
	if (!runResult) {
		return "error";
	}
	switch (runResult.status) {
		case "completed":
			return "completed";
		case "aborted":
			return "aborted";
		case "failed":
			return "error";
	}
}

async function buildUserTurnContent(
	userMessage: string,
	userImages: string[] | undefined,
	userFiles: string[] | undefined,
	loader: AgentConfig["userFileContentLoader"],
): Promise<Message["content"]> {
	// 惰性导入，避免经由 runtime barrels 的循环导入风险。
	const { buildInitialUserContent } = await import("./user-input-builder");
	return buildInitialUserContent(userMessage, userImages, userFiles, loader);
}

function tryGetModelInfo(config: AgentConfig): ModelInfo | undefined {
	if (config.knownModels?.[config.modelId]) {
		return config.knownModels[config.modelId];
	}
	const resolvedKnownModels = resolveKnownModelsFromConfig(config);
	if (resolvedKnownModels?.[config.modelId]) {
		return resolvedKnownModels[config.modelId];
	}
	return undefined;
}
