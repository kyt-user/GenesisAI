import { type ProviderErrorClass, safeJsonParse } from "@cline/shared";
import { AISDKError, APICallError, RetryError, TypeValidationError } from "ai";

/**
 * 能明确标识上下文窗口溢出的 provider 错误码
 *（OpenAI 家族的 `error.code`）。
 */
const CONTEXT_WINDOW_CODES = new Set(["context_length_exceeded"]);

/**
 * provider 用于上下文窗口溢出的消息形态。源自 legacy 扩展
 * 按 provider 的检测器（OpenAI、OpenRouter、Anthropic、
 * Cerebras、Bedrock、Vercel 网关 / 阿里 Qwen）——线路消息
 * 由 provider 撰写，在 SDK 架构上完全一致；只有外围的
 * 错误对象结构变了（由下面的信号遍历处理）。
 */
const CONTEXT_WINDOW_PATTERNS = [
	/\bcontext\s*(?:length|window|limit)\b/i,
	/\bmaximum\s*context\b/i,
	/\b(?:input\s*)?tokens?\s+exceeds?\b/i,
	/\btoo\s*many\s*tokens?\b/i,
	/\binput\s+is\s+too\s+long\b/i,
	/\bprompt\s+is\s+too\s+long\b/i,
	/reduce\s+the\s+length\s+of\s+the\s+messages\s+or\s+completion/i,
	/requested\s+input\s+length\s+.*exceeds\s+.*maximum/i,
];

/**
 * 表示请求因吞吐/配额而非大小失败。每分钟 token
 * 限制的消息也会提到 token 被"超出"，因此这些信号
 * 否决上下文窗口匹配。
 */
const RATE_LIMIT_PATTERNS = [/rate[\s_-]?limit/i, /per[\s_-]?minute\b/i];

/** 溢出拒绝以无效请求家族的 HTTP 状态到达。 */
const CONTEXT_WINDOW_STATUSES = new Set([400, 413, 422]);
const RATE_LIMIT_STATUS = 429;
/**
 * 凭据拒绝。有意只按状态判断：匹配消息文本
 *（"unauthorized"、"forbidden"）会在仅仅引用这些词的
 * provider 响应体上误报，而每个拒绝凭据的 provider
 * 都会在 HTTP 层体现。
 */
const AUTH_STATUSES = new Set([401, 403]);

const MAX_WALK_DEPTH = 8;

/**
 * 其值携带更多错误细节的对象键——人类可读文本
 *（message/detail/error_message；字符串由字符串分支记录）
 * 或 provider 与网关包裹的嵌套错误结构。
 */
const DETAIL_KEYS = [
	"message",
	"detail",
	"error_message",
	"error",
	"errors",
	"cause",
	"responseBody",
	"data",
	"value",
	"param",
] as const;

/** 可能携带 HTTP 状态的对象键。 */
const STATUS_KEYS = ["status", "statusCode", "code"] as const;

interface ErrorSignals {
	messages: string[];
	statuses: Set<number>;
	codes: Set<string>;
}

function recordStatus(signals: ErrorSignals, value: unknown): void {
	const numeric =
		typeof value === "number"
			? value
			: typeof value === "string" && /^\d{3}$/.test(value.trim())
				? Number(value.trim())
				: undefined;
	if (numeric !== undefined && numeric >= 100 && numeric <= 599) {
		signals.statuses.add(numeric);
	}
}

function collectSignals(
	value: unknown,
	signals: ErrorSignals,
	visited: Set<unknown>,
	depth: number,
): void {
	if (value == null || depth > MAX_WALK_DEPTH) {
		return;
	}
	if (typeof value === "string") {
		const text = value.trim();
		if (!text) {
			return;
		}
		signals.messages.push(text);
		// provider 和网关会把上游拒绝 JSON 编码进消息
		// 字符串（OpenRouter 流中错误、Vercel `value.error_message`）；
		// 解析使嵌入的 status/code 字段成为结构化信号。
		const parsed = safeJsonParse<unknown>(text);
		if (parsed !== undefined && typeof parsed === "object") {
			collectSignals(parsed, signals, visited, depth + 1);
		} else {
			// 不是完整 JSON——仍可挖掘嵌入的 `"code": 400` / `"status": 400`。
			const embedded = text.match(/"(?:code|status)"\s*:\s*"?(\d{3})"?/);
			if (embedded) {
				recordStatus(signals, embedded[1]);
			}
		}
		return;
	}
	if (typeof value !== "object") {
		return;
	}
	if (visited.has(value)) {
		return;
	}
	visited.add(value);

	if (Array.isArray(value)) {
		for (const item of value) {
			collectSignals(item, signals, visited, depth + 1);
		}
		return;
	}

	const record = value as Record<string, unknown>;
	for (const key of STATUS_KEYS) {
		recordStatus(signals, record[key]);
	}
	for (const key of ["code", "type", "name"]) {
		const candidate = record[key];
		if (typeof candidate === "string" && candidate.trim()) {
			signals.codes.add(candidate.trim());
		}
	}
	for (const key of DETAIL_KEYS) {
		const nested = record[key];
		if (
			nested !== undefined &&
			nested !== value &&
			typeof nested !== "number"
		) {
			collectSignals(nested, signals, visited, depth + 1);
		}
	}
}

/**
 * 对收集到的信号应用检测规则。类型化 AI SDK 预处理和结构
 * 遍历共享，使两条路径分类一致：上下文窗口判定需要
 * 溢出消息模式（或显式 provider 错误码）、没有速率
 * 限制信号，并且——当有 HTTP 状态可见时——必须是
 * 无效请求家族的 HTTP 状态。
 */
function verdictFromSignals(signals: ErrorSignals): ProviderErrorClass {
	if ([...signals.codes].some((code) => CONTEXT_WINDOW_CODES.has(code))) {
		return "context_window_exceeded";
	}

	if ([...signals.statuses].some((status) => AUTH_STATUSES.has(status))) {
		return "auth";
	}

	if (signals.statuses.has(RATE_LIMIT_STATUS)) {
		return "unknown";
	}
	if (
		signals.messages.some((message) =>
			RATE_LIMIT_PATTERNS.some((pattern) => pattern.test(message)),
		)
	) {
		return "unknown";
	}
	if (
		signals.statuses.size > 0 &&
		![...signals.statuses].some((status) => CONTEXT_WINDOW_STATUSES.has(status))
	) {
		return "unknown";
	}
	if (
		signals.messages.some((message) =>
			CONTEXT_WINDOW_PATTERNS.some((pattern) => pattern.test(message)),
		)
	) {
		return "context_window_exceeded";
	}
	return "unknown";
}

function collectSignalsFrom(values: readonly unknown[]): ErrorSignals {
	const signals: ErrorSignals = {
		messages: [],
		statuses: new Set(),
		codes: new Set(),
	};
	const visited = new Set<unknown>();
	for (const value of values) {
		collectSignals(value, signals, visited, 0);
	}
	return signals;
}

/**
 * 对真正的 AI SDK 错误实例分类，使用其类型化字段而不是
 * 猜测结构。当错误不是可识别的实例（或类型化包装
 * 引向死路）时返回 `undefined`，调用方则回退到
 * 结构遍历。
 *
 * `isInstance()` 是 AI SDK 基于 symbol 的守卫，因此能跨
 * 重复的包副本成立——但它永远无法匹配只是*命名了*
 * 某个 AI SDK 错误的网关转发纯 JSON 载荷（ENG-2394）；
 * 那些仍由结构遍历负责。
 */
function classifyTypedError(
	error: unknown,
	depth: number,
): ProviderErrorClass | undefined {
	if (depth > MAX_WALK_DEPTH) {
		return undefined;
	}
	// 先查具体类别再查通用守卫——每个 AI SDK 错误都是
	// AISDKError 的子类，通用检查会把它们吞掉。
	if (RetryError.isInstance(error)) {
		// 只有最后一次尝试决定判定：更早的尝试已被重试
		// 消耗掉（通常是速率限制），既不能否决也不能伪造
		// 其分类——因此无类型的最终错误单独遍历，
		// 而不是回退到整个包装的 `errors` 数组。
		const last = error.lastError ?? error.errors[error.errors.length - 1];
		if (last == null) {
			return undefined;
		}
		return (
			classifyTypedError(last, depth + 1) ??
			verdictFromSignals(collectSignalsFrom([last]))
		);
	}
	if (APICallError.isInstance(error)) {
		// 类型化的 statusCode 是唯一权威状态，把控整个
		// 判定：载荷中的任何内容——即使是速率限制或服务器
		// 失败响应体里回显的显式溢出错误码——都不能
		// 否决 HTTP 层。没有 statusCode 时，由载荷决定。
		const status =
			typeof error.statusCode === "number" ? error.statusCode : undefined;
		if (status !== undefined && AUTH_STATUSES.has(status)) {
			return "auth";
		}
		if (status !== undefined && !CONTEXT_WINDOW_STATUSES.has(status)) {
			return "unknown";
		}
		const signals = collectSignalsFrom([
			error.message,
			error.responseBody,
			error.data,
		]);
		signals.statuses = new Set(status !== undefined ? [status] : []);
		return verdictFromSignals(signals);
	}
	if (TypeValidationError.isInstance(error)) {
		// `value` 保存校验失败的载荷——对网关流而言
		// 就是上游 provider 的拒绝。只有确定性判定
		// 才算数；"unknown" 交由调用方的结构遍历处理。
		const verdict = verdictFromSignals(collectSignalsFrom([error.value]));
		return verdict !== "unknown" ? verdict : undefined;
	}
	if (AISDKError.isInstance(error)) {
		const verdict = classifyTypedError(error.cause, depth + 1);
		return verdict !== undefined && verdict !== "unknown" ? verdict : undefined;
	}
	return undefined;
}

/**
 * 将原始 provider 错误（或已展平的错误消息）分类为
 * {@link ProviderErrorClass}。在结构化错误对象仍然可用时
 * 调用——`extractErrorMessage` 会丢弃本分类依赖的
 * 结构。
 *
 * 类型化的 AI SDK 错误实例先按其类型化字段分类；
 * 其他一切——包括只是*看起来*像 AI SDK 错误的
 * 纯 JSON 载荷——都走保守的结构遍历。
 */
export function classifyProviderError(error: unknown): ProviderErrorClass {
	try {
		const typed = classifyTypedError(error, 0);
		if (typed !== undefined) {
			return typed;
		}
	} catch {
		// 落入结构遍历。
	}

	const signals: ErrorSignals = {
		messages: [],
		statuses: new Set(),
		codes: new Set(),
	};
	try {
		collectSignals(error, signals, new Set(), 0);
	} catch {
		return "unknown";
	}
	return verdictFromSignals(signals);
}

/**
 * 瞬时且可重试的 HTTP 状态：请求超时 / 冲突 /
 * 过早、速率限制，以及 5xx 服务器失败家族（含广泛使用的
 * 529 "overloaded"）。这与 AI SDK 自身的重试策略一致，
 * 仅作为非类型化 AI SDK 实例错误的回退；
 * 类型化错误交由 {@link APICallError.isRetryable} 判断。其他任何 4xx 都是
 * 调用方自己的请求被拒绝，不得重试。
 */
const RETRYABLE_STATUSES = new Set([
	408, 409, 425, 429, 500, 502, 503, 504, 529,
]);

/**
 * 唯一的消息回退。OpenRouter 在流中把上游失败转发为
 * 裸 "Provider returned error" 字符串，既无 HTTP 状态也无类型化
 * 错误可查，因此没有其他可依赖的东西。其他所有判定
 * 都来自 AI SDK 类型化的 `isRetryable` 标志或 HTTP 状态——
 * 而不是匹配自由格式的消息文本。
 */
const PROVIDER_RETURNED_ERROR_PATTERN = /provider returned error/i;

/**
 * 取自真实 AI SDK 错误实例自身的类型化 `isRetryable` 标志，
 * 而不是重新推导——这是随 SDK 演进仍保持正确的可维护
 * 路径。当错误不是可识别的实例时返回 `undefined`，
 * 于是 {@link isRetryableProviderError} 回退到
 * 结构遍历。
 */
function isRetryableTypedError(
	error: unknown,
	depth: number,
): boolean | undefined {
	if (depth > MAX_WALK_DEPTH) {
		return undefined;
	}
	if (RetryError.isInstance(error)) {
		// SDK 已经重试过并放弃；只有最后一次尝试决定在我们这层
		// 再试一次是否值得。更早的尝试
		// 已被重试消耗（通常是速率限制），不能参与投票，因此
		// 无法分类的最终错误独立进行结构判定，而不是
		// 遍历整个包装。
		const last = error.lastError ?? error.errors[error.errors.length - 1];
		if (last == null) {
			return undefined;
		}
		return (
			isRetryableTypedError(last, depth + 1) ?? isRetryableFromSignals(last)
		);
	}
	if (APICallError.isInstance(error)) {
		return error.isRetryable === true;
	}
	if (AISDKError.isInstance(error)) {
		return isRetryableTypedError(error.cause, depth + 1);
	}
	return undefined;
}

/**
 * 对非类型化 AI SDK 错误值的结构可重试性判定：
 * 展平消息、网关转发的 JSON 载荷，或 RetryError 内的
 * 最后一次尝试。HTTP 状态优先决定；消息文本仅用于
 * 唯一记录在案的无状态 provider 怪癖。
 */
function isRetryableFromSignals(value: unknown): boolean {
	const signals: ErrorSignals = {
		messages: [],
		statuses: new Set(),
		codes: new Set(),
	};
	try {
		collectSignals(value, signals, new Set(), 0);
	} catch {
		return false;
	}

	const statuses = [...signals.statuses];
	// 永不重试凭据拒绝或确定的上下文窗口溢出：
	// 同样的请求会再次失败。
	if (statuses.some((status) => AUTH_STATUSES.has(status))) {
		return false;
	}
	if ([...signals.codes].some((code) => CONTEXT_WINDOW_CODES.has(code))) {
		return false;
	}
	// 瞬时 HTTP 状态（含任何 5xx）可重试。
	if (
		statuses.some(
			(status) =>
				RETRYABLE_STATUSES.has(status) || (status >= 500 && status <= 599),
		)
	) {
		return true;
	}
	// 其他任何可见的 4xx 都是不可重试的客户端错误。
	if (statuses.some((status) => status >= 400 && status < 500)) {
		return false;
	}
	// 无类型化错误也无状态：我们特殊处理的唯一 provider 怪癖。
	return signals.messages.some((message) =>
		PROVIDER_RETURNED_ERROR_PATTERN.test(message),
	);
}

/**
 * 判断 provider/API 错误是值得带退避重试的瞬时失败，
 * 还是重试无法修复的永久失败（凭据拒绝、
 * 上下文窗口溢出、其他客户端 4xx 错误）。优先使用
 * AI SDK 自身的类型化 `isRetryable` 信号；对非实例
 *（已展平消息或网关转发 JSON）回退到 HTTP 状态，
 * 最后是唯一记录在案的 "Provider returned error" provider
 * 怪癖。接受原始结构化错误或展平的消息字符串。
 */
export function isRetryableProviderError(error: unknown): boolean {
	// 优先使用 AI SDK 自身的类型化可重试信号。
	try {
		const typed = isRetryableTypedError(error, 0);
		if (typed !== undefined) {
			return typed;
		}
	} catch {
		// 落入结构遍历。
	}

	return isRetryableFromSignals(error);
}

/**
 * agent 循环回合级重试视角下的可重试性——不得叠加在
 * 另一层已经花掉的重试之上。请求启动失败归 AI SDK
 * 所有：它自己用感知 `retry-after` 的退避重试，一旦
 * 耗尽便呈现 `RetryError`。重跑这样的回合会让
 * SDK 的尝试次数乘以 agent 的，因此 `RetryError` 在这里是终止性的，
 * 即使其最后一次尝试看起来是瞬时的。其他一切（最重要的是
 * provider 在流中发出的错误，SDK 永不重试）由
 * {@link isRetryableProviderError} 判断。
 */
export function isRetryableBeyondSdkRetries(error: unknown): boolean {
	// 与其他类型化检查一样有守卫：当 "ai" 模块只部分可用时
	//（测试用导出子集模拟它），`RetryError.isInstance` 会抛错，
	// 此时下面的分类器是正确的回退。
	try {
		if (RetryError.isInstance(error)) {
			return false;
		}
	} catch {
		// 落入分类器。
	}
	return isRetryableProviderError(error);
}
