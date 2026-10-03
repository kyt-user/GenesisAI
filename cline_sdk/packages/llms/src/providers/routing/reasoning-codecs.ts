import type {
	GatewayProviderContext,
	GatewayStreamRequest,
} from "@cline/shared";
import { DEFAULT_GATEWAY_MAX_OUTPUT_TOKENS } from "../gateway";
import { isPositiveFiniteNumber } from "../utils";

// OpenRouter 将总输出上限（`max_tokens` / `max_output_tokens`）
// 与 `reasoning.max_tokens` 分开，后者只限制推理 token 部分。
// 来源：
// - https://openrouter.ai/docs/api/reference/parameters
// - https://openrouter.ai/docs/api/reference/responses/reasoning
const OPENROUTER_REASONING_BUDGET_FRACTION = 0.6;

export function hasReasoningControls(
	reasoning: GatewayStreamRequest["reasoning"],
): boolean {
	return (
		reasoning?.enabled !== undefined ||
		typeof reasoning?.budgetTokens === "number"
	);
}

function resolveOpenRouterReasoningMaxTokens(
	request: GatewayStreamRequest,
	context?: GatewayProviderContext,
): number {
	const outputMaxTokens = isPositiveFiniteNumber(request.maxTokens)
		? request.maxTokens
		: isPositiveFiniteNumber(context?.model.maxOutputTokens)
			? context.model.maxOutputTokens
			: DEFAULT_GATEWAY_MAX_OUTPUT_TOKENS;
	return Math.max(
		1,
		Math.floor(outputMaxTokens * OPENROUTER_REASONING_BUDGET_FRACTION),
	);
}

export function buildOpenRouterReasoningOptions(
	request: GatewayStreamRequest,
	context?: GatewayProviderContext,
): Record<string, unknown> | undefined {
	const reasoning = request.reasoning;
	if (!hasReasoningControls(reasoning)) {
		return undefined;
	}

	if (reasoning?.enabled === false) {
		return { effort: "none" };
	}

	// AI SDK 的 `maxOutputTokens` 仍限制整个响应。此 provider 选项
	// 通过限制 OpenRouter 推理 token 在该响应内预留空间。
	// 有显式推理预算时保留它们；否则从解析出的
	// 请求预算、模型目录输出上限或默认值推导上限。
	if (typeof reasoning?.budgetTokens === "number") {
		return { max_tokens: reasoning.budgetTokens };
	}

	if (reasoning?.enabled === true) {
		return {
			enabled: true,
			max_tokens: resolveOpenRouterReasoningMaxTokens(request, context),
		};
	}

	return undefined;
}
