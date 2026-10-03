import { createGateway } from "@ai-sdk/gateway";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import type { LanguageModelV4 } from "@ai-sdk/provider";
import type {
	GatewayProviderContext,
	GatewayResolvedProviderConfig,
} from "@cline/shared";
import { modelProducesImages } from "@cline/shared";
import { createOpenRouter } from "@openrouter/ai-sdk-provider";
import { wrapLanguageModel } from "ai";
import { ensureFetch, resolveApiKey } from "../http";
import { splitToolImagesMiddleware } from "../middleware/split-tool-images";
import { isOpenAIReasoningEraModelId } from "../model-facts";
import type { ProviderFactoryResult } from "./types";

type FetchInput = Parameters<typeof fetch>[0];
type FetchWithOptionalPreconnect = typeof fetch & {
	preconnect?: (...args: unknown[]) => unknown;
};

function trimTrailingSlashes(value: string): string {
	let end = value.length;
	while (end > 0 && value.charCodeAt(end - 1) === 47) {
		end -= 1;
	}
	return value.slice(0, end);
}

function readAzureApiVersion(
	config: GatewayResolvedProviderConfig,
): string | undefined {
	const apiVersion = config.options?.apiVersion;
	if (typeof apiVersion !== "string") {
		return undefined;
	}
	const trimmed = apiVersion.trim();
	return trimmed.length > 0 ? trimmed : undefined;
}

function shouldAddAzureApiVersion(url: URL): boolean {
	return (
		url.pathname.startsWith("/openai/deployments/") &&
		!url.searchParams.has("api-version")
	);
}

function withAzureApiVersion(
	input: FetchInput,
	apiVersion: string,
): FetchInput {
	let url: URL;
	try {
		url = new URL(input instanceof Request ? input.url : input.toString());
	} catch {
		return input;
	}
	if (!shouldAddAzureApiVersion(url)) {
		return input;
	}
	url.searchParams.set("api-version", apiVersion);
	if (input instanceof Request) {
		return new Request(url.toString(), input);
	}
	return (typeof input === "string" ? url.toString() : url) as FetchInput;
}

function createAzureApiVersionFetch(
	config: GatewayResolvedProviderConfig,
): typeof fetch | undefined {
	const apiVersion = readAzureApiVersion(config);
	if (!apiVersion) {
		return config.fetch;
	}
	const baseFetch = config.fetch ?? globalThis.fetch;
	if (!baseFetch) {
		return config.fetch;
	}
	const azureFetch = ((input, init) =>
		baseFetch(withAzureApiVersion(input, apiVersion), init)) as typeof fetch;
	const baseFetchWithPreconnect = baseFetch as FetchWithOptionalPreconnect;
	(azureFetch as FetchWithOptionalPreconnect).preconnect =
		typeof baseFetchWithPreconnect.preconnect === "function"
			? baseFetchWithPreconnect.preconnect.bind(baseFetch)
			: () => undefined;
	return azureFetch;
}

type ResponseErrorHandler = (response: Response) => Promise<void> | void;

function resolveVercelGatewayImageBaseUrl(
	baseUrl: string | undefined,
): string | undefined {
	if (!baseUrl) return undefined;
	try {
		const url = new URL(baseUrl);
		if (
			url.hostname === "ai-gateway.vercel.sh" &&
			trimTrailingSlashes(url.pathname) === "/v1"
		) {
			// provider 的通用 OpenAI 兼容端点不是 AI SDK
			// Gateway 端点。让 @ai-sdk/gateway 选择其当前带版本的
			// `/ai` 基础路径，而不是产生 `/v1/image-model`。
			return undefined;
		}
		return trimTrailingSlashes(url.toString());
	} catch {
		return baseUrl;
	}
}

function readResponseErrorHandler(
	config: GatewayResolvedProviderConfig,
): ResponseErrorHandler | undefined {
	const handler = config.options?.onResponseError;
	return typeof handler === "function"
		? (handler as ResponseErrorHandler)
		: undefined;
}

function createResponseErrorFetch(input: {
	fetch: typeof fetch;
	onResponseError: ResponseErrorHandler;
}): typeof fetch {
	const responseErrorFetch = (async (requestInput, init) => {
		const response = await input.fetch(requestInput, init);

		await input.onResponseError(response);

		return response;
	}) as typeof fetch;

	const baseFetchWithPreconnect = input.fetch as FetchWithOptionalPreconnect;
	(responseErrorFetch as FetchWithOptionalPreconnect).preconnect =
		typeof baseFetchWithPreconnect.preconnect === "function"
			? baseFetchWithPreconnect.preconnect.bind(input.fetch)
			: () => undefined;
	return responseErrorFetch;
}

/**
 * OpenAI 的 chat-completions API 对推理时代模型拒绝 `max_tokens`
 * （"Unsupported parameter: 'max_tokens' is not supported with this
 * model. Use 'max_completion_tokens' instead."）。只对需要它的模型 id
 * 重命名该参数：OpenAI、Azure OpenAI 以及主要
 * OpenAI 兼容网关（OpenRouter、LiteLLM）都接受
 * `max_completion_tokens`，而只知道 `max_tokens` 的旧第三方服务器
 * 不提供 o-series/gpt-5 模型 id——因此其他所有
 * 请求保持其当前精确的线格式。
 */
export function withMaxCompletionTokensForReasoningModels(
	body: Record<string, unknown>,
): Record<string, unknown> {
	const { max_tokens: maxTokens, ...rest } = body;
	if (
		maxTokens == null ||
		typeof body.model !== "string" ||
		!isOpenAIReasoningEraModelId(body.model)
	) {
		return body;
	}
	return {
		...rest,
		// 保持通过 provider options 传入的显式 `max_completion_tokens`
		// 透传（若已存在）。
		max_completion_tokens: rest.max_completion_tokens ?? maxTokens,
	};
}

function isOpenRouterImageGenerationRequest(input: FetchInput): boolean {
	try {
		const url = new URL(
			input instanceof Request ? input.url : input.toString(),
		);
		return trimTrailingSlashes(url.pathname).endsWith("/images");
	} catch {
		return false;
	}
}

export function createSuccessDataResponseFetch(
	baseFetch: typeof fetch,
): typeof fetch {
	const responseEnvelopeFetch = (async (requestInput, init) => {
		const response = await baseFetch(requestInput, init);
		if (!response.ok || !isOpenRouterImageGenerationRequest(requestInput)) {
			return response;
		}

		const text = await response.text();
		let unwrapped = text;
		try {
			const payload = JSON.parse(text) as unknown;
			if (
				payload &&
				typeof payload === "object" &&
				!Array.isArray(payload) &&
				"success" in payload &&
				payload.success === true &&
				"data" in payload
			) {
				unwrapped = JSON.stringify(payload.data);
			}
		} catch {
			// 在下方重建原始响应，使为信封检测而消费它
			// 永远不会改变 provider 行为。
		}

		const headers = new Headers(response.headers);
		headers.delete("content-encoding");
		headers.delete("content-length");
		return new Response(unwrapped, {
			status: response.status,
			statusText: response.statusText,
			headers,
		});
	}) as typeof fetch;

	const baseFetchWithPreconnect = baseFetch as FetchWithOptionalPreconnect;
	(responseEnvelopeFetch as FetchWithOptionalPreconnect).preconnect =
		typeof baseFetchWithPreconnect.preconnect === "function"
			? baseFetchWithPreconnect.preconnect.bind(baseFetch)
			: () => undefined;
	return responseEnvelopeFetch;
}

export async function createOpenAICompatibleProviderModule(
	config: GatewayResolvedProviderConfig,
	context: GatewayProviderContext,
): Promise<ProviderFactoryResult> {
	// 不要预检 API key 是否缺失。如果凭据
	// 缺失或错误，provider 自己的响应（例如 401）就是
	// 权威错误，按原样呈现给用户。这使
	// `llms` 不对哪些 provider 需要或不需要 key 发表意见。
	const apiKey = await resolveApiKey(config);
	const fetch = createAzureApiVersionFetch(config);
	const onResponseError = readResponseErrorHandler(config);
	const providerFetch = onResponseError
		? createResponseErrorFetch({
				fetch: ensureFetch(fetch),
				onResponseError,
			})
		: fetch;
	const provider = createOpenAICompatible({
		name: context.provider.id,
		apiKey,
		...(config.baseUrl ? { baseURL: config.baseUrl } : {}),
		...(config.headers ? { headers: config.headers } : {}),
		...(providerFetch ? { fetch: providerFetch } : {}),
		includeUsage: true,
		transformRequestBody: withMaxCompletionTokensForReasoningModels,
	} as never);
	const useOpenRouterImageTransport =
		context.provider.metadata?.imageTransport === "openrouter" &&
		modelProducesImages(context.model);
	const openRouterFetch =
		context.provider.metadata?.responseEnvelope === "success-data"
			? createSuccessDataResponseFetch(ensureFetch(providerFetch))
			: providerFetch;
	const openRouterImageProvider = useOpenRouterImageTransport
		? createOpenRouter({
				apiKey,
				baseURL: config.baseUrl,
				headers: config.headers,
				fetch: openRouterFetch,
				compatibility:
					context.provider.id === "openrouter" ? "strict" : "compatible",
			})
		: undefined;
	const vercelGateway =
		context.provider.id === "vercel-ai-gateway"
			? createGateway({
					apiKey,
					baseURL: resolveVercelGatewayImageBaseUrl(config.baseUrl),
					headers: config.headers,
					fetch: providerFetch,
				})
			: undefined;
	return {
		// 用 `splitToolImagesMiddleware` 包装每个构造的模型，使
		// `output.type === 'content'` 携带图像数据部分的
		// `role:"tool"` 消息被拆分为占位文本 + 携带图像的合成
		// `role:"user"` 消息。OpenAI Chat
		// Completions 线路格式不支持多模态工具消息
		//（`@ai-sdk/openai-compatible` chat-messages 转换器
		// 对 parts 数组执行 `JSON.stringify`，丢失图像字节）。
		// 中间件在转换器运行之前对类型化的 `LanguageModelV4Prompt`
		// 操作，因此转换器只看到纯文本工具
		// 消息与相邻的多模态用户消息——这正是经典 Cline 在生产中
		// 使用了多年的线路模式（参见 origin/main 上
		// `src/core/api/transform/openai-format.ts` 中的
		// `convertToOpenAiMessages`）。
		operations: {
			language: (modelId) =>
				wrapLanguageModel({
					model: (openRouterImageProvider?.chat(modelId) ??
						provider(modelId)) as LanguageModelV4,
					middleware: splitToolImagesMiddleware,
				}),
			imageGeneration: (modelId) =>
				vercelGateway
					? vercelGateway.imageModel(modelId)
					: openRouterImageProvider
						? openRouterImageProvider.imageModel(modelId)
						: provider.imageModel(modelId),
		},
	};
}
