export type TelemetryPrimitive = string | number | boolean | null | undefined;

export type TelemetryValue =
	| TelemetryPrimitive
	| TelemetryObject
	| TelemetryArray;

export type TelemetryObject = { [key: string]: TelemetryValue };

export type TelemetryArray = Array<TelemetryValue>;

export type TelemetryProperties = TelemetryObject;

const DEFAULT_ERROR_MESSAGE_LIMIT = 500;

export type SdkTelemetryErrorComponent =
	| "shared"
	| "llms"
	| "agents"
	| "core"
	| "cli"
	| "vscode"
	| "desktop"
	| (string & {});

export type SdkTelemetryErrorSeverity =
	| "debug"
	| "info"
	| "warn"
	| "error"
	| "fatal";

export interface CaptureSdkErrorInput {
	component: SdkTelemetryErrorComponent;
	operation: string;
	error: unknown;
	/**
	 * A useful message derived while the caller still has domain-specific error
	 * context. The raw error remains the source of type, code, and status.
	 */
	errorMessage?: string;
	severity?: SdkTelemetryErrorSeverity;
	handled?: boolean;
	context?: TelemetryProperties;
	event?: string;
	messageLimit?: number;
}

export const AGENT_UNEXPECTED_REASONING_TOKENS_EVENT =
	"agent.reasoning.unexpected_tokens";

export interface CaptureAgentUnexpectedReasoningTokensInput {
	sessionId?: string;
	agentId: string;
	runId?: string;
	iteration: number;
	providerId?: string;
	modelId?: string;
	requestedThinking: false;
	reasoningTokenCount: number;
}

export const TASK_PROVIDER_REQUEST_STARTED_EVENT =
	"task.provider_request_started";
export const TASK_PROVIDER_STREAM_STARTED_EVENT =
	"task.provider_stream_started";
export const TASK_FIRST_CHUNK_RECEIVED_EVENT = "task.first_chunk_received";
export const TASK_PROVIDER_STREAM_FAILED_EVENT = "task.provider_stream_failed";
export const TASK_CANCELLED_EVENT = "task.cancelled";
export const TASK_MAX_TOKENS_RECOVERY_EVENT = "task.max_tokens_recovery";

export interface CaptureTaskLifecycleEventInput {
	event: string;
	sessionId?: string;
	ulid?: string;
	agentId?: string;
	conversationId?: string;
	runId?: string;
	iteration?: number;
	providerId?: string;
	modelId?: string;
	phase?: string;
	durationMs?: number;
	eventType?: string;
	error?: unknown;
	/**
	 * `error` 的分类（例如 context_window_exceeded），作为
	 * `error_class` 与规范化错误字段一起发射。
	 */
	errorClass?: string;
	messageLimit?: number;
}

/**
 * 进程外宿主生成此 core 的原因。JetBrains 插件通过
 * `CLINE_CORE_SPAWN_REASON` 设置它；与其 `SpawnReason` 保持同步。
 */
export const CORE_SPAWN_REASONS = [
	"initial",
	"crash_restart",
	"rollout_fallback",
	"rollout_demotion",
	"user_restart",
] as const;
export type CoreSpawnReason = (typeof CORE_SPAWN_REASONS)[number];

export interface TelemetryMetadata {
	extension_version: string;
	/**
	 * 宿主侧 Cline 分发包的版本：JetBrains 上是 JetBrains 插件版本
	 *（例如 1.1.61），VSCode 上是扩展版本（与
	 * `extension_version` 匹配）。宿主未报告时缺失。
	 */
	host_plugin_version?: string;
	cline_type: string;
	platform: string;
	platform_version: string;
	os_type: string;
	os_version: string;
	is_dev?: string;
	is_remote_workspace?: boolean;
	/**
	 * Spawn-time facts reported by an out-of-process host (the JetBrains plugin): how many
	 * cores this host window has spawned so far and why this one was started. Absent when the
	 * host runs core in-process (VS Code).
	 */
	core_spawn_ordinal?: number;
	core_spawn_reason?: CoreSpawnReason;
}

export interface ITelemetryService {
	setDistinctId(distinctId?: string): void;
	setMetadata(metadata: Partial<TelemetryMetadata>): void;
	updateMetadata(metadata: Partial<TelemetryMetadata>): void;
	setCommonProperties(properties: TelemetryProperties): void;
	updateCommonProperties(properties: TelemetryProperties): void;
	isEnabled(): boolean;
	capture(input: { event: string; properties?: TelemetryProperties }): void;
	captureRequired(event: string, properties?: TelemetryProperties): void;
	recordCounter(
		name: string,
		value: number,
		attributes?: TelemetryProperties,
		description?: string,
		required?: boolean,
	): void;
	recordHistogram(
		name: string,
		value: number,
		attributes?: TelemetryProperties,
		description?: string,
		required?: boolean,
	): void;
	recordGauge(
		name: string,
		value: number | null,
		attributes?: TelemetryProperties,
		description?: string,
		required?: boolean,
	): void;
	flush(): Promise<void>;
	dispose(): Promise<void>;
}

export const SDK_ERROR_TELEMETRY_EVENT = "sdk.error";

// `sdk.error` is a diagnostic firehose: a process stuck in a retry loop
// (e.g. an unattended agent re-hitting a rate-limited provider) can emit the
// same failure thousands of times and drown the signal. Identical failures
// are therefore capped per process: the first few per hour emit normally,
// the rest are only counted, and the count surfaces as `suppressed_count` on
// the next emission once the window rolls over — a hot loop stays visible
// without flooding. State is in-memory only and the cap never throws.

/** 每个窗口每个键允许的相同 `sdk.error` 发射次数。 */
export const SDK_ERROR_RATE_LIMIT_MAX_PER_WINDOW = 5;
/** 相同 `sdk.error` 发射的抑制窗口。 */
export const SDK_ERROR_RATE_LIMIT_WINDOW_MS = 60 * 60 * 1000;
/** 跟踪的不同失败键数上限；最旧的键被驱逐。 */
const SDK_ERROR_RATE_LIMIT_MAX_TRACKED_KEYS = 512;

interface SdkErrorWindow {
	startMs: number;
	emitted: number;
	suppressed: number;
}

const sdkErrorWindows = new Map<string, SdkErrorWindow>();

/**
 * 清除每进程的 `sdk.error` 速率限制状态（测试隔离）。
 *
 * @internal 仅导出使包测试套件能在测试之间隔离
 * 进程范围的抑制状态；不是受支持的运行时 API。
 */
export function resetSdkErrorRateLimiterForTests(): void {
	sdkErrorWindows.clear();
}

/**
 * 每个不同失败一个键。结构化判别器（`error_status`、
 * `error_code`）参与键的构成，因此 HTTP 429 和 HTTP 401 永远不会
 * 共享预算，而消息被规范化（数字串折叠、
 * 空白折叠、不区分大小写、有界），使仅因计数器或 id 不同
 * 的消息——`"iteration 14"` 与 `"iteration 99"`——被合并
 * 而不是各自获得新预算。
 */
function sdkErrorRateLimitKey(
	event: string,
	properties: TelemetryProperties,
): string {
	const message =
		typeof properties.error_message === "string"
			? properties.error_message
			: "";
	return [
		event,
		properties.component,
		properties.operation,
		properties.error_type,
		properties.error_code ?? "",
		properties.error_status ?? "",
		message
			.replace(/\d+/g, "#")
			.replace(/\s+/g, " ")
			.trim()
			.toLowerCase()
			.slice(0, 256),
	].join("\u0000");
}

function admitSdkError(key: string): { emit: boolean; suppressed: number } {
	const now = Date.now();
	const window = sdkErrorWindows.get(key);
	if (window && now - window.startMs < SDK_ERROR_RATE_LIMIT_WINDOW_MS) {
		if (window.emitted < SDK_ERROR_RATE_LIMIT_MAX_PER_WINDOW) {
			window.emitted += 1;
			return { emit: true, suppressed: 0 };
		}
		window.suppressed += 1;
		return { emit: false, suppressed: window.suppressed };
	}
	// 新键或窗口已过期：发射，并携带前一个窗口中
	// 被抑制的发射计数。
	const suppressed = window?.suppressed ?? 0;
	sdkErrorWindows.delete(key);
	if (sdkErrorWindows.size >= SDK_ERROR_RATE_LIMIT_MAX_TRACKED_KEYS) {
		const oldest = sdkErrorWindows.keys().next();
		if (!oldest.done) {
			sdkErrorWindows.delete(oldest.value);
		}
	}
	sdkErrorWindows.set(key, { startMs: now, emitted: 1, suppressed: 0 });
	return { emit: true, suppressed };
}

export function captureAgentUnexpectedReasoningTokens(
	telemetry: ITelemetryService | undefined,
	input: CaptureAgentUnexpectedReasoningTokensInput,
): void {
	telemetry?.capture({
		event: AGENT_UNEXPECTED_REASONING_TOKENS_EVENT,
		properties: stripUndefinedTelemetryProperties({
			sessionId: input.sessionId,
			agentId: input.agentId,
			runId: input.runId,
			iteration: input.iteration,
			providerId: input.providerId,
			modelId: input.modelId,
			requestedThinking: input.requestedThinking,
			reasoningTokenCount: input.reasoningTokenCount,
		}),
	});
}

export function captureTaskLifecycleEvent(
	telemetry: ITelemetryService | undefined,
	input: CaptureTaskLifecycleEventInput,
): void {
	if (!telemetry) {
		return;
	}
	telemetry.capture({
		event: input.event,
		properties: stripUndefinedTelemetryProperties({
			sessionId: input.sessionId,
			ulid: input.ulid ?? input.sessionId,
			agentId: input.agentId,
			conversationId: input.conversationId,
			runId: input.runId,
			iteration: input.iteration,
			provider: input.providerId,
			providerId: input.providerId,
			model: input.modelId,
			modelId: input.modelId,
			phase: input.phase,
			durationMs: input.durationMs,
			eventType: input.eventType,
			...(input.error === undefined
				? {}
				: normalizeSdkError(input.error, input.messageLimit)),
			error_class: input.errorClass,
		}),
	});
}

/**
 * 上报 SDK 错误，受上述每个进程对相同失败的
 * 音量上限约束。
 *
 * 当失败被记录时返回 `true`——已发射，或被音量上限
 * 计入 `suppressed_count`——当遥测不可用时返回 `false`。位于
 * 层边界的上报者转发返回值（参见模型流 `finish` 事件上的
 * `errorReported`），使外层知道该失败已被计入，一个底层
 * 失败只产生一个事件，而不是它传播经过的每一层各产生一个。
 */
export function captureSdkError(
	telemetry: ITelemetryService | undefined,
	input: CaptureSdkErrorInput,
): boolean {
	if (!telemetry) {
		return false;
	}
	const event = input.event ?? SDK_ERROR_TELEMETRY_EVENT;
	const properties = buildSdkErrorProperties(input);
	let suppressed = 0;
	try {
		const decision = admitSdkError(sdkErrorRateLimitKey(event, properties));
		if (!decision.emit) {
			return true;
		}
		suppressed = decision.suppressed;
	} catch {
		// 音量上限绝不能阻止错误上报。
	}
	telemetry.capture({
		event,
		properties:
			suppressed > 0
				? { ...properties, suppressed_count: suppressed }
				: properties,
	});
	return true;
}

export function buildSdkErrorProperties(
	input: CaptureSdkErrorInput,
): TelemetryProperties {
	// 剥离 undefined 值（与这里的其他 capture 辅助函数一致）——
	// 否则 OTel 适配器会将它们导出为字面量 "undefined" 字符串。
	return stripUndefinedTelemetryProperties({
		...(input.context ?? {}),
		component: input.component,
		operation: input.operation,
		severity: input.severity ?? "error",
		handled: input.handled ?? true,
		...normalizeSdkError(input.error, input.messageLimit, input.errorMessage),
	});
}

function stripUndefinedTelemetryProperties(
	properties: TelemetryProperties,
): TelemetryProperties {
	const result: TelemetryProperties = {};
	for (const [key, value] of Object.entries(properties)) {
		if (value !== undefined) {
			result[key] = value;
		}
	}
	return result;
}

export function normalizeSdkError(
	error: unknown,
	messageLimit = DEFAULT_ERROR_MESSAGE_LIMIT,
	errorMessage?: string,
): TelemetryProperties {
	const record = isRecord(error) ? error : undefined;
	const errorObject = error instanceof Error ? error : undefined;
	const message =
		stringValue(errorMessage) ??
		stringValue(errorObject?.message) ??
		stringValue(record?.message) ??
		fallbackErrorString(error) ??
		"Unknown error";
	const code = stringOrNumberValue(record?.code);
	const status =
		numberValue(record?.status) ??
		numberValue(record?.statusCode) ??
		numberValue(record?.responseStatus);

	return {
		error_type:
			errorObject?.name?.trim() ||
			stringValue(record?.name) ||
			errorObject?.constructor?.name ||
			"Error",
		error_message: truncateTelemetryString(
			sanitizeTelemetryErrorMessage(message),
			messageLimit,
		),
		...(code !== undefined ? { error_code: code } : {}),
		...(status !== undefined ? { error_status: status } : {}),
	};
}

function sanitizeTelemetryErrorMessage(message: string): string {
	return message
		.replace(/(authorization=Bearer\s+)[^&\s]+/gi, "$1[redacted]")
		.replace(
			/(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret)=([^&\s]+)/gi,
			"$1=[redacted]",
		)
		.replace(/(Bearer\s+)[A-Za-z0-9._~+/-]+=*/gi, "$1[redacted]")
		.replace(/\/Users\/[^/\s]+/g, "/Users/[redacted]")
		.replace(/\/home\/[^/\s]+/g, "/home/[redacted]")
		.replace(/([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\s]+/g, "$1[redacted]");
}

function truncateTelemetryString(value: string, limit: number): string {
	const normalizedLimit = Math.max(1, Math.floor(limit));
	return value.length > normalizedLimit
		? value.substring(0, normalizedLimit)
		: value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null;
}

function stringValue(value: unknown): string | undefined {
	return typeof value === "string" && value.trim().length > 0
		? value
		: undefined;
}

function fallbackErrorString(error: unknown): string | undefined {
	if (error instanceof Error) {
		return undefined;
	}
	const value = typeof error === "string" ? error : String(error);
	return value === "[object Object]" ? undefined : stringValue(value);
}

function stringOrNumberValue(value: unknown): string | number | undefined {
	if (typeof value === "string" && value.trim().length > 0) {
		return value;
	}
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}
	return undefined;
}

function numberValue(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value)
		? value
		: undefined;
}

/**
 * 盖在含真实 OTLP 导出器的 tracer provider 上的标记属性
 *——收集器中继。追踪决策必须显式识别中继，
 * 而不是从 "存在某个记录型 tracer" 推断：否则仅控制台的
 * tracer 会被误分类为中继。provider 实例上的属性
 *（通过 OTel API 全局访问）能在打包模块重复时存活，
 * 而模块级注册表不能。
 */
export const OTLP_TRACE_RELAY_MARKER = "_clineOtlpTraceRelay";

export function markOtlpTraceRelayProvider(provider: object): void {
	(provider as Record<string, unknown>)[OTLP_TRACE_RELAY_MARKER] = true;
}

export function isOtlpTraceRelayProvider(provider: unknown): boolean {
	return (
		!!provider &&
		typeof provider === "object" &&
		(provider as Record<string, unknown>)[OTLP_TRACE_RELAY_MARKER] === true
	);
}

export interface OpenTelemetryClientConfig {
	/**
	 * 遥测是否通过 OTEL_TELEMETRY_ENABLED 启用
	 */
	enabled: boolean;

	/**
	 * 指标导出器类型 - 可用逗号分隔多个导出器
	 * 示例："console"、"otlp"、"console,otlp"
	 */
	metricsExporter?: string;

	/**
	 * 日志/事件导出器类型 - 可用逗号分隔多个导出器
	 * 示例："console"、"otlp"
	 */
	logsExporter?: string;

	/**
	 * 分布式追踪导出器类型 - 逗号分隔的多个导出器。
	 * 示例："console"、"otlp"。未设置时不注册 `TracerProvider`。
	 */
	tracesExporter?: string;

	/**
	 * OTel resource `service.name` (default "cline"). Distinguishes processes
	 * that ship in the same binary — e.g. the CLI vs the detached hub daemon.
	 */
	serviceName?: string;

	/**
	 * OTel 资源 `service.version`。
	 */
	serviceVersion?: string;

	/**
	 * OTLP 导出器协议。SDK 支持目前限于 "http/json"。
	 */
	otlpProtocol?: string;

	/**
	 * 通用 OTLP 端点（未设置特定端点时使用）
	 */
	otlpEndpoint?: string;

	/**
	 * 通用 OTLP 请求头
	 */
	otlpHeaders?: Record<string, string>;

	/**
	 * 指标专用的 OTLP 协议
	 */
	otlpMetricsProtocol?: string;

	/**
	 * 指标专用的 OTLP 端点
	 */
	otlpMetricsEndpoint?: string;

	otlpMetricsHeaders?: Record<string, string>;

	/**
	 * 日志专用的 OTLP 协议
	 */
	otlpLogsProtocol?: string;

	/**
	 * 日志专用的 OTLP 端点
	 */
	otlpLogsEndpoint?: string;

	otlpLogsHeaders?: Record<string, string>;

	/**
	 * 追踪专用的 OTLP 协议（SDK 支持目前限于 "http/json"）
	 */
	otlpTracesProtocol?: string;

	/**
	 * 追踪专用的 OTLP 端点（导出 OTLP 追踪时默认为 {@link otlpEndpoint}）
	 */
	otlpTracesEndpoint?: string;

	otlpTracesHeaders?: Record<string, string>;

	/**
	 * 指标导出间隔（毫秒，用于控制台导出器）
	 */
	metricExportInterval?: number;

	/**
	 * 是否为 gRPC OTLP 导出器使用非安全（无 TLS）连接
	 * 无 TLS 的本地开发设为 "true"
	 * 默认：false（使用 TLS）
	 */
	otlpInsecure?: boolean;

	/**
	 * 日志记录的最大批量大小（默认：512）
	 */
	logBatchSize?: number;

	/**
	 * 导出日志前的最大等待时间（毫秒，默认：5000）
	 */
	logBatchTimeout?: number;

	/**
	 * 日志记录的最大队列大小（默认：2048）
	 */
	logMaxQueueSize?: number;
}
