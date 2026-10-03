import { createOpenAI } from "@ai-sdk/openai";
import type {
	GatewayProviderContext,
	GatewayResolvedProviderConfig,
} from "@cline/shared";
import { resolveApiKey } from "../http";
import type { ProviderFactoryResult } from "./types";

function isChatGptOAuthBaseUrl(baseUrl: string | undefined): boolean {
	if (!baseUrl) {
		return false;
	}
	try {
		const { hostname } = new URL(baseUrl);
		return hostname === "chatgpt.com" || hostname.endsWith(".chatgpt.com");
	} catch {
		return false;
	}
}

export async function createOpenAIProviderModule(
	config: GatewayResolvedProviderConfig,
	context: GatewayProviderContext,
): Promise<ProviderFactoryResult> {
	const apiKey = await resolveApiKey(config);
	const provider = createOpenAI({
		apiKey,
		baseURL: config.baseUrl,
		headers: config.headers,
		fetch: config.fetch,
		name: context.provider.id,
	});
	// ChatGPT OAuth Codex 后端拒绝 `max_output_tokens`，且
	// OpenAI Responses API 应用自己的默认值，因此网关合成的
	// 上限永远不会转发。显式上限——无论由网关
	// 从调用方请求解析还是直接传到此 provider——
	// 对 API 密钥用法都会被尊重，因为该端点支持输出
	// 限制。
	const isChatGptOAuth = isChatGptOAuthBaseUrl(config.baseUrl);
	return {
		buildModelTools: (tools) => {
			const result: ReturnType<
				NonNullable<ProviderFactoryResult["buildModelTools"]>
			> = {};
			for (const tool of tools) {
				switch (tool.name) {
					case "web_search":
						result.web_search = { tool: provider.tools.webSearch() };
						break;
					case "image_generation":
						result.image_generation = {
							tool: provider.tools.imageGeneration({
								outputFormat: tool.outputFormat ?? "png",
							}),
							projectResult: (output) => {
								const record =
									output && typeof output === "object" && !Array.isArray(output)
										? (output as Record<string, unknown>)
										: undefined;
								if (
									typeof record?.result !== "string" ||
									record.result.length === 0
								) {
									throw new Error(
										"OpenAI image generation tool returned no supported image output",
									);
								}
								return {
									media: [
										{
											modality: "image",
											mediaType: `image/${tool.outputFormat ?? "png"}`,
											source: { type: "base64", data: record.result },
										},
									],
								};
							},
						};
						break;
				}
			}
			return result;
		},
		operations: {
			language: (modelId) => provider.responses(modelId),
			imageGeneration: (modelId) => provider.image(modelId),
		},
		buildStreamConfig: (request) => ({
			...(!isChatGptOAuth &&
			request.maxTokens !== undefined &&
			request.defaultedMaxTokens !== true
				? { maxOutputTokens: request.maxTokens }
				: {}),
			temperature: request.temperature,
		}),
	};
}
