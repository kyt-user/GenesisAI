import { isTranscriptionModel } from "@cline/shared";
import { z } from "zod";
import type { ProviderConfig } from "../providers/config";
import { resolveVercelAiGatewayBaseUrl } from "../providers/url";
import type { ModelInfo } from "./types";

const catalogSchema = z.object({
	data: z.array(
		z.object({
			id: z.string().trim().min(1),
			name: z.string().optional(),
			type: z.string().optional(),
			tags: z.array(z.string()).optional(),
			modalities: z
				.object({
					input: z.array(z.string()).optional(),
					output: z.array(z.string()).optional(),
				})
				.optional(),
			supported_specifications: z.array(z.string()).optional(),
		}),
	),
});

/**
 * 语音输入必须使用网关当前的转录路由。共享的
 * models.dev 快照可能包含已移除的模型，且没有流式标签。
 * 不要将此列表与捆绑模型合并，也不要从名称推断支持。
 */
export async function fetchVercelTranscriptionModels(
	config: ProviderConfig,
): Promise<Record<string, ModelInfo>> {
	const baseUrl = resolveVercelAiGatewayBaseUrl(
		config.baseUrl,
		"https://ai-gateway.vercel.sh/v4/ai",
	);
	const endpoint = `${baseUrl.slice(0, -"/v4/ai".length)}/v1/models`;
	const timeout = AbortSignal.timeout(config.timeoutMs ?? 5_000);
	const response = await (config.fetch ?? fetch)(endpoint, {
		headers: config.headers,
		signal: config.abortSignal
			? AbortSignal.any([config.abortSignal, timeout])
			: timeout,
	});
	if (!response.ok) {
		throw new Error(
			`Unable to verify Vercel AI Gateway transcription models (${response.status})`,
		);
	}
	const catalog = catalogSchema.parse(await response.json());
	return Object.fromEntries(
		catalog.data
			.filter(
				(model) =>
					isTranscriptionModel(model) &&
					model.supported_specifications?.includes("v4"),
			)
			.map((model) => [
				model.id,
				{
					id: model.id,
					name: model.name ?? model.id,
					operation: "transcription",
					// 支持时优先使用声明的 WebSocket 路径。标签是
					// 能力元数据，不是同时支持批量的声明。
					operationModes: model.tags?.includes("websocket-transcription")
						? ["streaming"]
						: ["batch"],
					modalities: { input: ["audio"], output: ["text"] },
				} satisfies ModelInfo,
			]),
	);
}
