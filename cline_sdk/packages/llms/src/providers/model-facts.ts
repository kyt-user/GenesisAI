import type {
	GatewayModelRoute,
	GatewayProviderContext,
	GatewayReasoningFormat,
	GatewayStreamRequest,
	ModelOperation,
	ModelReasoningOption,
	ReasoningEffort,
} from "@cline/shared";
import { REASONING_LEVELS } from "@cline/shared";

const ACTIVE_REASONING_EFFORTS = REASONING_LEVELS.filter(
	(level): level is ReasoningEffort => level !== "none",
);

interface ModelReasoningControls {
	effort?: Extract<ModelReasoningOption, { type: "effort" }>;
	budget?: Extract<ModelReasoningOption, { type: "budget_tokens" }>;
	toggle: boolean;
	efforts: ReasoningEffort[];
	supportsOff: boolean;
	supportsDefault: boolean;
}

export function getModelReasoningControls(
	options: readonly ModelReasoningOption[] | undefined,
): ModelReasoningControls | undefined {
	if (options === undefined) {
		return undefined;
	}

	const effort = options.find((option) => option.type === "effort");
	const budget = options.find((option) => option.type === "budget_tokens");
	const toggle = options.some((option) => option.type === "toggle");
	const advertised = new Set(effort?.values ?? []);
	return {
		effort,
		budget,
		toggle,
		efforts: ACTIVE_REASONING_EFFORTS.filter((value) => advertised.has(value)),
		supportsOff: toggle || advertised.has("none"),
		supportsDefault: advertised.has("default"),
	};
}

export function normalizeReasoningEffort(
	effort: ReasoningEffort,
	supportedEfforts: readonly ReasoningEffort[],
): ReasoningEffort | undefined {
	if (supportedEfforts.length === 0) {
		return undefined;
	}
	if (supportedEfforts.includes(effort)) {
		return effort;
	}

	const requestedIndex = ACTIVE_REASONING_EFFORTS.indexOf(effort);
	return supportedEfforts.reduce((nearest, candidate) => {
		const nearestDistance = Math.abs(
			ACTIVE_REASONING_EFFORTS.indexOf(nearest) - requestedIndex,
		);
		const candidateDistance = Math.abs(
			ACTIVE_REASONING_EFFORTS.indexOf(candidate) - requestedIndex,
		);
		// 平局时保留更多能力。
		return candidateDistance <= nearestDistance ? candidate : nearest;
	});
}

export function resolveModelFamily(
	context: GatewayProviderContext,
): string | undefined {
	const family = context.model.metadata?.family;
	return typeof family === "string" ? family : undefined;
}

export function normalizeRoutingValue(value: string | undefined) {
	const normalized = value?.trim().toLowerCase();
	return normalized ? normalized : undefined;
}

function normalizedFamily(context: GatewayProviderContext): string {
	return normalizeRoutingValue(resolveModelFamily(context)) ?? "";
}

function normalizedModelId(
	request: Pick<GatewayStreamRequest, "modelId">,
): string {
	return normalizeRoutingValue(request.modelId) ?? "";
}

function geminiModelDescriptor(input: {
	request: Pick<GatewayStreamRequest, "modelId">;
	context: GatewayProviderContext;
}): string {
	return [
		input.request.modelId,
		input.context.model.id,
		input.context.model.name,
		input.context.model.metadata?.family,
	]
		.filter(Boolean)
		.join(" ")
		.toLowerCase();
}

function isProviderBaseOrigin(
	context: GatewayProviderContext,
	origin: string,
): boolean {
	const baseUrl = normalizeRoutingValue(
		context.config.baseUrl ?? context.provider.api,
	)?.replace(/\/+$/, "");
	if (!baseUrl) {
		return false;
	}

	try {
		return new URL(baseUrl).origin.toLowerCase() === origin;
	} catch {
		return baseUrl === origin || baseUrl.startsWith(`${origin}/`);
	}
}

function isAnthropicLineageValue(value: string | undefined): boolean {
	const normalized = normalizeRoutingValue(value);
	return normalized
		? normalized.includes("anthropic") || normalized.includes("claude")
		: false;
}

function isClaudeLineageValue(value: string | undefined): boolean {
	return normalizeRoutingValue(value)?.includes("claude") ?? false;
}

function isQwenLineageValue(value: string | undefined): boolean {
	const normalized = normalizeRoutingValue(value);
	return normalized
		? /(^|[/:._-])qwen(?:$|[/:._-]|\d)/.test(normalized)
		: false;
}

export function isAnthropicCompatibleModel(options: {
	modelId?: string;
	family?: string;
}): boolean {
	const family = normalizeRoutingValue(options.family);
	if (family) {
		return isAnthropicLineageValue(family);
	}

	return isAnthropicCompatibleModelId(options.modelId);
}

export function isAnthropicCompatibleModelId(
	modelId: string | undefined,
): boolean {
	if (!modelId) {
		return false;
	}

	return isAnthropicLineageValue(modelId);
}

export function isClaudeModelId(modelId: string | undefined): boolean {
	if (!modelId) {
		return false;
	}

	return isClaudeLineageValue(modelId);
}

export function isClaudeFableModelId(modelId: string | undefined): boolean {
	return normalizeRoutingValue(modelId)?.includes("claude-fable") ?? false;
}

// 已知的 pre-adaptive（自适应之前）Claude 家族，它们只接受手动的
// `thinking: {type: "enabled", budgetTokens}` 线格式（或完全不支持思考）：
// Claude Instant、2.x 以及 3.x 版本在前的 id。镜像 @ai-sdk/anthropic
// 能力查找中的 legacy 防护。
const CLAUDE_LEGACY_FAMILY_PATTERN =
	/claude-(?:instant(?:-|$)|v?2(?=$|[-.:])|3(?=$|[-.]))/;

// 匹配现代的名称在前（name-first）Anthropic id（"claude-sonnet-4-6"、
// "claude-opus-5"、"anthropic.claude-opus-4-8-v1:0"、"claude-sonnet-4.6"）。
// 次版本号上限为两位数字，因此日期戳后缀
// （"claude-sonnet-5-20260629"）不会被解析为版本号。
const CLAUDE_NAME_FIRST_VERSION_PATTERN =
	/claude-(?:opus|sonnet|haiku)-(\d+)(?:[.-](\d{1,2}))?(?=$|[-.:@])/;

/**
 * Claude 模型 id 的线格式年代，仅在目录 `reasoningOptions` 元数据
 * 不可用时（离线烘焙目录、用户手输的未列出 id 如 "claude-opus-4-6:1m"）
 * 用作回退。
 *
 * - "adaptive"：4.6+/5.x 模型；Anthropic API 对这些模型拒绝手动的
 *   `thinking: {type: "enabled", budgetTokens}` 格式。
 * - "legacy"：已知的 pre-4.6 家族，需要手动格式。
 * - "unknown-claude"：版本解析器不认识的 Claude id。
 *   调用方应将其视为比已知列表更新并优先采用 adaptive——
 *   这是生态趋同的前向兼容策略
 *   （vercel/ai#17804 针对 @ai-sdk/anthropic 的能力查找；opencode 的
 *   transform.ts 在 opus-4.7、sonnet-5 和 opus-5 反复落榜之后）。
 *   新版 Claude 发布都拒绝手动格式，因此落后于发布的允许列表
 *   会让每个推理请求变成硬性 API 错误。
 * - "not-claude"：非 Claude id（包括 Anthropic 兼容别名）
 *   对第三方端点保持保守。
 */
export type ClaudeThinkingEra =
	| "adaptive"
	| "legacy"
	| "unknown-claude"
	| "not-claude";

export function resolveClaudeThinkingEra(
	modelId: string | undefined,
): ClaudeThinkingEra {
	const normalized = normalizeRoutingValue(modelId);
	if (!normalized || !normalized.includes("claude")) {
		return "not-claude";
	}
	if (CLAUDE_LEGACY_FAMILY_PATTERN.test(normalized)) {
		return "legacy";
	}

	const match = CLAUDE_NAME_FIRST_VERSION_PATTERN.exec(normalized);
	if (match) {
		const major = Number(match[1]);
		const minor = match[2] !== undefined ? Number(match[2]) : 0;
		return major >= 5 || (major === 4 && minor >= 6) ? "adaptive" : "legacy";
	}

	return "unknown-claude";
}

export function isQwenModel(options: {
	modelId?: string;
	family?: string;
}): boolean {
	const family = normalizeRoutingValue(options.family);
	if (isQwenLineageValue(family)) {
		return true;
	}

	return isQwenLineageValue(options.modelId);
}

// OpenAI 推理时代的聊天模型：o 系列（o1/o3/o4，包括 -mini、
// -pro 和带日期的变体）与 gpt-5 家族（包括 gpt-5-chat）。
// 这些模型偏离经典 chat-completions 参数规则——最重的是
// 它们拒绝 `max_tokens` 并要求 `max_completion_tokens`。
// 检测采用 id 模式回退（镜像 legacy 扩展的
// OpenAI 处理器），因为 OpenAI 兼容端点接受自由格式的
// 用户手输模型 id，没有目录元数据可依赖。这些模式
// 要求非字母数字边界，因此 "gpt-4o" 或 "yolo1" 之类的 id 绝不
// 匹配，而 "openai/o3-mini" 之类的命名空间 id 则能匹配。
//
// 维护：
// - 如果 OpenAI 发布采用相同参数规则的新家族（例如 gpt-6），
//   在此添加模式，并在
//   `vendors/openai-compatible.test.ts` 的正/负例列表中加用例。
//   在此之前，失败模式是响亮、自描述的 OpenAI 400（"'max_tokens' is not
//   supported with this model. Use 'max_completion_tokens' instead."），
//   而不是静默故障。
// - 保持每个模式两侧的边界锚定；放松它
//   有把无关第三方模型的参数改名的风险。
// - 只有 `vendors/openai-compatible.ts` 中的
//   `withMaxCompletionTokensForReasoningModels` 消费它。如果将来
//   `@ai-sdk/openai-compatible` 自行映射 `max_completion_tokens`
//   （如 `@ai-sdk/openai` 已经做的那样），删除该转换和此辅助函数。
const OPENAI_O_SERIES_MODEL_ID_PATTERN = /(^|[^a-z0-9])o[134](?=$|[^a-z0-9])/;
const OPENAI_GPT5_FAMILY_MODEL_ID_PATTERN =
	/(^|[^a-z0-9])gpt-?5(?=$|[^a-z0-9])/;

export function isOpenAIReasoningEraModelId(
	modelId: string | undefined,
): boolean {
	const normalized = normalizeRoutingValue(modelId);
	if (!normalized) {
		return false;
	}

	return (
		OPENAI_O_SERIES_MODEL_ID_PATTERN.test(normalized) ||
		OPENAI_GPT5_FAMILY_MODEL_ID_PATTERN.test(normalized)
	);
}

export function resolveGeminiThinkingMode(input: {
	request: Pick<GatewayStreamRequest, "modelId">;
	context: GatewayProviderContext;
}): "level" | "budget" | undefined {
	const controls = getModelReasoningControls(
		input.context.model.reasoningOptions,
	);
	if (controls) {
		return controls.effort
			? "level"
			: controls.budget || controls.toggle
				? "budget"
				: undefined;
	}

	// Legacy/离线目录尚未携带 reasoning_options。把它们的
	// 线格式选择集中在一个回退边界；在线模型使用上方的元数据。
	const descriptor = geminiModelDescriptor(input);
	return /(^|[/\s])gemini-3([.-]|$)/.test(descriptor)
		? "level"
		: /(^|[/\s])gemini-2\.5([-\s]|$)/.test(descriptor) ||
				descriptor.includes("gemini-flash-latest")
			? "budget"
			: undefined;
}

function modelFamilyMatches(
	family: string | undefined,
	routeFamily: string | undefined,
): boolean {
	const normalizedFamily = normalizeRoutingValue(family);
	const normalizedRouteFamily = normalizeRoutingValue(routeFamily);
	if (!normalizedFamily || !normalizedRouteFamily) {
		return false;
	}
	if (normalizedFamily === normalizedRouteFamily) {
		return true;
	}
	return normalizedRouteFamily === "qwen"
		? isQwenLineageValue(normalizedFamily)
		: false;
}

export function modelRouteMatches(
	route: GatewayModelRoute,
	options: {
		modelId?: string;
		family?: string;
		capabilities?: readonly string[];
		operation?: ModelOperation;
		modalities?: import("@cline/shared").ModelModalities;
	},
): boolean {
	if (
		"requiredCapability" in route &&
		route.requiredCapability &&
		!options.capabilities?.includes(route.requiredCapability)
	) {
		return false;
	}

	switch (route.matcher) {
		case "anthropic-compatible":
			return isAnthropicCompatibleModel(options);
		case "model-operation":
			// 语言是标准默认操作；只有专用传输才需要
			// 显式声明操作。
			return (options.operation ?? "language") === route.operation;
		case "model-output-modality":
			return options.modalities?.output.includes(route.modality) ?? false;
		case "model-family":
			return modelFamilyMatches(options.family, route.family);
		case "model-id":
			return (
				normalizeRoutingValue(options.modelId) ===
				normalizeRoutingValue(route.modelId)
			);
	}
}

export function providerReasoningRouteMatches(
	format: GatewayReasoningFormat,
	request: Pick<GatewayStreamRequest, "modelId">,
	context: GatewayProviderContext,
): boolean {
	const reasoning = context.provider.metadata?.routing?.reasoning;
	if (reasoning?.format !== format) {
		return false;
	}

	return reasoning.routes.some((route) =>
		modelRouteMatches(route, {
			modelId: request.modelId,
			family: resolveModelFamily(context),
			capabilities: context.model.capabilities,
		}),
	);
}

export function isGlmModel(
	request: Pick<GatewayStreamRequest, "modelId">,
	context: GatewayProviderContext,
): boolean {
	const family = normalizedFamily(context);

	// 动态 provider 回退：一些路由/本地目录只提供 id。
	return family.includes("glm") || normalizedModelId(request).includes("glm");
}

export function isMiniMaxM3Model(
	request: Pick<GatewayStreamRequest, "modelId">,
	_context: GatewayProviderContext,
): boolean {
	const modelId = normalizedModelId(request);

	return modelId === "minimax-m3" || modelId === "minimax/minimax-m3";
}

export function isKimiK26Family(context: GatewayProviderContext): boolean {
	return normalizedFamily(context) === "kimi-k2.6";
}

export function isMoonshotKimiModelIdFallback(
	request: Pick<GatewayStreamRequest, "modelId">,
): boolean {
	// Moonshot 路由模型 id 的动态 provider 回退：当家族
	// 元数据缺失或不够具体时。
	return normalizedModelId(request).includes("moonshotai/kimi-");
}

export function isDeepSeekFamily(context: GatewayProviderContext): boolean {
	return normalizedFamily(context).includes("deepseek");
}

/**
 * 解析出的模型是否声明支持图片输入（其
 * 网关能力中包含 `"images"`）。完全没有能力数据的模型（例如
 * 在任何目录之外解析出的 id）放开通过：保留图片，
 * 而不是从一个可能有能力的模型中隐藏它们。
 */
export function modelSupportsImageInput(
	context: GatewayProviderContext,
): boolean {
	const capabilities = context.model.capabilities;
	if (!capabilities) {
		return true;
	}
	return capabilities.includes("images");
}

export function getReasoningDefaultOnMetadata(
	context: GatewayProviderContext,
): boolean | undefined {
	const value = context.model.metadata?.reasoningDefaultOn;
	return typeof value === "boolean" ? value : undefined;
}

export function isOllamaQwen3ModelIdFallback(
	request: Pick<GatewayStreamRequest, "providerId" | "modelId">,
): boolean {
	// 本地 Ollama 模型从 /api/tags 发现，通常只提供
	// 如 "qwen3-coder:30b" 之类的名称。此回退用于
	// modelReasoningDefaultsOn 在无目录元数据时的判断。
	return (
		request.providerId === "ollama" &&
		normalizedModelId(request).includes("qwen3")
	);
}

export function isCerebrasProvider(
	request: Pick<GatewayStreamRequest, "providerId">,
	context: GatewayProviderContext,
): boolean {
	const providerIds = [
		request.providerId,
		context.config.providerId,
		context.provider.id,
	].map((id) => id.toLowerCase());

	return (
		providerIds.includes("cerebras") ||
		isProviderBaseOrigin(context, "https://api.cerebras.ai")
	);
}

export function modelReasoningDefaultsOn(options: {
	request: Pick<GatewayStreamRequest, "providerId" | "modelId">;
	context: GatewayProviderContext;
}): boolean {
	return (
		getReasoningDefaultOnMetadata(options.context) ??
		isOllamaQwen3ModelIdFallback(options.request)
	);
}
