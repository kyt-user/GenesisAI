// Ollama vendor，经由 `ollama-ai-provider-v2` AI SDK provider 由原生
// Ollama API（`/api/chat`）支撑。
//
// Ollama 无法通过通用的 OpenAI 兼容路径（`/v1/chat/completions`）驱动：
// 该端点忽略 Ollama 专有的 `options.num_ctx` 字段，因此每个模型
// 都以服务器默认上下文窗口（4096）加载，而不论模型的实际容量
// 或用户配置的上下文大小。原生 API 按请求接受
// `options.num_ctx`；此边界将 provider 中立的
// 模型 `contextWindow` 映射到它。

import type {
	GatewayProviderContext,
	GatewayResolvedProviderConfig,
} from "@cline/shared";
import { wrapLanguageModel } from "ai";
// 已安装的包被打过补丁（参见
// `patches/ollama-ai-provider-v2@4.0.1.patch`）以保留上游 4.0.1 版本破坏的
// 四项原生线路契约：未设置的 `think` 必须省略而不是发送为 `false`，
// 流中途的 `{"error": ...}` 对象必须作为流错误浮现而不是在干净
// 完成前被丢弃，仅附件的用户回合必须发送字符串 `content`（而非 `[]`），
// 且工具结果必须携带文档化的 `tool_name` 字段。
// `ollama.wire.test.ts` 在真实 provider 边界锁定每项契约；
// 一旦上游版本覆盖它们就移除补丁。
import { createOllama } from "ollama-ai-provider-v2";
import { ensureFetch, resolveApiKey } from "../http";
import { splitToolImagesMiddleware } from "../middleware/split-tool-images";
import type { ProviderFactoryResult } from "./types";

/**
 * 将配置的 base URL 规范化为 provider 预期的原生 Ollama API 根
 *（它会追加诸如 `/chat` 的端点路径）。
 *
 * 用户配置 `http://localhost:11434` 或
 * `https://ollama.com` 这样的主机；4.0.0 OpenAI 兼容
 * 路由保存的配置可能带 `/v1` 后缀，原生 API 配置则带 `/api`。
 */
export function normalizeOllamaBaseUrl(
	baseUrl: string | undefined,
): string | undefined {
	const trimmed = baseUrl?.trim().replace(/\/+$/, "");
	if (!trimmed) {
		return undefined;
	}
	return `${trimmed.replace(/\/(?:v1|api)$/, "")}/api`;
}

/**
 * 未配置超时时等待响应开始的时间。
 *
 * 刻意宽松：Ollama 在冷加载模型期间保持 `/api/chat` 打开，
 * 只在加载完成后才发送响应头，因此对于
 * 大模型（或大 `num_ctx`，此 vendor 会请求）会话的首个
 * 请求经常需要几分钟才开始流式传输。
 * 此处紧张的预算会把每次冷加载变成面向用户的超时错误
 *（参见 cline/cline#12829——旧版处理器 30 秒的默认值只是
 * 可以忍受，因为它的重试装饰器会静默重新发起请求直到
 * 模型加载完成）。不可达的服务器不是此超时的职责：
 * 连接级失败（拒绝、DNS）会自行立即拒绝，
 * 且用户总能在 UI 中取消请求。这仅约束
 * 已接受但静默的情况，5 分钟与其他基于 AI SDK 的 agent
 * 使用的头超时默认值一致。
 */
export const OLLAMA_DEFAULT_TIMEOUT_MS = 300_000;

/**
 * 读取配置的请求超时（旧版 `requestTimeoutMs`
 * 设置）；零/无效值回退到默认值。
 */
export function readOllamaTimeoutMs(
	config: GatewayResolvedProviderConfig,
): number {
	const timeoutMs = config.timeoutMs;
	if (
		typeof timeoutMs === "number" &&
		Number.isFinite(timeoutMs) &&
		timeoutMs > 0
	) {
		return Math.floor(timeoutMs);
	}
	return OLLAMA_DEFAULT_TIMEOUT_MS;
}

/**
 * 包装 fetch，使*响应*必须在 `timeoutMs` 内开始。一旦头
 * 到达，计时器即被清除——流式传输 body 永远不会被中断。
 * 镜像旧版处理器：它将聊天调用（流开始）
 * 与超时竞赛，而不是限制整个生成过程。
 */
export function withOllamaResponseTimeout(
	baseFetch: typeof fetch,
	timeoutMs: number,
): typeof fetch {
	return (async (input, init) => {
		const timeoutController = new AbortController();
		const timer = setTimeout(
			() =>
				timeoutController.abort(
					new Error(
						`Ollama request timed out after ${timeoutMs / 1000} seconds`,
					),
				),
			timeoutMs,
		);
		// AbortSignal.any 使上游取消在整个
		// 请求期间保持有效（包括计时器清除后的 body 流式传输）并
		// 自行清理监听器——无需手动管理。
		const upstreamSignal = init?.signal;
		const signal = upstreamSignal
			? AbortSignal.any([upstreamSignal, timeoutController.signal])
			: timeoutController.signal;
		try {
			return await baseFetch(input, { ...init, signal });
		} finally {
			clearTimeout(timer);
		}
	}) as typeof fetch;
}

export async function createOllamaProviderModule(
	config: GatewayResolvedProviderConfig,
	_context: GatewayProviderContext,
): Promise<ProviderFactoryResult> {
	// API 密钥只在 Ollama Cloud（ollama.com）需要；本地服务器
	// 接受未认证请求，因此缺少密钥不是错误。
	// Provider 通过请求头接受认证。显式配置的头
	// 优先于便捷的 API 密钥设置。
	const apiKey = await resolveApiKey(config);
	const baseURL = normalizeOllamaBaseUrl(config.baseUrl);
	const headers = {
		...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
		...config.headers,
	};
	const provider = createOllama({
		...(baseURL ? { baseURL } : {}),
		...(Object.keys(headers).length > 0 ? { headers } : {}),
		compatibility: "strict",
		fetch: withOllamaResponseTimeout(
			ensureFetch(config.fetch),
			readOllamaTimeoutMs(config),
		),
	});
	// 空响应重试（本地后端常见故障，否则会
	// 让任务硬性失败）在 `ai-sdk.ts` 中为每个
	// vendor 集中应用（见 `withEmptyResponseRetry`），包裹在此模型之外，使
	// 每次重试重跑整个请求。`splitToolImagesMiddleware` 被
	// 在此附加，原因与 OpenAI 兼容 vendor 相同：
	// 下游转换器会把多模态工具结果内容字符串化，
	// 丢失图片字节。
	return {
		operations: {
			language: (modelId) =>
				wrapLanguageModel({
					model: provider.chat(modelId),
					middleware: splitToolImagesMiddleware,
				}),
		},
	};
}
