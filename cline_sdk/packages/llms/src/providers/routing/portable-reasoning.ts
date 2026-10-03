import type { GatewayStreamRequest } from "@cline/shared";
import type { CallSettings } from "ai";

export type AiSdkReasoning = NonNullable<CallSettings["reasoning"]>;

const PORTABLE_REASONING_PROVIDERS = new Set([
	"anthropic",
	"bedrock",
	"deepseek",
	"fireworks",
	"gemini",
	"google",
	"groq",
	"openai-native",
	"openai-codex",
	"ollama",
	"vertex",
	"xai",
]);

const NON_PORTABLE_REASONING_PROVIDERS = new Set([
	"claude-code",
	"dify",
	"mistral",
	"opencode",
	"sapaicore",
]);

/** Resolve reasoning intent owned by the AI SDK's portable top-level option. */
export function resolvePortableReasoning(
	request: GatewayStreamRequest,
): AiSdkReasoning | undefined {
	const reasoning = request.reasoning;
	if (!reasoning) {
		return undefined;
	}
	const fullySupported = PORTABLE_REASONING_PROVIDERS.has(request.providerId);
	if (reasoning.enabled === false) {
		return fullySupported ? "none" : undefined;
	}
	if (typeof reasoning.budgetTokens === "number") {
		return undefined;
	}
	if (reasoning.effort) {
		if (NON_PORTABLE_REASONING_PROVIDERS.has(request.providerId)) {
			return undefined;
		}
		return reasoning.effort === "max" ? "xhigh" : reasoning.effort;
	}
	return reasoning.enabled === true &&
		!NON_PORTABLE_REASONING_PROVIDERS.has(request.providerId)
		? "medium"
		: undefined;
}

/**
 * 在组合 provider options 之前移除可移植意图。当 providerOptions
 * 中也出现推理控制时，AI SDK 会忽略顶层 reasoning。
 */
export function withoutPortableReasoning(
	request: GatewayStreamRequest,
): GatewayStreamRequest {
	const normalizedRequest =
		request.reasoning?.enabled === false &&
		(request.reasoning.effort !== undefined ||
			request.reasoning.budgetTokens !== undefined)
			? { ...request, reasoning: { enabled: false } }
			: request;
	return resolvePortableReasoning(normalizedRequest)
		? { ...normalizedRequest, reasoning: undefined }
		: normalizedRequest;
}
