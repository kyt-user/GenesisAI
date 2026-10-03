import type { BasicLogger, ITelemetryService } from "@cline/shared";
import {
	ClineCoreAutomationController,
	createClineCoreAutomationExtensionContext,
	createClineCoreAutomationRuntimeHandlers,
	normalizeAutomationCronScope,
	normalizeAutomationOptions,
} from "./cline-core/automation";
import {
	createClineCorePendingPromptsApi,
	createClineCoreSettingsApi,
	type RuntimeHostServiceExtensions,
} from "./cline-core/runtime-services";
import {
	normalizeClineCoreStartInput,
	toClineCoreStartInput,
} from "./cline-core/start-input";
import { emitSessionStartedTelemetry } from "./cline-core/telemetry";
import type {
	ClineCoreAutomationApi,
	ClineCoreAutomationOptions,
	ClineCoreListHistoryOptions,
	ClineCoreOptions,
	ClineCoreSettingsApi,
	ClineCoreStartInput,
	CompareCheckpointInput,
	CompareCheckpointResult,
	RestoreInput,
	RestoreResult,
	StartSessionBootstrap,
} from "./cline-core/types";

import { CronService } from "./cron/service/cron-service";
import type { RuntimeCapabilities } from "./runtime/capabilities";
import { normalizeRuntimeCapabilities } from "./runtime/capabilities";
import { listSessionHistory } from "./runtime/host/history";
import { createRuntimeHost } from "./runtime/host/host";
import type {
	PendingPromptsServiceApi,
	RuntimeHost,
	RuntimeHostSubscribeOptions,
	SessionConnectionRuntimeService,
	SessionModelRuntimeService,
	SessionUsageRuntimeService,
	StartSessionInput,
	StartSessionResult,
} from "./runtime/host/runtime-host";
import {
	FeatureFlagsService,
	NoOpFeatureFlagsProvider,
} from "./services/feature-flags";
import { resolveCoreDistinctId } from "./services/telemetry/distinct-id";
import { compareCheckpointToWorkspace } from "./session/checkpoint-diff";
import {
	projectSessionMessagesForDisplay,
	type SessionDisplayMessage,
} from "./session/display-messages";
import type { CoreSessionEvent } from "./types/events";
import type { SessionHistoryRecord } from "./types/sessions";

export type {
	ClineAutomationEventIngressResult,
	ClineAutomationEventLog,
	ClineAutomationEventSuppression,
	ClineAutomationListEventsOptions,
	ClineAutomationListRunsOptions,
	ClineAutomationListSpecsOptions,
	ClineAutomationRun,
	ClineAutomationRunStatus,
	ClineAutomationSpec,
	ClineCoreAutomationApi,
	ClineCoreAutomationOptions,
	ClineCoreListHistoryOptions,
	ClineCoreOptions,
	ClineCoreSettingsApi,
	ClineCoreStartInput,
	CompareCheckpointInput,
	CompareCheckpointResult,
	HubOptions,
	RemoteOptions,
	RestoreInput,
	RestoreOptions,
	RestoreResult,
	RuntimeHostMode,
	StartSessionBootstrap,
} from "./cline-core/types";

/**
 * Cline Core SDK 的主入口点。
 *
 * @example
 * ```ts
 * import { ClineCore } from "@cline/core";
 *
 * const cline = await ClineCore.create({ clientName: "my-app" });
 * const session = await cline.start({ ... });
 * ```
 */
export class ClineCore {
	readonly clientName: string | undefined;
	readonly runtimeAddress: string | undefined;
	readonly automation: ClineCoreAutomationApi;
	readonly settings: ClineCoreSettingsApi;
	readonly featureFlags: FeatureFlagsService;
	readonly pendingPrompts: PendingPromptsServiceApi;
	private readonly host: RuntimeHost;
	private readonly prepare: ClineCoreOptions["prepare"] | undefined;
	private readonly capabilities: RuntimeCapabilities | undefined;
	private readonly logger: BasicLogger | undefined;
	private readonly telemetry: ITelemetryService | undefined;
	private readonly distinctId: string | undefined;
	private readonly automationService: CronService | undefined;
	private readonly activeSessionBootstraps = new Map<
		string,
		StartSessionBootstrap
	>();
	private readonly unsubscribeBootstrapCleanup: () => void;

	private constructor(
		host: RuntimeHost,
		clientName: string | undefined,
		runtimeAddress: string | undefined,
		prepare: ClineCoreOptions["prepare"],
		capabilities: RuntimeCapabilities | undefined,
		logger: BasicLogger | undefined,
		telemetry: ITelemetryService | undefined,
		distinctId: string | undefined,
		featureFlags: FeatureFlagsService,
		automationOptions:
			| (ClineCoreAutomationOptions & { logger?: BasicLogger })
			| undefined,
	) {
		this.clientName = clientName;
		this.runtimeAddress = runtimeAddress;
		this.host = host;
		this.prepare = prepare;
		this.capabilities = capabilities;
		this.logger = logger;
		this.telemetry = telemetry;
		this.distinctId = distinctId;
		this.featureFlags = featureFlags;
		this.settings = createClineCoreSettingsApi(host);
		this.pendingPrompts = createClineCorePendingPromptsApi(host);
		this.automation = new ClineCoreAutomationController(() => {
			if (!this.automationService) {
				throw new Error(
					"ClineCore automation is not enabled. Pass `automation: true` or automation options to ClineCore.create().",
				);
			}
			return this.automationService;
		});
		this.automationService = automationOptions
			? new CronService({
					workspaceRoot: automationOptions.workspaceRoot ?? process.cwd(),
					specs: {
						cronSpecsDir:
							automationOptions.cronSpecsDir ?? automationOptions.cronDir,
						scope: normalizeAutomationCronScope(automationOptions.cronScope),
						workspaceRoot: automationOptions.workspaceRoot,
					},
					runtimeHandlers: createClineCoreAutomationRuntimeHandlers({
						host,
						getExtensionContext: () =>
							createClineCoreAutomationExtensionContext({
								automationService: this.automationService,
								automation: this.automation,
								clientName: this.clientName,
								distinctId: this.distinctId,
								logger: this.logger,
								telemetry: this.telemetry,
							}),
					}),
					dbPath: automationOptions.dbPath,
					logger: automationOptions.logger,
					telemetry: this.telemetry,
					pollIntervalMs: automationOptions.pollIntervalMs,
					claimLeaseSeconds: automationOptions.claimLeaseSeconds,
					globalMaxConcurrency: automationOptions.globalMaxConcurrency,
					watcherDebounceMs: automationOptions.watcherDebounceMs,
				})
			: undefined;
		this.unsubscribeBootstrapCleanup = this.host.subscribe((event) => {
			if (event.type !== "ended") {
				return;
			}
			void this.disposeSessionBootstrap(event.payload.sessionId);
		});
	}

	/**
	 * 创建一个新的 ClineCore 实例。
	 *
	 * 这是初始化 SDK 的主工厂方法。它根据提供的选项装配运行时宿主
	 * （local、hub 或 remote），并让 SDK 准备好启动会话。
	 *
	 * @param options SDK 实例的配置选项
	 * @returns 解析为新 ClineCore 实例的 Promise
	 *
	 * @example
	 * ```ts
	 * const cline = await ClineCore.create({
	 *   clientName: "my-app",
	 *   backendMode: "local",
	 * });
	 * ```
	 */
	static async create(options: ClineCoreOptions = {}): Promise<ClineCore> {
		const distinctId = resolveCoreDistinctId(options.distinctId);
		const capabilities = normalizeRuntimeCapabilities(options.capabilities);
		const normalizedOptions = { ...options, capabilities, distinctId };
		const host = await createRuntimeHost(normalizedOptions);
		const automationOptions = normalizeAutomationOptions(options.automation);
		const featureFlags =
			options.featureFlags ||
			new FeatureFlagsService({
				provider: new NoOpFeatureFlagsProvider(),
				telemetry: options.telemetry,
				logger: options.logger,
				context: {
					distinctId,
					clientName: options.clientName,
				},
			});
		const core = new ClineCore(
			host,
			options.clientName,
			host.runtimeAddress,
			options.prepare,
			capabilities,
			options.logger,
			options.telemetry,
			distinctId,
			featureFlags,
			automationOptions
				? { ...automationOptions, logger: options.logger }
				: undefined,
		);
		if (automationOptions && automationOptions.autoStart !== false) {
			await core.automation.start();
		}
		return core;
	}

	private async disposeSessionBootstrap(sessionId: string): Promise<void> {
		const bootstrap = this.activeSessionBootstraps.get(sessionId);
		if (!bootstrap) {
			return;
		}
		this.activeSessionBootstraps.delete(sessionId);
		await Promise.resolve(bootstrap.dispose?.());
	}

	/**
	 * 用提供的配置启动一个新的 Cline 会话。
	 *
	 * 此方法初始化并开始一个新的 Agent 会话。它处理会话设置、运行所有准备
	 * hook，并返回会话元数据与事件流。会话会持续运行，直到被显式停止或中止。
	 *
	 * @param input 会话配置与启动参数
	 * @returns 解析为会话元数据与事件流的 Promise
	 *
	 * @example
	 * ```ts
	 * const result = await cline.start({
	 *   config: {
	 *     providerId: "anthropic",
	 *     modelId: "claude-opus-4-1",
	 *   },
	 * });
	 *
	 * // 订阅会话事件
	 * result.subscribe((event) => {
	 *   console.log("Session event:", event);
	 * });
	 * ```
	 */
	start(input: StartSessionInput): Promise<StartSessionResult>;
	/**
	 * 用扩展的 core 特有配置启动一个新的 Cline 会话。
	 * 该重载允许指定本地运行时选项与配置覆盖。
	 */
	start(input: ClineCoreStartInput): Promise<StartSessionResult>;
	async start(
		input: StartSessionInput | ClineCoreStartInput,
	): Promise<StartSessionResult> {
		const clineCoreInput = toClineCoreStartInput(input);
		const bootstrap = await this.prepare?.(clineCoreInput);
		try {
			const preparedInput = bootstrap
				? await bootstrap.applyToStartSessionInput(clineCoreInput)
				: clineCoreInput;
			const result = await this.host.startSession(
				normalizeClineCoreStartInput(preparedInput, {
					defaultCapabilities: this.capabilities,
					withExtensionContext: (context) =>
						createClineCoreAutomationExtensionContext({
							automationService: this.automationService,
							automation: this.automation,
							context,
							clientName: this.clientName,
							distinctId: this.distinctId,
							logger: this.logger,
							telemetry: this.telemetry,
						}),
				}),
			);
			if (bootstrap) {
				const activeSession = await this.host.getSession(result.sessionId);
				if (activeSession) {
					this.activeSessionBootstraps.set(result.sessionId, bootstrap);
				} else {
					await Promise.resolve(bootstrap.dispose?.());
				}
			}
			emitSessionStartedTelemetry({
				input: preparedInput,
				sessionId: result.sessionId,
				telemetry: this.telemetry,
				clientName: this.clientName,
				runtimeAddress: this.runtimeAddress,
			});
			return result;
		} catch (error) {
			await Promise.resolve(bootstrap?.dispose?.());
			throw error;
		}
	}
	/**
	 * 向活跃会话发送消息或命令。
	 *
	 * 此方法与运行中的会话通信，允许你在会话进行期间发送用户消息、
	 * 工具响应或其他会话输入。
	 *
	 * @example
	 * ```ts
	 * await cline.send(sessionId, {
	 *   type: "user_message",
	 *   text: "Please implement the login feature",
	 * });
	 * ```
	 */
	send: RuntimeHost["runTurn"] = (...args) => this.host.runTurn(...args);
	/**
	 * 获取会话累积的 token 与成本用量。
	 *
	 * 返回会话资源消耗的指标，包括跨不同 API 提供商使用的 token 与相关成本。
	 * `usage` 字段是根/主 Agent 的用量；`aggregateUsage` 包含队友与子代理。
	 *
	 * @example
	 * ```ts
	 * const usageSummary = await cline.getAccumulatedUsage(sessionId);
	 * console.log(`Total cost: $${usageSummary?.aggregateUsage?.totalCost}`);
	 * ```
	 */
	getAccumulatedUsage: SessionUsageRuntimeService["getAccumulatedUsage"] = (
		...args
	) => {
		const service = this.host as RuntimeHostServiceExtensions;
		return service.getAccumulatedUsage?.(...args) ?? Promise.resolve(undefined);
	};
	/**
	 * 中止进行中的工具执行，但不停止会话。
	 *
	 * 打断当前工具操作（例如文件读取、shell 命令）同时保持会话存活。
	 * 会话可以在中止后继续处理。用于取消长时间运行的操作。
	 *
	 * @example
	 * ```ts
	 * // 停止当前操作但保持会话运行
	 * await cline.abort(sessionId);
	 * ```
	 */
	abort: RuntimeHost["abort"] = (...args) => this.host.abort(...args);
	/**
	 * 优雅地停止一个活跃会话。
	 *
	 * 终止会话并清理相关资源。与 abort 不同，这会完全结束会话。
	 * 停止后无法恢复该会话。
	 *
	 * @example
	 * ```ts
	 * // 干净地关闭会话
	 * await cline.stop(sessionId);
	 * ```
	 */
	stop: RuntimeHost["stopSession"] = async (sessionId) => {
		await this.host.stopSession(sessionId);
		await this.disposeSessionBootstrap(sessionId);
	};
	/**
	 * 释放 ClineCore 实例及所有相关资源。
	 *
	 * 关闭运行时宿主、关闭连接，并清理所有活跃会话与引导（bootstrap）。
	 * 在不再使用 SDK 实例时调用，通常是在应用关闭时。调用 dispose 后，
	 * 该实例不可再复用。
	 *
	 * @example
	 * ```ts
	 * // 完成后清理
	 * await cline.dispose();
	 * ```
	 */
	dispose: RuntimeHost["dispose"] = async (...args) => {
		try {
			await this.automationService?.dispose();
			await this.host.dispose(...args);
		} finally {
			this.unsubscribeBootstrapCleanup();
			const sessionIds = [...this.activeSessionBootstraps.keys()];
			await Promise.allSettled(
				sessionIds.map((sessionId) => this.disposeSessionBootstrap(sessionId)),
			);
		}
	};
	/**
	 * 按 ID 获取特定会话的信息。
	 *
	 * 拉取会话的当前元数据与状态，包括配置、状态及其他会话详情。
	 *
	 * @example
	 * ```ts
	 * const session = await cline.get(sessionId);
	 * console.log("Session status:", session?.status);
	 * ```
	 */
	get: RuntimeHost["getSession"] = (...args) => this.host.getSession(...args);
	/**
	 * 通过共享的历史列表路径列出近期会话。
	 */
	listHistory = async (
		options: ClineCoreListHistoryOptions = {},
	): Promise<SessionHistoryRecord[]> =>
		await listSessionHistory(this.host, options);
	/**
	 * 列出近期会话，并推断历史展示元数据。
	 *
	 * 获取分页的近期会话列表，可选择用给定数量限制。
	 *
	 * @param limit 最多返回的会话数（默认 200）
	 * @returns 解析为会话历史记录数组的 Promise
	 *
	 * @example
	 * ```ts
	 * const sessions = await cline.list(50);
	 * sessions.forEach((session) => {
	 *   console.log(`Session ${session.sessionId}: ${session.metadata?.title}`);
	 * });
	 * ```
	 */
	list = async (
		limit = 200,
		options: Omit<ClineCoreListHistoryOptions, "limit"> = {},
	): Promise<SessionHistoryRecord[]> =>
		await this.listHistory({ ...options, limit });
	/**
	 * 永久删除一个会话及其所有关联数据。
	 *
	 * 从存储中移除会话并清理所有相关资源。这是不可撤销的破坏性操作。
	 *
	 * @param sessionId 要删除的会话 ID
	 * @returns 解析为 true（已删除）或 false（未找到）的 Promise
	 *
	 * @example
	 * ```ts
	 * const deleted = await cline.delete(sessionId);
	 * if (deleted) {
	 *   console.log("Session deleted successfully");
	 * }
	 * ```
	 */
	delete: RuntimeHost["deleteSession"] = async (sessionId) => {
		const deleted = await this.host.deleteSession(sessionId);
		if (deleted) {
			await this.disposeSessionBootstrap(sessionId);
		}
		return deleted;
	};
	/**
	 * 更新既有会话的元数据。
	 *
	 * 修改标题等可变元数据，同时保留消息历史与其他会话数据。
	 *
	 * @example
	 * ```ts
	 * await cline.update(sessionId, {
	 *   title: "Updated session title",
	 * });
	 * ```
	 */
	update: RuntimeHost["updateSession"] = (...args) =>
		this.host.updateSession(...args);
	/**
	 * 存储既有会话的压缩工作上下文状态。
	 */
	updateSessionCompactionState: RuntimeHost["updateSessionCompactionState"] = (
		...args
	) => this.host.updateSessionCompactionState(...args);
	/**
	 * 读取会话的压缩工作上下文 sidecar 文件（若存在）。
	 */
	readSessionCompactionState: RuntimeHost["readSessionCompactionState"] = (
		...args
	) => this.host.readSessionCompactionState(...args);
	/**
	 * 读取会话的规范消息历史。
	 *
	 * 这是 resume、fork 与压缩（compaction）使用的模型/回放表示。
	 * 提供商自有的模型工具活动在此保持为观测性元数据。如需把该活动投射为
	 * 普通工具块的 UI transcript，请使用 {@link readDisplayMessages}。
	 *
	 * @example
	 * ```ts
	 * const messages = await cline.readMessages(sessionId);
	 * messages.forEach((msg) => {
	 *   console.log(`${msg.role}: ${msg.content}`);
	 * });
	 * ```
	 */
	readMessages: RuntimeHost["readSessionMessages"] = (...args) =>
		this.host.readSessionMessages(...args);

	/**
	 * 读取为展示而投射的 transcript。观测性的模型工具活动会以与普通本地工具
	 * 相同的工具块呈现。
	 *
	 * resume、fork、压缩或模型回放请使用 {@link readMessages}。
	 */
	async readDisplayMessages(
		sessionId: string,
	): Promise<SessionDisplayMessage[]> {
		return projectSessionMessagesForDisplay(
			await this.host.readSessionMessages(sessionId),
		);
	}

	/**
	 * 读取会话的消息历史，当会话仍驻留在此宿主时优先使用内存中的实时对话。
	 *
	 * 持久化的 transcript 只在助手消息/轮次边界处追平，因此 `readMessages`
	 * 可能错过进行中（或刚刚中止）的轮次。当当前对话很重要时使用它——
	 * 例如在 plan/act 模式切换期间为新会话播种。当会话未驻留或宿主不跟踪
	 * 实时会话时，回退到持久化 transcript。
	 */
	readLiveMessages: RuntimeHost["readSessionMessages"] = (sessionId) =>
		this.host.readLiveSessionMessages
			? this.host.readLiveSessionMessages(sessionId)
			: this.host.readSessionMessages(sessionId);

	async restore(input: RestoreInput): Promise<RestoreResult> {
		const normalizedStart = input.start
			? normalizeClineCoreStartInput(input.start, {
					defaultCapabilities: this.capabilities,
					withExtensionContext: (context) =>
						createClineCoreAutomationExtensionContext({
							automationService: this.automationService,
							automation: this.automation,
							context,
							clientName: this.clientName,
							distinctId: this.distinctId,
							logger: this.logger,
							telemetry: this.telemetry,
						}),
				})
			: undefined;
		return this.host.restoreSession({
			sessionId: input.sessionId,
			checkpointRunCount: input.checkpointRunCount,
			cwd: input.cwd,
			restore: input.restore,
			start: normalizedStart,
		});
	}

	async compareCheckpoint(
		input: CompareCheckpointInput,
	): Promise<CompareCheckpointResult> {
		const sessionId = input.sessionId.trim();
		if (!sessionId) {
			throw new Error("sessionId is required");
		}
		const session = await this.host.getSession(sessionId);
		if (!session) {
			throw new Error(`Session ${sessionId} not found`);
		}
		return compareCheckpointToWorkspace({
			session,
			checkpointRunCount: input.checkpointRunCount,
			cwd: input.cwd,
		});
	}

	/**
	 * 处理来自运行时环境的 hook 事件。
	 *
	 * 处理可能影响当前会话的系统或环境事件（例如工作区变更、外部信号）。
	 * 通常由宿主环境调用，而不是由消费方代码直接调用。
	 *
	 * @internal
	 */
	ingestHookEvent: RuntimeHost["dispatchHookEvent"] = (...args) =>
		this.host.dispatchHookEvent(...args);
	/**
	 * 订阅会话事件。
	 *
	 * 为所有会话事件（消息、状态变更、错误等）注册监听器。
	 * 返回一个取消订阅函数来停止监听。
	 *
	 * @param listener 每个事件触发时调用的回调函数
	 * @param options 订阅的可选配置
	 * @returns 取消订阅函数
	 *
	 * @example
	 * ```ts
	 * const unsubscribe = cline.subscribe((event) => {
	 *   if (event.type === "message") {
	 *     console.log("New message:", event.payload.message);
	 *   }
	 * });
	 *
	 * // 稍后停止监听
	 * unsubscribe();
	 * ```
	 */
	subscribe(
		listener: (event: CoreSessionEvent) => void,
		options?: RuntimeHostSubscribeOptions,
	): () => void {
		return this.host.subscribe(listener, options);
	}
	/**
	 * 本实例是否已订阅某会话的实时事件。
	 *
	 * 在 hub 模式下，ClineCore 会在启动、发送或列出待发 prompt 时订阅会话，
	 * 并在 stop 时取消订阅。同时直接观察 hub 的客户端可以用它来只渲染一份
	 * 会话事件，而不是两份。
	 */
	hasSessionSubscription(sessionId: string): boolean {
		return this.host.hasSessionSubscription?.(sessionId) ?? false;
	}
	/**
	 * 更新活跃会话使用的 AI 模型。
	 *
	 * 把会话切换到另一个 AI 模型，同时保持会话状态与消息历史。
	 * 这让你可以用不同的模型继续对话。
	 *
	 * @example
	 * ```ts
	 * // 会话中途切换到另一个模型
	 * await cline.updateSessionModel(sessionId, "claude-opus-4-1");
	 * ```
	 */
	updateSessionModel: SessionModelRuntimeService["updateSessionModel"] = (
		...args
	) => {
		const service = this.host as RuntimeHostServiceExtensions;
		return service.updateSessionModel?.(...args) ?? Promise.resolve();
	};
	/**
	 * 更新活跃会话后续轮次的提供商/模型/推理连接选项。
	 */
	updateSessionConnection: SessionConnectionRuntimeService["updateSessionConnection"] =
		(...args) => {
			const service = this.host as RuntimeHostServiceExtensions;
			return service.updateSessionConnection?.(...args) ?? Promise.resolve();
		};
}
