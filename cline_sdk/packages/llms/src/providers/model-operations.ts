import type {
	GatewayModelDefinition,
	GatewayModelOperationCapability,
	GatewayProviderManifest,
	GatewayProviderMetadata,
	ModelInfo,
	ModelModalities,
	ModelOperation,
	ModelOperationMode,
} from "@cline/shared";
import { modelRouteMatches } from "./model-facts";

const IMAGE_LANGUAGE_OPERATION: GatewayModelOperationCapability = {
	operation: "language",
	outputModalities: ["text", "image"],
};

const IMAGE_GENERATION_OPERATION: GatewayModelOperationCapability = {
	operation: "image-generation",
	inputModalities: ["text", "image"],
	outputModalities: ["image"],
};

interface BuiltinTranscriptionOperation {
	transport: NonNullable<GatewayProviderMetadata["transcriptionTransport"]>;
	modes: readonly ModelOperationMode[];
	streamingModels?: readonly string[];
}

export const BUILTIN_TRANSCRIPTION_TRANSPORTS = {
	"openai-native": {
		transport: "openai-native",
		modes: ["batch", "streaming"],
		streamingModels: ["gpt-realtime-whisper"],
	},
	"vercel-ai-gateway": {
		transport: "vercel-ai-gateway",
		modes: ["batch", "streaming"],
	},
	elevenlabs: {
		transport: "elevenlabs",
		modes: ["batch", "streaming"],
		streamingModels: ["scribe_v2_realtime"],
	},
	evroc: { transport: "openai-compatible", modes: ["batch"] },
	groq: { transport: "openai-compatible", modes: ["batch"] },
	mistral: { transport: "openai-compatible", modes: ["batch"] },
	nearai: { transport: "openai-compatible", modes: ["batch"] },
	"privatemode-ai": {
		transport: "openai-compatible",
		modes: ["batch"],
	},
	scaleway: { transport: "openai-compatible", modes: ["batch"] },
} as const satisfies Readonly<Record<string, BuiltinTranscriptionOperation>>;

/** SDK-supported live models absent from the shared external catalog. */
export function getBuiltinStreamingTranscriptionModels(
	providerId: string,
): Record<string, ModelInfo> {
	const config = (
		BUILTIN_TRANSCRIPTION_TRANSPORTS as Readonly<
			Record<string, BuiltinTranscriptionOperation>
		>
	)[providerId];
	return Object.fromEntries(
		(config?.streamingModels ?? []).map((id) => [
			id,
			{
				id,
				name: id,
				operation: "transcription",
				operationModes: ["streaming"],
				modalities: { input: ["audio"], output: ["text"] },
			},
		]),
	);
}

/**
 * 有显式验证过的非文本操作的内置传输。
 *
 * 有意不参考 provider 家族。特别是，OpenAI 兼容的
 * 聊天端点不意味着支持独立的 `/images/generations` 传输。
 * 新 provider 只有在其具体适配器实现该操作后才在此加入。
 */
const BUILTIN_MEDIA_OPERATION_CAPABILITIES: Readonly<
	Record<string, readonly GatewayModelOperationCapability[]>
> = {
	"openai-native": [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
	gemini: [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
	vertex: [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
	bedrock: [IMAGE_GENERATION_OPERATION],
	openrouter: [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
	"vercel-ai-gateway": [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
	digitalocean: [IMAGE_GENERATION_OPERATION],
	xai: [IMAGE_GENERATION_OPERATION],
	cline: [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
	"cline-pass": [IMAGE_LANGUAGE_OPERATION, IMAGE_GENERATION_OPERATION],
};

/**
 * 按 provider ID 键控的可执行操作声明。
 *
 * 转录能力派生自传输声明，使 provider 不会意外
 * 声明没有匹配执行器的模型，
 * 或注册被目录过滤掉的执行器。
 */
export const BUILTIN_MODEL_OPERATION_CAPABILITIES: Readonly<
	Record<string, readonly GatewayModelOperationCapability[]>
> = Object.fromEntries(
	[
		...new Set([
			...Object.keys(BUILTIN_MEDIA_OPERATION_CAPABILITIES),
			...Object.keys(BUILTIN_TRANSCRIPTION_TRANSPORTS),
		]),
	].map((providerId) => {
		const transcription = (
			BUILTIN_TRANSCRIPTION_TRANSPORTS as Readonly<
				Record<string, BuiltinTranscriptionOperation>
			>
		)[providerId];
		return [
			providerId,
			[
				...(BUILTIN_MEDIA_OPERATION_CAPABILITIES[providerId] ?? []),
				...(transcription
					? [
							{
								operation: "transcription" as const,
								modes: transcription.modes,
								inputModalities: ["audio" as const],
								outputModalities: ["text" as const],
							},
						]
					: []),
			],
		] as const;
	}),
);

export function resolveModelOperation(model: {
	operation?: ModelOperation;
}): ModelOperation {
	return model.operation ?? "language";
}

function includesAll<T>(
	available: readonly T[] | undefined,
	required: readonly T[] | undefined,
): boolean {
	return (
		available === undefined ||
		required === undefined ||
		required.length === 0 ||
		required.every((value) => available.includes(value))
	);
}

interface OperationModelDescriptor {
	id: string;
	operation?: ModelOperation;
	operationModes?: readonly ModelOperationMode[];
	modalities?: ModelModalities;
	capabilities?: readonly string[];
	metadata?: GatewayModelDefinition["metadata"];
}

function capabilityRoutesMatchModel(
	capability: GatewayModelOperationCapability,
	model: OperationModelDescriptor,
): boolean {
	const family = model.metadata?.family;
	const routeContext = {
		modelId: model.id,
		family: typeof family === "string" ? family : undefined,
		capabilities: model.capabilities,
		operation: resolveModelOperation(model),
		modalities: model.modalities,
	};
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

function capabilityMatchesModel(
	capability: GatewayModelOperationCapability,
	model: OperationModelDescriptor,
): boolean {
	const operation = resolveModelOperation(model);
	const outputModalities =
		operation === "image-generation" ||
		operation === "speech-generation" ||
		operation === "video-generation"
			? model.modalities?.output.filter((modality) => modality !== "text")
			: model.modalities?.output;
	if (!capabilityRoutesMatchModel(capability, model)) {
		return false;
	}
	return (
		includesAll(capability.modes, model.operationModes) &&
		includesAll(capability.inputModalities, model.modalities?.input) &&
		includesAll(capability.outputModalities, outputModalities)
	);
}

function operationCapabilitiesSupportModel(
	capabilities: readonly GatewayModelOperationCapability[] | undefined,
	model: OperationModelDescriptor,
): boolean {
	const operation = resolveModelOperation(model);
	const needsExplicitCapability =
		operation !== "language" ||
		model.modalities?.output.some((modality) => modality !== "text") === true;
	if (!needsExplicitCapability) return true;

	return (
		capabilities?.some(
			(capability) =>
				capability.operation === operation &&
				capabilityMatchesModel(capability, model),
		) ?? false
	);
}

/**
 * 将外部目录事实约束到所选内置传输已实现的模态。
 * 目录可能声明 SDK 不会请求或解码的额外端点
 * 特性（例如图片模型上的 PDF 输出）。存储的模型
 * 必须描述可执行的子集。
 */
export function normalizeBuiltinModelOperationModalities(input: {
	providerId: string;
	modelId: string;
	operation?: ModelOperation;
	operationModes?: readonly ModelOperationMode[];
	modalities?: ModelModalities;
	family?: string;
	capabilities?: readonly string[];
}): ModelModalities | undefined {
	if (!input.modalities) return undefined;
	// 语音分类必须看到完整的 provider 声明形状。把
	// 多模态会话裁剪为 audio -> text 会将其误判为受支持的 STT。
	if (input.operation === "transcription" || input.operation === "realtime")
		return input.modalities;
	const model: OperationModelDescriptor = {
		id: input.modelId,
		operation: input.operation,
		operationModes: input.operationModes,
		modalities: input.modalities,
		capabilities: input.capabilities,
		metadata: input.family ? { family: input.family } : undefined,
	};
	const capability = BUILTIN_MODEL_OPERATION_CAPABILITIES[
		input.providerId
	]?.find(
		(candidate) =>
			candidate.operation === resolveModelOperation(model) &&
			capabilityRoutesMatchModel(candidate, model),
	);
	if (!capability) return input.modalities;

	const intersect = <T>(
		declared: readonly T[],
		supported: readonly T[] | undefined,
	): T[] =>
		supported === undefined
			? [...declared]
			: declared.filter((value) => supported.includes(value));
	const inputModalities = intersect(
		input.modalities.input,
		capability.inputModalities,
	);
	const outputModalities = intersect(
		input.modalities.output,
		capability.outputModalities,
	);

	// 保留无法匹配的声明，使 fail-closed 支持检查
	// 拒绝它，而不是把空交集变成表面上的支持。
	if (inputModalities.length === 0 || outputModalities.length === 0) {
		return input.modalities;
	}
	return { input: inputModalities, output: outputModalities };
}

/** 直接从 provider manifest 解析模型操作。 */
export function providerManifestSupportsModelOperation(
	manifest: Pick<
		GatewayProviderManifest,
		"modelOperationCapabilities" | "models"
	>,
	model: Pick<
		GatewayModelDefinition,
		| "id"
		| "operation"
		| "operationModes"
		| "modalities"
		| "capabilities"
		| "metadata"
	>,
): boolean {
	return operationCapabilitiesSupportModel(
		manifest.modelOperationCapabilities,
		model,
	);
}

/** Catalog-time form of the same fail-closed transport capability check. */
export function builtinProviderSupportsModelOperation(input: {
	providerId: string;
	modelId: string;
	operation?: ModelOperation;
	operationModes?: readonly ModelOperationMode[];
	modalities?: ModelModalities;
	family?: string;
	capabilities?: readonly string[];
}): boolean {
	const capabilities = BUILTIN_MODEL_OPERATION_CAPABILITIES[input.providerId];
	return operationCapabilitiesSupportModel(capabilities, {
		id: input.modelId,
		operation: input.operation,
		operationModes: input.operationModes,
		modalities: input.modalities,
		capabilities: input.capabilities,
		metadata: input.family ? { family: input.family } : undefined,
	});
}
