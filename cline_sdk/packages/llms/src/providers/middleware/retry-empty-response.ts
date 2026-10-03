// LanguageModelV4 中间件：当模型返回*空*回合——没有转换出的内容
// 也没有不受支持的输出（权威分类见
// `stream-part-classification.ts`）——时重试该流。
//
// 背景：provider 会间歇性地返回正常结束但完全没有内容的响应。
// 最初在本地后端（尤其是 Ollama）观察到，但生产遥测显示托管
// 后端（openrouter、cline、通用 OpenAI 兼容端点）也一样。
// 在 Cline 的运行时中，空的 assistant 回合是硬失败（"Model
// returned empty response"），因此一次抖动的生成就会杀死整个
// 任务。`ai-sdk-ollama` provider 为此内置了一个"可靠性"层，
// 但它位于 `doGenerate` 中并且*接管工具循环*（它自己执行工具
// 并强制合成最终文本答案）。这与 Cline 根本不兼容——Cline 通过
// `doStream` 流式输出并运行自己的工具循环——只有工具调用的回合
// 在这里是正确的、期望的结果，而不是需要"补全"的东西。
// 因此，与其采用那一层，本中间件只添加对"流式、自循环宿主"
// 安全的那一块：仅当回合真正什么都没产出时才重试。
//
// 它应用于 `ai-sdk.ts` 中央组装配点的每个 AI SDK vendor
//（见 `withEmptyResponseRetry`）；vendor 可以通过
// `ProviderFactoryResult.retryEmptyResponses` 选择退出或调整尝试次数。
//
// 流机制：每次尝试都会缓冲，直到它证明自己——第一个输出分片
//（转换出的内容、不受支持的输出或错误）接受该尝试，此时缓冲
// 被冲刷，该尝试的其余部分实时流过。被拒绝（空）的尝试被整体
// 丢弃，因此被丢弃请求的结构分片、响应元数据或空块标记
// 永远不会泄漏到逻辑流中——一次重试过的请求产生一条干净的流。
// 由于空的尝试在托管 provider 上仍会产生真实 token 费用，
// 其 `finish.usage` 会被聚合到最终的 finish 分片中，因此
// 花了三次请求的回合会报告三次请求的用量。
//
// 同一个"证明前缓冲"窗口还覆盖第二种瞬时故障模式：尝试的流
// 在模型产出任何内容之前以网络中断拒绝（见 `isTransientNetworkError`）。
// SDK 架构上的生产遥测显示它们是主要的网络类运行杀手——
// `terminated: SocketError: other side closed (UND_ERR_SOCKET)`、
// `terminated: BodyTimeoutError`、`terminated: read ECONNRESET`、
// `fetch failed: HeadersTimeoutError`。AI SDK 自带的重试只保护
// 请求*发起*阶段（当该调用以可重试的 APICallError 拒绝时重跑
// `doStream()`）；一旦流已经开始，body 死亡会逃逸 `postToApi`
// 的错误包装，以原始形态拒绝该流，杀死运行——legacy 扩展的
// 首块重试循环曾静默吸收这种失败。在这里重试与空重试同样安全：
// 尚未发出任何东西，因此被丢弃的尝试对消费者不可见。输出已经
// 流过之后的失败仍原样通过（在内容中途恢复需要去重已交付的
// 文本），而用户中止永不重试。
//
// 安全属性：
//   * 只有工具调用的回合算作内容，因此永不重试。
//   * 不受支持但真实的输出（自定义分片、推理文件、来源、
//     provider 执行的工具结果）也永不重试——模型已经响应了；
//     重新计费请求不会有用。
//   * 非空回合实时流过：缓冲只持续到第一个输出分片，对非空
//     回合而言那就是它开始产出任何东西的时刻。
//   * 以错误结束或触及 token 上限的回合原样通过（重试不会有用，
//     还可能掩盖原因）。
//   * provider 上报的带内 `error` 分片接受该尝试并通过——它们是
//     业务错误，不是传输故障。
//   * 成功重试的失败永远不会到达 `streamText`，因此不会为其
//     触发 `onError`/`captureSdkError`/`task.provider_api_error`；
//     耗尽重试或不可重试的失败与未包裹的流一样拒绝，上报路径
//     保持不变。

import type {
	LanguageModelV4Middleware,
	LanguageModelV4StreamPart,
	LanguageModelV4StreamResult,
	LanguageModelV4Usage,
} from "@ai-sdk/provider";
import { classifyModelStreamPart } from "./stream-part-classification";

/** 最小日志接口（`BasicLogger` 的子集）。 */
interface RetryLogger {
	log?(message: string, meta?: Record<string, unknown>): void;
}

export interface RetryEmptyResponseOptions {
	/** 总尝试次数含首次（因此 `3` 表示最多 2 次重试）。 */
	maxAttempts?: number;
	/** 每次空响应重试前的延迟，单位毫秒。 */
	retryDelayMs?: number;
	/**
	 * 首次网络中断重试前的延迟，单位毫秒；
	 * 之后每次翻倍（默认值下 2s → 4s，与
	 * legacy 扩展的退避一致）。
	 */
	networkRetryDelayMs?: number;
	logger?: RetryLogger;
}

/** 默认总尝试次数（首次 + 2 次重试）。 */
export const DEFAULT_EMPTY_RESPONSE_MAX_ATTEMPTS = 3;
/** 重试前的默认延迟。 */
export const DEFAULT_EMPTY_RESPONSE_RETRY_DELAY_MS = 250;
/** 首次网络中断重试前的默认延迟。 */
export const DEFAULT_NETWORK_RETRY_DELAY_MS = 2_000;

const MAX_CAUSE_DEPTH = 8;

/**
 * 瞬时传输中断的错误码，覆盖本包运行的各种运行时：
 * undici（Node fetch——VS Code 扩展宿主）、
 * 纯 Node/Bun 套接字，以及 Bun 的 fetch（CLI、桌面 sidecar）。
 */
const TRANSIENT_NETWORK_ERROR_CODES = new Set([
	"UND_ERR_SOCKET",
	"UND_ERR_BODY_TIMEOUT",
	"UND_ERR_HEADERS_TIMEOUT",
	"UND_ERR_CONNECT_TIMEOUT",
	"ECONNRESET",
	"EPIPE",
	"ETIMEDOUT",
	"ConnectionClosed",
]);

/**
 * Node 的 fetch 会把每次 body 中途终止包装为 `TypeError("terminated")`
 *（附带套接字级 cause），连接失败包装为
 * `TypeError("fetch failed")`；WebKit 则说 "failed to fetch"。
 */
const NETWORK_TYPE_ERROR_MESSAGES = new Set([
	"terminated",
	"fetch failed",
	"failed to fetch",
]);

/**
 * 判断一个错误（在其 `cause` 链的任意位置）是否标识瞬时
 * 传输中断——provider 从未拒绝的请求，其底层连接死亡或超时。
 * 链中任何位置的中止都会否决匹配：被取消的请求会暴露相同的
 * 套接字词汇，而用户取消永不重试。其他一切——HTTP 业务
 * 错误、上下文窗口错误、provider 上报的载荷——都不
 * 匹配。
 */
export function isTransientNetworkError(error: unknown): boolean {
	let aborted = false;
	let transient = false;
	let current: unknown = error;
	const seen = new Set<unknown>();
	for (
		let depth = 0;
		depth < MAX_CAUSE_DEPTH &&
		current != null &&
		typeof current === "object" &&
		!seen.has(current);
		depth++
	) {
		seen.add(current);
		const candidate = current as {
			name?: unknown;
			message?: unknown;
			code?: unknown;
			cause?: unknown;
		};
		if (
			candidate.name === "AbortError" ||
			candidate.name === "ResponseAborted"
		) {
			aborted = true;
		}
		if (
			(current instanceof TypeError &&
				typeof candidate.message === "string" &&
				NETWORK_TYPE_ERROR_MESSAGES.has(candidate.message.toLowerCase())) ||
			(typeof candidate.code === "string" &&
				TRANSIENT_NETWORK_ERROR_CODES.has(candidate.code))
		) {
			transient = true;
		}
		current = candidate.cause;
	}
	return transient && !aborted;
}

/**
 * 表示"正常"结束的 finish reason——此时空 body 是值得重试的
 * 瞬时 provider 故障。`error`（上游失败）、
 * `length`（token 上限——重试会再次撞上）和 `content-filter`
 * 都不处理。
 */
const RETRYABLE_FINISH_REASONS = new Set(["stop", "other", "unknown"]);

type FinishPart = Extract<LanguageModelV4StreamPart, { type: "finish" }>;

function addCounts(
	a: number | undefined,
	b: number | undefined,
): number | undefined {
	if (a === undefined && b === undefined) {
		return undefined;
	}
	return (a ?? 0) + (b ?? 0);
}

/** 逐字段求两个标准化 v4 用量记录之和。 */
export function addUsage(
	a: LanguageModelV4Usage,
	b: LanguageModelV4Usage,
): LanguageModelV4Usage {
	return {
		inputTokens: {
			total: addCounts(a.inputTokens?.total, b.inputTokens?.total),
			noCache: addCounts(a.inputTokens?.noCache, b.inputTokens?.noCache),
			cacheRead: addCounts(a.inputTokens?.cacheRead, b.inputTokens?.cacheRead),
			cacheWrite: addCounts(
				a.inputTokens?.cacheWrite,
				b.inputTokens?.cacheWrite,
			),
		},
		outputTokens: {
			total: addCounts(a.outputTokens?.total, b.outputTokens?.total),
			text: addCounts(a.outputTokens?.text, b.outputTokens?.text),
			reasoning: addCounts(
				a.outputTokens?.reasoning,
				b.outputTokens?.reasoning,
			),
		},
		// `raw` 是 provider 特有的，无法通用求和；
		// 发出的 finish 保留最后一次尝试的原始载荷。
		...(b.raw !== undefined ? { raw: b.raw } : {}),
	};
}

/**
 * 将被丢弃尝试的用量折算进实际发出的 finish 分片，
 * 使托管 provider 的计费反映该回合发起的每个请求
 *（包括缓存和推理明细），而不只是被接受的那一个。
 */
function withAggregatedUsage(
	finish: FinishPart,
	discardedUsage: readonly LanguageModelV4Usage[],
): FinishPart {
	if (discardedUsage.length === 0) {
		return finish;
	}
	let usage = finish.usage;
	for (const discarded of discardedUsage) {
		usage = addUsage(discarded, usage);
	}
	return { ...finish, usage };
}

/** Sleep that resolves early (without throwing) when the signal aborts. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((resolve) => {
		if (ms <= 0 || signal?.aborted) {
			resolve();
			return;
		}
		const onAbort = () => {
			clearTimeout(timer);
			resolve();
		};
		const timer = setTimeout(() => {
			signal?.removeEventListener("abort", onAbort);
			resolve();
		}, ms);
		signal?.addEventListener("abort", onAbort, { once: true });
	});
}

/**
 * 创建重试空模型响应的中间件。将其作为最外层中间件应用
 *（`wrapLanguageModel` 数组中的第一个），使每次重试
 * 都重跑完整请求。
 */
export function createRetryEmptyResponseMiddleware(
	options: RetryEmptyResponseOptions = {},
): LanguageModelV4Middleware {
	const maxAttempts = Math.max(
		1,
		options.maxAttempts ?? DEFAULT_EMPTY_RESPONSE_MAX_ATTEMPTS,
	);
	const retryDelayMs = Math.max(
		0,
		options.retryDelayMs ?? DEFAULT_EMPTY_RESPONSE_RETRY_DELAY_MS,
	);
	const networkRetryDelayMs = Math.max(
		0,
		options.networkRetryDelayMs ?? DEFAULT_NETWORK_RETRY_DELAY_MS,
	);
	const logger = options.logger;

	return {
		specificationVersion: "v4",
		wrapStream: async ({ doStream, params, model }) => {
			const abortSignal = params.abortSignal;
			// 立即启动首次尝试，与正常 doStream 的时序一致。
			const firstResult = await doStream();
			// AI SDK 会持有此对象直到步骤完成；让其 headers 与呈现给
			// 外部的尝试对齐，而不是与被丢弃的重试对齐。
			const response = { ...firstResult.response };

			const stream = new ReadableStream<LanguageModelV4StreamPart>({
				async start(controller) {
					let result: LanguageModelV4StreamResult = firstResult;
					const discardedUsage: LanguageModelV4Usage[] = [];
					// 与共享的尝试编号分开计数，使首次网络重试
					// 总是等待 `networkRetryDelayMs`，
					// 无论之前发生过多少次空响应重试。
					let networkRetries = 0;

					for (let attempt = 1; ; attempt++) {
						response.headers = result.response?.headers;
						const reader = result.stream.getReader();
						// 在该尝试证明非空之前暂扣的分片。
						const buffered: LanguageModelV4StreamPart[] = [];
						let accepted = false;
						let pendingFinish: FinishPart | null = null;
						let streamFailure: unknown;
						let streamFailed = false;

						try {
							while (true) {
								const { done, value } = await reader.read();
								if (done) {
									break;
								}
								if (value.type === "finish") {
									// 暂扣以便发出的 finish 能携带
									// 聚合用量；它本来也总是最后一个。
									pendingFinish = value;
									continue;
								}
								if (!accepted) {
									const kind = classifyModelStreamPart(value);
									if (
										kind === "converted-content" ||
										kind === "unsupported-output" ||
										kind === "error"
									) {
										// 模型产出了输出（或真实
										// 错误）：接受本次尝试，冲刷已缓冲的
										// 内容，转为实时。
										accepted = true;
										for (const part of buffered) {
											controller.enqueue(part);
										}
										buffered.length = 0;
									}
								}
								if (accepted) {
									controller.enqueue(value);
								} else {
									buffered.push(value);
								}
							}
						} catch (error) {
							streamFailed = true;
							streamFailure = error;
						} finally {
							reader.releaseLock();
						}

						if (streamFailed) {
							const canRetryFailure =
								// 只有内容前死亡才重试；一旦
								// 输出已经流过，恢复会重放
								// 消费者已经见过的文本。
								!accepted &&
								attempt < maxAttempts &&
								abortSignal?.aborted !== true &&
								isTransientNetworkError(streamFailure);
							if (!canRetryFailure) {
								controller.error(streamFailure);
								return;
							}
							if (pendingFinish) {
								discardedUsage.push(pendingFinish.usage);
							}
							response.headers = undefined;
							const delayMs = networkRetryDelayMs * 2 ** networkRetries;
							networkRetries++;
							logger?.log?.(
								"Transient network interruption before any model output; retrying",
								{
									severity: "warn",
									provider: model.provider,
									modelId: model.modelId,
									attempt,
									maxAttempts,
									retryDelayMs: delayMs,
									error:
										streamFailure instanceof Error
											? streamFailure.message
											: String(streamFailure),
								},
							);
							await sleep(delayMs, abortSignal);
							if (abortSignal?.aborted) {
								// 用户在退避期间取消：呈现
								// 中止，而不是重新拨号。
								controller.error(abortSignal.reason ?? streamFailure);
								return;
							}
							try {
								result = await doStream();
							} catch (error) {
								controller.error(error);
								return;
							}
							continue;
						}

						if (accepted) {
							if (pendingFinish) {
								controller.enqueue(
									withAggregatedUsage(pendingFinish, discardedUsage),
								);
							}
							controller.close();
							return;
						}

						const finishReason =
							pendingFinish?.finishReason.unified ?? "unknown";
						const canRetry =
							attempt < maxAttempts &&
							RETRYABLE_FINISH_REASONS.has(finishReason);

						if (!canRetry) {
							// 重试耗尽（或 finish 不可重试）：原样
							// 呈现最后一次尝试——其结构分片加上
							// 携带所有尝试用量的 finish——使
							// 下游失败如实反映发生了什么。
							for (const part of buffered) {
								controller.enqueue(part);
							}
							if (pendingFinish) {
								controller.enqueue(
									withAggregatedUsage(pendingFinish, discardedUsage),
								);
							}
							controller.close();
							return;
						}

						if (pendingFinish) {
							discardedUsage.push(pendingFinish.usage);
						}

						response.headers = undefined;
						logger?.log?.("Model returned an empty response; retrying", {
							severity: "warn",
							provider: model.provider,
							modelId: model.modelId,
							attempt,
							maxAttempts,
							finishReason,
						});

						if (retryDelayMs > 0) {
							await sleep(retryDelayMs, abortSignal);
						}
						if (abortSignal?.aborted) {
							// 用户在退避期间取消：呈现中止，
							// 而不是用已中止的信号重新拨号。
							controller.error(abortSignal.reason);
							return;
						}
						try {
							result = await doStream();
						} catch (error) {
							controller.error(error);
							return;
						}
					}
				},
			});

			return { ...firstResult, response, stream };
		},
	};
}
