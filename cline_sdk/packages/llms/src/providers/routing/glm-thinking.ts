import type {
	GatewayProviderContext,
	GatewayProviderMetadata,
	GatewayStreamRequest,
} from "@cline/shared";
import { isGlmModel } from "../model-facts";
import type { ProviderOptionsPatch } from "./utils";

/**
 * GLM thinking 路由。
 *
 * 原生 Z.AI 使用 `thinking: { type: "enabled" | "disabled" }`。
 * 路由的 OpenAI 兼容 GLM 端点应使用通用的 `reasoning`
 * 控制格式。返回值是普通的 provider-options 补丁，
 * 使组合器可以依赖合并顺序而非带外标志。
 */

export const GLM_THINKING_ROUTING_METADATA: GatewayProviderMetadata = {
	routing: {
		reasoning: {
			format: "glm-thinking",
			routes: [
				{ matcher: "model-family", family: "glm" },
				{ matcher: "model-family", family: "glm-air" },
				{ matcher: "model-family", family: "glm-flash" },
			],
		},
	},
};

function buildNativeZaiThinkingOptions(request: GatewayStreamRequest) {
	if (request.reasoning?.enabled === undefined) {
		return undefined;
	}
	return {
		thinking: {
			type: request.reasoning.enabled ? "enabled" : "disabled",
		},
	};
}

function buildRoutedGlmReasoningOptions(request: GatewayStreamRequest) {
	if (request.reasoning?.enabled === true) {
		return {
			reasoning: {
				enabled: true,
			},
		};
	}
	if (request.reasoning?.enabled === false) {
		return {
			reasoning: {
				exclude: true,
			},
		};
	}
	return undefined;
}

export function buildNativeGlmThinkingProviderOptionsPatch(
	request: GatewayStreamRequest,
	providerOptionsKey: string,
): ProviderOptionsPatch | undefined {
	// 原生 Z.AI GLM 端点期望 `thinking.type`；它们不接受
	// 路由的 `reasoning.enabled` / `reasoning.exclude` 格式。
	const nativeThinking = buildNativeZaiThinkingOptions(request);
	return nativeThinking
		? {
				openaiCompatible: nativeThinking,
				[request.providerId]: nativeThinking,
				...(providerOptionsKey !== request.providerId
					? { [providerOptionsKey]: nativeThinking }
					: {}),
			}
		: undefined;
}

export function buildRoutedGlmReasoningProviderOptionsPatch(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
	providerOptionsKey: string,
	options?: { includeProviderBuckets?: boolean },
): ProviderOptionsPatch | undefined {
	// 路由的 GLM 端点保持 OpenAI 兼容，使用通用的
	// `reasoning` include/exclude 格式，而不是原生 Z.AI 的 `thinking.type`。
	if (!isGlmModel(request, context)) {
		return undefined;
	}

	const routed = buildRoutedGlmReasoningOptions(request);
	if (!routed) {
		return undefined;
	}

	return {
		openaiCompatible: routed,
		...(options?.includeProviderBuckets === false
			? {}
			: {
					[request.providerId]: routed,
					...(providerOptionsKey !== request.providerId
						? { [providerOptionsKey]: routed }
						: {}),
				}),
	};
}
