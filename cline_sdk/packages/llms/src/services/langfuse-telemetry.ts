import type { Tracer } from "@opentelemetry/api";
import type { Telemetry } from "ai";

type DirectLangfuseTelemetryConfig = {
	baseUrl: string;
	publicKey: string;
	secretKey: string;
};

type DirectLangfuseTelemetryRuntime = {
	integration: Telemetry;
	tracerProvider: {
		forceFlush(): Promise<void>;
		shutdown(): Promise<void>;
	};
};

export type LangfuseTraceAttributes = {
	userId?: string;
	sessionId?: string;
	tags?: string[];
	metadata?: Record<string, string>;
	traceName?: string;
};

/**
 * 在 SDK 操作期间设置 Langfuse 追踪级属性。
 * 运行时上下文是有用的观察元数据，但 Langfuse 的 Sessions 和
 * Users 视图是从传播的追踪属性索引的。
 */
export async function withLangfuseTraceAttributes<T>(
	enabled: boolean,
	attributes: LangfuseTraceAttributes,
	callback: () => T | Promise<T>,
): Promise<T> {
	if (!enabled) {
		return await callback();
	}

	const { propagateAttributes } = await import("@langfuse/tracing");
	return await propagateAttributes(attributes, callback);
}

const LANGFUSE_DEBUG_ENV = "CLINE_DEBUG_LANGFUSE";

let directLangfuseRuntimes = new Map<
	string,
	Promise<DirectLangfuseTelemetryRuntime | undefined>
>();
let directLangfuseDisposableRegistration: Promise<void> | undefined;
let langfuseContextManagerInitialization: Promise<void> | undefined;

function readDirectLangfuseTelemetryConfig():
	| DirectLangfuseTelemetryConfig
	| undefined {
	const baseUrl = process.env.LANGFUSE_BASE_URL?.trim();
	const publicKey = process.env.LANGFUSE_PUBLIC_KEY?.trim();
	const secretKey = process.env.LANGFUSE_SECRET_KEY?.trim();

	if (!baseUrl || !publicKey || !secretKey) {
		return undefined;
	}

	return { baseUrl, publicKey, secretKey };
}

function isClineProviderId(providerId: string): boolean {
	return providerId === "cline" || providerId === "cline-pass";
}

export type AiSdkTelemetryDecision = {
	isEnabled: boolean;
	integrations?: Telemetry;
	recordInputs?: boolean;
	recordOutputs?: boolean;
};

const TELEMETRY_DISABLED: AiSdkTelemetryDecision = { isEnabled: false };

/**
 * 每次调用只选择一个集成。宿主 OTLP 中继优先于
 * 直接凭据，其采样、退出和内容策略在每条流上都会
 * 检查。直接导出拥有隔离的 tracer provider。
 */
export async function resolveAiSdkTelemetry(
	providerId: string,
	samplingKey?: string,
): Promise<AiSdkTelemetryDecision> {
	if (!isClineProviderId(providerId)) {
		return TELEMETRY_DISABLED;
	}

	let relayTracer = await getHostOtlpTracer();
	if (!relayTracer) {
		const config = readDirectLangfuseTelemetryConfig();
		if (!config) return TELEMETRY_DISABLED;
		const integration = await ensureDirectLangfuseIntegration(
			providerId,
			config,
		);
		// 宿主可能在异步直接运行时初始化期间注册其中继。
		// 在选择流的路线之前应用其策略。
		relayTracer = await getHostOtlpTracer();
		if (!relayTracer) {
			return integration
				? { isEnabled: true, integrations: integration }
				: TELEMETRY_DISABLED;
		}
	}

	if (await isTelemetryOptedOutGlobally()) {
		return TELEMETRY_DISABLED;
	}

	const percent = readTraceSamplePercent();
	if (percent <= 0) {
		return TELEMETRY_DISABLED;
	}
	if (percent < 100) {
		// 没有稳定键就没有确定性决策；保持关闭，而不是
		// 每请求闪烁并将任务分裂在采样线两侧。
		if (!samplingKey) {
			return TELEMETRY_DISABLED;
		}
		if (fnv1a32(samplingKey) % 100 >= percent) {
			return TELEMETRY_DISABLED;
		}
	}
	const { LangfuseVercelAiSdkIntegration } = await import(
		"@langfuse/vercel-ai-sdk"
	);
	// 每次调用都解析宿主 tracer，使远程配置替换不会
	// 留下缓存集成附着在已关闭的 provider 上。
	const integration = new LangfuseVercelAiSdkIntegration({
		tracer: relayTracer,
	});
	const recordContent = isEnvTruthy(process.env.CLINE_TRACE_RECORD_CONTENT);
	return {
		isEnabled: true,
		integrations: integration,
		recordInputs: recordContent,
		recordOutputs: recordContent,
	};
}

function readTraceSamplePercent(): number {
	// 字面量环境变量访问，使打包器能内联构建时值。
	const raw = process.env.CLINE_TRACE_SAMPLE_PERCENT?.trim();
	if (!raw) {
		// 注册 traces 导出器是宿主的主动选择；默认为
		// 全量，让环境变量（或收集器）降低音量。
		return 100;
	}
	const percent = Number.parseFloat(raw);
	return Number.isFinite(percent) ? percent : 100;
}

/**
 * 用户的全局遥测退出设置（由
 * 扩展/CLI 设置流程写入的共享设置文件）。Span 绕过 ITelemetryService 包装器
 *（后者为事件和指标强制执行退出），因此中继路径每条流
 * 重新检查此设置——这也尊重会话中途的退出。直接 Langfuse
 * 路径有意不在此处门控：它仅在
 * 操作员显式提供凭据时激活。
 */
async function isTelemetryOptedOutGlobally(): Promise<boolean> {
	let raw: string;
	try {
		const [{ readFileSync }, { resolveGlobalSettingsPath }] = await Promise.all(
			[import("node:fs"), import("@cline/shared/storage")],
		);
		raw = readFileSync(resolveGlobalSettingsPath(), "utf8");
	} catch (error) {
		// 真正缺失的文件意味着从未记录过退出（首次
		// 运行）。其他所有失败——权限、I/O、此运行时无 fs——
		// 都失败关闭：无法验证的同意不是同意。
		return (error as NodeJS.ErrnoException)?.code !== "ENOENT";
	}
	try {
		return JSON.parse(raw)?.telemetryOptOut === true;
	} catch {
		// 格式错误的设置（例如非原子写入器重写中途的
		// 撕裂读取）失败关闭：已退出的用户不应
		// 因设置文件损坏而启动追踪。
		return true;
	}
}

/**
 * 仅当全局注册的 tracer provider 是预期的
 * OTLP 收集器中继时为 true——由其创建者盖上的标记标识，
 * 而非 "存在某个记录型 tracer"。仅控制台的 tracer 既不能
 * 启用中继路径，也不能抑制直接 Langfuse 导出。
 */
async function getHostOtlpTracer(): Promise<Tracer | undefined> {
	const [{ trace }, { isOtlpTraceRelayProvider }] = await Promise.all([
		import("@opentelemetry/api"),
		import("@cline/shared"),
	]);
	const provider = trace.getTracerProvider() as { getDelegate?: () => unknown };
	if (
		isOtlpTraceRelayProvider(provider) ||
		isOtlpTraceRelayProvider(provider.getDelegate?.())
	) {
		return trace.getTracer("cline-provider-langfuse");
	}
	return undefined;
}

/** FNV-1a：跨进程稳定，因此任务在重试时采样结果相同。 */
function fnv1a32(value: string): number {
	let hash = 0x811c9dc5;
	for (let i = 0; i < value.length; i++) {
		hash ^= value.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return hash;
}

async function ensureDirectLangfuseIntegration(
	providerId: string,
	config: DirectLangfuseTelemetryConfig,
): Promise<Telemetry | undefined> {
	const configKey = JSON.stringify(config);
	let runtimePromise = directLangfuseRuntimes.get(configKey);
	if (!runtimePromise) {
		runtimePromise = registerDirectLangfuseDisposable().then(
			async () => await initializeDirectLangfuseTelemetry(config),
		);
		directLangfuseRuntimes.set(configKey, runtimePromise);
	}

	const runtime = await runtimePromise;
	if (!runtime && directLangfuseRuntimes.get(configKey) === runtimePromise) {
		directLangfuseRuntimes.delete(configKey);
	}
	debugLangfuse(
		`resolved direct integration=${String(Boolean(runtime))} provider=${providerId}`,
	);
	return runtime?.integration;
}

async function registerDirectLangfuseDisposable(): Promise<void> {
	if (!directLangfuseDisposableRegistration) {
		directLangfuseDisposableRegistration = import("@cline/shared").then(
			({ registerDisposable }) => {
				registerDisposable(disposeLangfuseTelemetry);
			},
		);
	}
	await directLangfuseDisposableRegistration;
}

async function ensureLangfuseContextManager(): Promise<void> {
	if (!langfuseContextManagerInitialization) {
		langfuseContextManagerInitialization = Promise.all([
			import("@opentelemetry/api"),
			import("@opentelemetry/context-async-hooks"),
		]).then(([{ context }, { AsyncLocalStorageContextManager }]) => {
			const contextManager = new AsyncLocalStorageContextManager().enable();
			if (!context.setGlobalContextManager(contextManager)) {
				// 另一个 OpenTelemetry 所有者已安装上下文管理器。
				contextManager.disable();
			}
		});
	}
	await langfuseContextManagerInitialization;
}

async function initializeDirectLangfuseTelemetry(
	config: DirectLangfuseTelemetryConfig,
): Promise<DirectLangfuseTelemetryRuntime | undefined> {
	try {
		// 直接 SDK 消费者拥有此隔离的导出器。它有意
		// 不替换或修改进程的全局 tracer provider。
		if (!process.env.OTEL_SERVICE_NAME?.trim()) {
			process.env.OTEL_SERVICE_NAME = "cline-sdk";
		}
		await ensureLangfuseContextManager();
		const [
			{ LangfuseSpanProcessor },
			{ LangfuseVercelAiSdkIntegration },
			{ NodeTracerProvider },
		] = await Promise.all([
			import("@langfuse/otel"),
			import("@langfuse/vercel-ai-sdk"),
			import("@opentelemetry/sdk-trace-node"),
		]);

		const spanProcessor = new LangfuseSpanProcessor(config);
		const tracerProvider = new NodeTracerProvider({
			spanProcessors: [spanProcessor],
		});
		const integration = new LangfuseVercelAiSdkIntegration({
			tracer: tracerProvider.getTracer("cline-langfuse-direct"),
		});
		debugLangfuse(`created isolated direct exporter baseUrl=${config.baseUrl}`);

		return { integration, tracerProvider };
	} catch (error) {
		debugLangfuse(
			`direct initialization failed error=${error instanceof Error ? error.message : String(error)}`,
		);
		return undefined;
	}
}

export async function disposeLangfuseTelemetry(): Promise<void> {
	const pendingRuntimes = [...directLangfuseRuntimes.values()];
	directLangfuseRuntimes.clear();
	directLangfuseDisposableRegistration = undefined;
	const settledRuntimes = await Promise.allSettled(pendingRuntimes);
	const runtimes = settledRuntimes.flatMap((result) =>
		result.status === "fulfilled" && result.value ? [result.value] : [],
	);

	await Promise.all(
		runtimes.map(async ({ tracerProvider }) => {
			try {
				await tracerProvider.forceFlush();
				debugLangfuse("direct forceFlush completed");
			} catch (error) {
				debugLangfuse(
					`direct forceFlush failed error=${error instanceof Error ? error.message : String(error)}`,
				);
			}
		}),
	);
	await Promise.all(
		runtimes.map(async ({ tracerProvider }) => {
			try {
				await tracerProvider.shutdown();
				debugLangfuse("direct shutdown completed");
			} catch (error) {
				debugLangfuse(
					`direct shutdown failed error=${error instanceof Error ? error.message : String(error)}`,
				);
			}
		}),
	);
}

export function debugLangfuse(message: string): void {
	if (!isLangfuseDebugEnabled()) {
		return;
	}
	console.warn(`[langfuse-debug] ${message}`);
}

function isLangfuseDebugEnabled(): boolean {
	const raw = process.env[LANGFUSE_DEBUG_ENV];
	if (!raw) {
		return false;
	}
	const normalized = raw.trim().toLowerCase();
	return normalized === "1" || normalized === "true" || normalized === "yes";
}

function isEnvTruthy(raw: string | undefined): boolean {
	if (!raw) return false;
	const normalized = raw.trim().toLowerCase();
	return normalized === "1" || normalized === "true" || normalized === "yes";
}

export function resetLangfuseTelemetryForTests(): void {
	directLangfuseRuntimes = new Map();
	directLangfuseDisposableRegistration = undefined;
	langfuseContextManagerInitialization = undefined;
}
