import {
	type GatewayModelRoute,
	type GatewayPromptCacheStrategy,
	type GatewayProviderContext,
	type GatewayProviderManifest,
	type GatewayProviderMetadata,
	type GatewayStreamRequest,
	resolveReasoningBudgetFromRatio,
} from "@cline/shared";
import {
	getModelReasoningControls,
	isAnthropicCompatibleModel,
	isQwenModel,
	modelRouteMatches,
	resolveClaudeThinkingEra,
	resolveModelFamily,
} from "../model-facts";
import { createEphemeralCacheControl, toProviderOptionsKey } from "./utils";

const ANTHROPIC_DEFAULT_THINKING_BUDGET_TOKENS = 1024;
const ANTHROPIC_MAX_THINKING_BUDGET_TOKENS = 128000;

export type AnthropicReasoningRequestPolicy =
	| { kind: "none" }
	| { kind: "anthropic-manual" }
	| { kind: "anthropic-adaptive" };

/**
 * Provider 元数据拥有行为路由。「anthropic-compatible」是 Claude/Anthropic
 * 谱系的一个路由匹配器；提示词缓存和推理各自独立决定是否使用它。
 */

const ANTHROPIC_COMPATIBLE_ROUTE: GatewayModelRoute = {
	matcher: "anthropic-compatible",
};

// Qwen 缓存支持因模型而异；直接的 Dashscope/OpenRouter 目录
// 只有在其模型元数据包含 prompt-cache 支持后才会匹配此路由。
const QWEN_PROMPT_CACHE_ROUTE: GatewayModelRoute = {
	matcher: "model-family",
	family: "qwen",
	requiredCapability: "prompt-cache",
};

function createAnthropicRoutingMetadata(options?: {
	promptCacheRoutes?: GatewayModelRoute[];
	reasoningRoutes?: GatewayModelRoute[];
}): GatewayProviderMetadata {
	const promptCacheRoutes: GatewayModelRoute[] = options?.promptCacheRoutes ?? [
		ANTHROPIC_COMPATIBLE_ROUTE,
	];
	const reasoningRoutes: GatewayModelRoute[] = options?.reasoningRoutes ?? [
		ANTHROPIC_COMPATIBLE_ROUTE,
	];
	return {
		routing: {
			...(promptCacheRoutes.length > 0
				? {
						promptCache: {
							format: "anthropic-cache-control",
							routes: promptCacheRoutes.map((route) => ({ ...route })),
						},
					}
				: {}),
			...(reasoningRoutes.length > 0
				? {
						reasoning: {
							format: "anthropic-thinking",
							routes: reasoningRoutes.map((route) => ({ ...route })),
						},
					}
				: {}),
		},
	};
}

export const ANTHROPIC_ROUTING_METADATA = createAnthropicRoutingMetadata();

export const QWEN_CACHE_ROUTING_METADATA = createAnthropicRoutingMetadata({
	promptCacheRoutes: [QWEN_PROMPT_CACHE_ROUTE],
	reasoningRoutes: [],
});

export const ANTHROPIC_AND_QWEN_CACHE_ROUTING_METADATA =
	createAnthropicRoutingMetadata({
		promptCacheRoutes: [ANTHROPIC_COMPATIBLE_ROUTE, QWEN_PROMPT_CACHE_ROUTE],
	});

export function createPromptCacheProviderOptions(
	providerId: string,
	includeAnthropic: boolean,
) {
	const providerOptions: Record<string, unknown> = {
		openaiCompatible: createEphemeralCacheControl(),
		[providerId]: createEphemeralCacheControl(),
	};

	const providerOptionsKey = toProviderOptionsKey(providerId);
	if (providerOptionsKey !== providerId) {
		providerOptions[providerOptionsKey] = createEphemeralCacheControl();
	}
	if (includeAnthropic) {
		providerOptions.anthropic = createEphemeralCacheControl();
	}

	return providerOptions;
}

export function applyPromptCacheToLastTextPart(
	message: Record<string, unknown> | undefined,
	providerId: string,
	includeAnthropic: boolean,
): void {
	if (!message) {
		return;
	}

	const content = message.content;
	if (typeof content === "string") {
		const cachedContent: Record<string, unknown>[] = [
			{
				type: "text",
				text: content,
				providerOptions: createPromptCacheProviderOptions(
					providerId,
					includeAnthropic,
				),
			},
		];
		if (!includeAnthropic) {
			// 保持非 Anthropic 的 OpenAI 兼容请求为多部分格式，使
			// cache_control 保留在内容部分上，而不是被折叠
			// 为消息元数据。Anthropic 拒绝纯空白文本块。
			cachedContent.push({ type: "text", text: " " });
		}
		message.content = cachedContent;
		return;
	}

	if (!Array.isArray(content)) {
		return;
	}

	const textPartCount = content.filter(
		(part) =>
			part &&
			typeof part === "object" &&
			(part as { type?: unknown }).type === "text",
	).length;

	for (let i = content.length - 1; i >= 0; i--) {
		const part = content[i];
		if (
			part &&
			typeof part === "object" &&
			(part as { type?: unknown }).type === "text"
		) {
			const needsFiller = textPartCount === 1 && !includeAnthropic;
			content[i] = {
				...(part as Record<string, unknown>),
				providerOptions: createPromptCacheProviderOptions(
					providerId,
					includeAnthropic,
				),
			};
			if (needsFiller) {
				content.push({ type: "text", text: " " });
			}
			return;
		}
	}
}

export function shouldApplyPromptCache(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): boolean {
	return resolvePromptCacheRoute(request, context) !== undefined;
}

function shouldApplyAnthropicCacheBucket(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): boolean {
	return (
		resolvePromptCacheRoute(request, context)?.matcher ===
		"anthropic-compatible"
	);
}

function resolveLegacyPromptCacheStrategy(
	provider: GatewayProviderManifest,
): GatewayPromptCacheStrategy | undefined {
	return provider.metadata?.promptCacheStrategy === "anthropic-automatic"
		? "anthropic-automatic"
		: undefined;
}

function resolveLegacyPromptCacheRoute(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): GatewayModelRoute | undefined {
	if (
		resolveLegacyPromptCacheStrategy(context.provider) !== "anthropic-automatic"
	) {
		return undefined;
	}

	const family = resolveModelFamily(context);
	if (
		isAnthropicCompatibleModel({
			modelId: request.modelId,
			family,
		})
	) {
		return { matcher: "anthropic-compatible" };
	}

	// `promptCacheStrategy` 早于显式路由，历史上把
	// Qwen id 当作 Anthropic 兼容处理。保留这种选择加入的自定义 provider
	// 行为，但保持返回的路由为非 Anthropic，使 Qwen 仍获得
	// 新路由路径使用的 OpenAI 兼容 cache_control 格式。
	if (isQwenModel({ modelId: request.modelId, family })) {
		return family
			? { matcher: "model-family", family }
			: { matcher: "model-id", modelId: request.modelId };
	}

	return undefined;
}

function resolveLegacyReasoningRoute(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): GatewayModelRoute | undefined {
	if (
		resolveLegacyPromptCacheStrategy(context.provider) !== "anthropic-automatic"
	) {
		return undefined;
	}

	const family = resolveModelFamily(context);
	return isAnthropicCompatibleModel({
		modelId: request.modelId,
		family,
	})
		? { matcher: "anthropic-compatible" }
		: undefined;
}

function resolveUnroutedAnthropicReasoningRoute(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): GatewayModelRoute | undefined {
	if (context.provider.metadata?.routing) {
		return undefined;
	}

	const family = resolveModelFamily(context);
	return isAnthropicCompatibleModel({
		modelId: request.modelId,
		family,
	})
		? { matcher: "anthropic-compatible" }
		: undefined;
}

export function resolvePromptCacheRoute(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): GatewayModelRoute | undefined {
	const promptCache = context.provider.metadata?.routing?.promptCache;
	if (promptCache) {
		if (promptCache.format !== "anthropic-cache-control") {
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

	return resolveLegacyPromptCacheRoute(request, context);
}

export function resolveReasoningRoute(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): GatewayModelRoute | undefined {
	const reasoning = context.provider.metadata?.routing?.reasoning;
	if (!reasoning) {
		return (
			resolveLegacyReasoningRoute(request, context) ??
			resolveUnroutedAnthropicReasoningRoute(request, context)
		);
	}
	if (reasoning.format !== "anthropic-thinking") {
		return undefined;
	}

	return reasoning.routes.find((route) =>
		modelRouteMatches(route, {
			modelId: request.modelId,
			family: resolveModelFamily(context),
			capabilities: context.model.capabilities,
		}),
	);
}

export function shouldEmitAnthropicReasoning(
	context: GatewayProviderContext,
): boolean {
	const capabilities = context.model.capabilities;
	return !capabilities || capabilities.includes("reasoning");
}

export function resolveAnthropicReasoningRequestPolicy(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): AnthropicReasoningRequestPolicy {
	if (
		!resolveReasoningRoute(request, context) ||
		!shouldEmitAnthropicReasoning(context)
	) {
		return { kind: "none" };
	}

	const controls = getModelReasoningControls(context.model.reasoningOptions);
	if (controls) {
		// 声明 effort 控制的模型属于 adaptive 时代。它们的 API
		// 拒绝手动线格式（thinking.type "enabled"），即使同时
		// 声明了 budget_tokens 控制，因此数值请求预算
		// 不能强制使用手动思考；预算会被忽略，
		// 转而采用 adaptive。
		if (controls.effort) {
			return { kind: "anthropic-adaptive" };
		}
		return controls.budget || controls.toggle
			? { kind: "anthropic-manual" }
			: { kind: "none" };
	}

	// 无目录推理元数据：回退到基于 id 的年代策略
	// （见 resolveClaudeThinkingEra）。4.6+/5.x Claude id 需要 adaptive；
	// 未知 Claude id 默认 adaptive 以保持前向兼容，
	// 与 @ai-sdk/anthropic 的能力默认值一致——但请求
	// 携带显式数值预算时除外，那表明自定义
	// 端点期望手动格式。Legacy Claude 家族和
	// 非 Claude 的 Anthropic 兼容 id 保持手动，这是对
	// 第三方端点安全的格式。
	const era = resolveClaudeThinkingEra(request.modelId);
	const hasExplicitBudget = typeof request.reasoning?.budgetTokens === "number";
	return era === "adaptive" || (era === "unknown-claude" && !hasExplicitBudget)
		? { kind: "anthropic-adaptive" }
		: { kind: "anthropic-manual" };
}

export function buildAnthropicProviderOptions(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
) {
	const explicitBudget =
		request.reasoning?.enabled === false
			? undefined
			: request.reasoning?.budgetTokens;

	// 仅 effort 和启用/禁用意图走可移植的顶层
	// reasoning 选项，因此只有显式预算请求才会到达此线格式。
	// Adaptive 时代模型即使收到数值预算也拒绝手动格式
	// （thinking.type "enabled"），所以请求预算会被忽略，
	// 转而采用 adaptive 思考。
	let thinking: Record<string, unknown> | undefined;
	let effort: string | undefined;
	if (typeof explicitBudget === "number") {
		const policy = resolveAnthropicReasoningRequestPolicy(request, context);
		if (policy.kind === "anthropic-adaptive") {
			thinking = { type: "adaptive" };
			effort = request.reasoning?.effort;
		} else if (policy.kind === "anthropic-manual") {
			const budgetTokens = resolveAnthropicManualBudget(request, context);
			if (budgetTokens !== undefined) {
				thinking = { type: "enabled", budgetTokens };
			}
		}
	}

	return {
		...(effort ? { effort } : {}),
		...(thinking ? { thinking } : {}),
		...(shouldApplyAnthropicCacheBucket(request, context)
			? createEphemeralCacheControl()
			: {}),
	};
}

export function resolveAnthropicCompatibleReasoningBudget(options: {
	modelId?: string;
	family?: string;
	effort?: string;
	maxTokens?: number;
	explicitBudgetTokens?: number;
}) {
	const minimumBudget = ANTHROPIC_DEFAULT_THINKING_BUDGET_TOKENS;
	const maximumBudget = Math.min(
		ANTHROPIC_MAX_THINKING_BUDGET_TOKENS,
		typeof options.maxTokens === "number"
			? options.maxTokens - 1
			: ANTHROPIC_MAX_THINKING_BUDGET_TOKENS,
	);
	if (maximumBudget < 1) {
		return undefined;
	}
	const defaultBudget = Math.min(minimumBudget, maximumBudget);
	if (
		typeof options.explicitBudgetTokens === "number" &&
		options.explicitBudgetTokens > 0
	) {
		return Math.min(
			Math.max(Math.floor(options.explicitBudgetTokens), defaultBudget),
			maximumBudget,
		);
	}

	if (
		(!options.modelId && !options.family) ||
		!isAnthropicCompatibleModel({
			modelId: options.modelId,
			family: options.family,
		})
	) {
		return undefined;
	}
	if (!options.effort || typeof options.maxTokens !== "number") {
		return defaultBudget;
	}

	return (
		resolveReasoningBudgetFromRatio({
			// Anthropic 思考与可见输出共享 max_tokens。
			effort: options.effort === "max" ? "xhigh" : options.effort,
			maxBudget: maximumBudget,
			minimumBudget: defaultBudget,
		}) ?? defaultBudget
	);
}

function resolveAnthropicManualBudget(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): number | undefined {
	const explicitBudgetTokens = request.reasoning?.budgetTokens;
	if (
		typeof explicitBudgetTokens !== "number" &&
		context.model.reasoningOptions !== undefined
	) {
		return undefined;
	}
	return resolveAnthropicCompatibleReasoningBudget({
		modelId: request.modelId,
		family: resolveModelFamily(context),
		effort: request.reasoning?.effort,
		maxTokens: request.maxTokens,
		explicitBudgetTokens,
	});
}

export function buildAnthropicCompatibleReasoningOptions(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
) {
	const policy = resolveAnthropicReasoningRequestPolicy(request, context);
	if (
		policy.kind === "none" ||
		(!request.reasoning?.enabled &&
			!request.reasoning?.effort &&
			typeof request.reasoning?.budgetTokens !== "number")
	) {
		return undefined;
	}

	const budgetTokens = resolveAnthropicManualBudget(request, context);
	if (request.reasoning?.enabled === false) {
		return { enabled: false };
	}
	const reasoning: Record<string, unknown> = {};

	if (request.reasoning?.enabled === true) {
		reasoning.enabled = true;
	}
	if (
		policy.kind === "anthropic-manual" &&
		typeof budgetTokens === "number" &&
		budgetTokens >= 0
	) {
		reasoning.max_tokens = budgetTokens;
	}

	return Object.keys(reasoning).length > 0 ? reasoning : undefined;
}

export function buildGatewayReasoningOptions(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
) {
	if (
		request.reasoning?.enabled === undefined &&
		!request.reasoning?.effort &&
		typeof request.reasoning?.budgetTokens !== "number"
	) {
		return undefined;
	}

	const policy = resolveAnthropicReasoningRequestPolicy(request, context);
	const reasoningRoute = resolveReasoningRoute(request, context);
	const family = resolveModelFamily(context);
	const shouldSuppressUnsupportedRoutedReasoning =
		policy.kind === "none" && reasoningRoute !== undefined;
	const shouldSuppressUnroutedAnthropicLikeReasoning =
		policy.kind === "none" &&
		reasoningRoute === undefined &&
		(shouldApplyPromptCache(request, context) ||
			isQwenModel({
				modelId: request.modelId,
				family,
			}) ||
			isAnthropicCompatibleModel({
				modelId: request.modelId,
				family,
			}));
	if (
		shouldSuppressUnsupportedRoutedReasoning ||
		shouldSuppressUnroutedAnthropicLikeReasoning
	) {
		return undefined;
	}

	const budgetTokens =
		request.reasoning?.enabled === false
			? undefined
			: policy.kind === "anthropic-manual"
				? resolveAnthropicManualBudget(request, context)
				: request.reasoning?.budgetTokens;
	const reasoning: Record<string, unknown> = {
		...(request.reasoning?.enabled === true
			? { enabled: true }
			: request.reasoning?.enabled === false
				? { enabled: false }
				: {}),
	};

	if (typeof budgetTokens === "number" && budgetTokens >= 0) {
		reasoning.max_tokens = budgetTokens;
	}

	return Object.keys(reasoning).length > 0 ? reasoning : undefined;
}
