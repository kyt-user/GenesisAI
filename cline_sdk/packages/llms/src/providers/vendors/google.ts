import { createGoogleGenerativeAI } from "@ai-sdk/google";
import type {
	GatewayProviderContext,
	GatewayResolvedProviderConfig,
} from "@cline/shared";
import { resolveApiKey } from "../http";
import type { ProviderFactoryResult } from "./types";

const API_VERSION_SEGMENT = /^v\d+(?:alpha|beta)?\d*$/i;

/**
 * 旧版 Gemini base-URL 设置（以及 Google 自己的 `@google/genai`
 * 客户端）将 base URL 视为主机根并自行追加 API 版本，
 * 而 `@ai-sdk/google` 期望版本段是 `baseURL` 的一部分
 *（其默认值为 `.../v1beta`）。保留旧版语义：
 * 除非 URL 已以版本段结尾，否则追加 `/v1beta`。
 */
export function normalizeGeminiBaseUrl(
	baseUrl: string | undefined,
): string | undefined {
	const trimmed = baseUrl?.trim().replace(/\/+$/, "");
	if (!trimmed) {
		return undefined;
	}
	const lastSegment = trimmed.slice(trimmed.lastIndexOf("/") + 1);
	return API_VERSION_SEGMENT.test(lastSegment) ? trimmed : `${trimmed}/v1beta`;
}

export async function createGoogleProviderModule(
	config: GatewayResolvedProviderConfig,
	context: GatewayProviderContext,
): Promise<ProviderFactoryResult> {
	const apiKey = await resolveApiKey(config);
	const provider = createGoogleGenerativeAI({
		apiKey,
		baseURL: normalizeGeminiBaseUrl(config.baseUrl),
		headers: config.headers,
		fetch: config.fetch,
		name: context.provider.id,
	});
	return {
		buildModelTools: (tools) => {
			const result: ReturnType<
				NonNullable<ProviderFactoryResult["buildModelTools"]>
			> = {};
			for (const tool of tools) {
				if (tool.name === "web_search") {
					result.web_search = { tool: provider.tools.googleSearch({}) };
				}
			}
			return result;
		},
		operations: {
			language: (modelId) => provider(modelId),
			imageGeneration: (modelId) => provider.image(modelId),
		},
	};
}
