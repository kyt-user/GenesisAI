import type {
	GatewayProviderManifest,
	ModelOperation,
	ModelToolName,
} from "@cline/shared";
import { BUILTIN_PROVIDER_MANIFESTS_BY_ID } from "./builtins";
import { normalizeProviderId } from "./ids";
import { modelRouteMatches } from "./model-facts";

export interface ModelToolSupportInput {
	providerId: string;
	modelId?: string;
}

function resolveModelRouteContext(
	manifest: GatewayProviderManifest,
	modelId: string | undefined,
): {
	modelId: string;
	family?: string;
	capabilities?: readonly string[];
	operation?: ModelOperation;
	modalities?: import("@cline/shared").ModelModalities;
} {
	const resolvedModelId = modelId?.trim() || manifest.defaultModelId;
	const model = manifest.models.find((entry) => entry.id === resolvedModelId);
	const family = model?.metadata?.family;
	return {
		modelId: resolvedModelId,
		family: typeof family === "string" ? family : undefined,
		capabilities: model?.capabilities,
		operation: model?.operation,
		modalities: model?.modalities,
	};
}

/** 直接从 provider manifest 解析模型工具能力。 */
export function providerManifestSupportsModelTool(
	manifest: GatewayProviderManifest,
	modelId: string | undefined,
	toolName: ModelToolName,
): boolean {
	const capability = manifest.modelToolCapabilities?.find(
		(entry) => entry.name === toolName,
	);
	if (!capability) {
		return false;
	}

	const routeContext = resolveModelRouteContext(manifest, modelId);
	if (
		capability.routes?.length &&
		!capability.routes.some((route) => modelRouteMatches(route, routeContext))
	) {
		return false;
	}

	return !capability.excludeRoutes?.some((route) =>
		modelRouteMatches(route, routeContext),
	);
}

/**
 * 解析已配置 Cline provider 的稳定原生模型工具支持。
 * 这有意描述的是 provider 执行支持，而非普通的
 * 函数/工具调用支持。内置 spec 是事实来源；vendor
 * 模块只把受支持的可移植工具翻译为 AI SDK 工具对象。
 */
export function supportsModelTool(
	input: ModelToolSupportInput,
	toolName: ModelToolName,
): boolean {
	const providerId = normalizeProviderId(input.providerId);
	const manifest = BUILTIN_PROVIDER_MANIFESTS_BY_ID[providerId];
	return manifest
		? providerManifestSupportsModelTool(manifest, input.modelId, toolName)
		: false;
}

/**
 * provider 是否为其至少部分模型声明了该模型工具，
 * 与所选模型无关。设置界面用它来
 * 解释 provider 级可用性；按请求的附加仍通过
 * supportsModelTool 结合所选模型解析。
 */
export function providerOffersModelTool(
	providerId: string,
	toolName: ModelToolName,
): boolean {
	const manifest =
		BUILTIN_PROVIDER_MANIFESTS_BY_ID[normalizeProviderId(providerId)];
	return (
		manifest?.modelToolCapabilities?.some(
			(capability) => capability.name === toolName,
		) === true
	);
}
