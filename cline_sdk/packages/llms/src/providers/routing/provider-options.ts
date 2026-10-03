import type {
	GatewayProviderContext,
	GatewayStreamRequest,
} from "@cline/shared";
import { buildAnthropicProviderOptions } from "./anthropic-compatible";
import { buildCompatibleProviderOptions } from "./generic-compatible";
import { withoutPortableReasoning } from "./portable-reasoning";
import {
	buildProviderOptionRulePatches,
	matchProviderOptionRules,
	PROVIDER_OPTION_RULES,
	resolveProviderOptionSuppressions,
} from "./provider-option-rules";
import {
	type AiSdkProviderOptionsTarget,
	inferProviderOptionsTarget,
	type ProviderOptionMatchInput,
} from "./provider-options-types";
import { normalizeReasoningRequest } from "./reasoning-options";
import { type ProviderOptionsPatch, toProviderOptionsKey } from "./utils";

export type { AiSdkProviderOptionsTarget } from "./provider-options-types";
export type { ProviderOptionsPatch } from "./utils";

/**
 * 按顺序合并补丁。后应用的补丁按桶键覆盖先前的；
 * 嵌套对象值是替换，而非深度合并。
 */
export function mergeProviderOptionPatches(
	patches: ReadonlyArray<ProviderOptionsPatch | undefined>,
): Record<string, unknown> {
	const result: Record<string, Record<string, unknown>> = {};
	for (const patch of patches) {
		if (!patch) {
			continue;
		}
		for (const [bucket, options] of Object.entries(patch)) {
			result[bucket] = { ...(result[bucket] ?? {}), ...options };
		}
	}
	return result;
}

function buildBaseProviderOptionsPatch(
	compatibleOptions: Record<string, unknown>,
	anthropicOptions: Record<string, unknown>,
): ProviderOptionsPatch {
	return {
		anthropic: anthropicOptions,
		openaiCompatible: compatibleOptions,
	};
}

/**
 * 从命名的 provider/模型家族规则组合 AI SDK `providerOptions`。
 *
 * `provider-option-rules.ts` 中的规则表是特殊 provider
 * 和模型家族的行为矩阵。保持组合器简单：先构建共享
 * 桶，再按顺序合并规则补丁。
 *
 * 路由职责边界：
 * - 网关模型能力说明模型能做什么，例如推理。
 * - 模型元数据记录稳定的已知模型事实，例如
 *   `reasoningDefaultOn`。
 * - Provider 元数据记录稳定的 provider 策略，例如提示词缓存。
 * - Provider 选项规则只把不可移植的请求意图编码为
 *   provider 线格式，例如精确预算和原生开关对象。
 */
export function composeAiSdkProviderOptions(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
	target: AiSdkProviderOptionsTarget = inferProviderOptionsTarget(
		request.providerId,
	),
): Record<string, unknown> {
	const normalizedRequest = normalizeReasoningRequest(
		withoutPortableReasoning(request),
		context,
	);
	const providerOptionsKey = toProviderOptionsKey(normalizedRequest.providerId);
	const matchInput: ProviderOptionMatchInput = {
		request: normalizedRequest,
		context,
		providerOptionsKey,
		target,
	};
	const matchedRules = matchProviderOptionRules(
		PROVIDER_OPTION_RULES,
		matchInput,
	);
	const suppressions = resolveProviderOptionSuppressions(matchedRules);
	const compatibleOptions = buildCompatibleProviderOptions({
		request: normalizedRequest,
		context,
		target,
		suppressions,
	});
	const anthropicOptions = buildAnthropicProviderOptions(
		normalizedRequest,
		context,
	);
	const buildInput = {
		...matchInput,
		compatibleOptions,
		anthropicOptions,
		suppressions,
	};

	return mergeProviderOptionPatches([
		buildBaseProviderOptionsPatch(compatibleOptions, anthropicOptions),
		...buildProviderOptionRulePatches(matchedRules, buildInput),
	]);
}
