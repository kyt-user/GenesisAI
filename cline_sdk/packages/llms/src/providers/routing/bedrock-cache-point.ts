import type {
	GatewayModelRoute,
	GatewayProviderContext,
	GatewayProviderMetadata,
	GatewayStreamRequest,
} from "@cline/shared";
import { modelRouteMatches, resolveModelFamily } from "../model-facts";

/**
 * Bedrock 的 Converse API 将提示词缓存检查点表达为专用的
 * `cachePoint` 内容块。`@ai-sdk/amazon-bedrock` 只从
 * `providerOptions.bedrock.cachePoint` 标记发出它们，并静默丢弃
 * Anthropic 的 `cache_control` 方言，因此 Bedrock 需要自己的提示词缓存
 * 线格式。
 */
export const BEDROCK_ROUTING_METADATA: GatewayProviderMetadata = {
	routing: {
		promptCache: {
			format: "bedrock-cache-point",
			routes: [{ matcher: "anthropic-compatible" }],
		},
		reasoning: {
			format: "anthropic-thinking",
			routes: [{ matcher: "anthropic-compatible" }],
		},
	},
};

export function resolveBedrockCachePointRoute(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): GatewayModelRoute | undefined {
	const promptCache = context.provider.metadata?.routing?.promptCache;
	if (promptCache?.format !== "bedrock-cache-point") {
		return undefined;
	}

	return promptCache.routes.find((route) =>
		modelRouteMatches(route, {
			modelId: request.modelId,
			family: resolveModelFamily(context),
			capabilities: context.model.capabilities,
		}),
	);
}

export function shouldApplyBedrockCachePoint(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): boolean {
	return resolveBedrockCachePointRoute(request, context) !== undefined;
}

export function createBedrockCachePointProviderOptions() {
	return {
		bedrock: { cachePoint: { type: "default" as const } },
	};
}

/**
 * 将消息级缓存点标记附加到最后一条用户消息上。
 * Bedrock 消息转换器会在该消息内容之后追加 `cachePoint` 块，
 * 缓存到达它为止的整个前缀（工具、系统提示和历史）
 * ——镜像 Anthropic 写入器的检查点放置。
 */
export function applyBedrockCachePointToLastUserMessage(
	messages: Array<Record<string, unknown>>,
): void {
	for (let i = messages.length - 1; i >= 0; i--) {
		const message = messages[i];
		if (message?.role !== "user") {
			continue;
		}
		message.providerOptions = {
			...(message.providerOptions as Record<string, unknown> | undefined),
			...createBedrockCachePointProviderOptions(),
		};
		return;
	}
}
