import { isClineProvider } from "@cline/shared";

export type ProviderOptionsPatch = Record<string, Record<string, unknown>>;

export function toProviderOptionsKey(providerId: string): string {
	return providerId.replace(/-([a-z0-9])/gi, (_match, char: string) =>
		char.toUpperCase(),
	);
}

export function createEphemeralCacheControl() {
	return {
		cache_control: { type: "ephemeral" as const },
	};
}

/**
 * 为 provider id 定位 AI SDK provider 名称桶，并在两者
 * 不同时定位其 camelCase 别名桶（例如 `vercel-ai-gateway` +
 * `vercelAiGateway`）。
 *
 * 桶名必须与 AI SDK provider 的 `name` 一致，因为
 * openai-compatible 模型只从 `providerOptions[<name>]`
 * （及其 camelCase 别名）应用请求体透传。对几乎所有
 * provider，名称就是网关 provider id，但两个 Cline 网关 id
 * （`cline` 和 `cline-pass`）都由共享的 "cline" AI SDK provider
 * 服务（见 `createClineProviderModule`）并命中同一个 Cline API，
 * 因此它们的选项都以共享的 `cline` 桶为键。
 */
export function buildProviderAndAliasPatch(options: {
	providerId: string;
	providerOptionsKey: string;
	bucketOptions: Record<string, unknown>;
}): ProviderOptionsPatch {
	const { bucketOptions } = options;
	const providerId = isClineProvider(options.providerId)
		? "cline"
		: options.providerId;
	const providerOptionsKey = isClineProvider(options.providerId)
		? "cline"
		: options.providerOptionsKey;
	const needsAlias =
		providerOptionsKey !== providerId && providerOptionsKey !== "anthropic";
	return {
		[providerId]: bucketOptions,
		...(needsAlias ? { [providerOptionsKey]: bucketOptions } : {}),
	};
}

export function buildThinkingPatch(options: {
	providerId: string;
	providerOptionsKey: string;
	thinkingType: "enabled" | "disabled";
}): ProviderOptionsPatch {
	const bucketOptions = { thinking: { type: options.thinkingType } };
	return {
		...buildProviderAndAliasPatch({
			providerId: options.providerId,
			providerOptionsKey: options.providerOptionsKey,
			bucketOptions,
		}),
		openaiCompatible: bucketOptions,
	};
}
