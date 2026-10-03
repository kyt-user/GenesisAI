import { createMistral } from "@ai-sdk/mistral";
import type { LanguageModelV4 } from "@ai-sdk/provider";
import type { GatewayResolvedProviderConfig } from "@cline/shared";
import { wrapLanguageModel } from "ai";
import { resolveApiKey } from "../http";
import { splitToolImagesMiddleware } from "../middleware/split-tool-images";
import type { ProviderFactoryResult } from "./types";

export async function createMistralProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	const provider = createMistral({
		apiKey: await resolveApiKey(config),
		baseURL: config.baseUrl,
		headers: config.headers,
		fetch: config.fetch,
	});
	return {
		// Mistral 的 chat-messages 转换器与 `@ai-sdk/openai-compatible`
		// 有同样的多模态工具消息限制：`role:"tool"` 内容必须是
		// 单个字符串，因此带 image-data 部分的 `ToolResultOutput`
		// 类型 `'content'` 在序列化时会丢失字节。用
		// `splitToolImagesMiddleware` 在转换器运行前重写类型化提示词。
		// 见 `middleware/split-tool-images.ts`。
		operations: {
			language: (modelId) =>
				wrapLanguageModel({
					model: provider(modelId) as LanguageModelV4,
					middleware: splitToolImagesMiddleware,
				}),
		},
	};
}
