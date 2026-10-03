/**
 * Model Information Types
 *
 * Zod schemas and inferred types for model capabilities, pricing,
 * and metadata. These live in shared so that agent types can reference
 * ModelInfo without depending on @cline/llms.
 */

import { z } from "zod";
import { ModelReasoningOptionSchema } from "./reasoning-options";

export const ApiFormatSchema = z.enum(["default", "openai-responses", "r1"]);

export type ApiFormat = z.infer<typeof ApiFormatSchema>;

export const ApiFormat = {
	DEFAULT: "default" as const,
	OPENAI_RESPONSES: "openai-responses" as const,
	R1: "r1" as const,
} as const;

export const ModelCapabilitySchema = z.enum([
	"images",
	"video",
	"tools",
	"streaming",
	"prompt-cache",
	"reasoning",
	"reasoning-effort",
	"computer-use",
	"global-endpoint",
	"structured_output",
	"temperature",
	"files",
]);

export type ModelCapability = z.infer<typeof ModelCapabilitySchema>;

export const ModelStatusSchema = z.enum([
	"active",
	"preview",
	"deprecated",
	"legacy",
]);

export type ModelStatus = z.infer<typeof ModelStatusSchema>;

export const ModelPricingSchema = z.object({
	input: z.number().optional(),
	output: z.number().optional(),
	cacheWrite: z.number().optional(),
	cacheRead: z.number().optional(),
});

export type ModelPricing = z.infer<typeof ModelPricingSchema>;

export const ThinkingConfigSchema = z.object({
	maxBudget: z.number().optional(),
	outputPrice: z.number().optional(),
	thinkingLevel: z.enum(["low", "high"]).optional(),
});

export type ThinkingConfig = z.infer<typeof ThinkingConfigSchema>;

export const ModelMetadataSchema = z
	// Keep metadata open for catalog-defined facts while typing routing fields.
	.object({
		reasoningDefaultOn: z.boolean().optional(),
		/** Per-model wire protocol for gateways that serve multiple API formats. */
		apiProtocol: z
			.enum(["openai-chat", "openai-responses", "anthropic", "gemini"])
			.optional(),
	})
	.catchall(z.unknown());

export type ModelMetadata = z.infer<typeof ModelMetadataSchema>;

export const ModelModalitySchema = z.enum([
	"text",
	"image",
	"audio",
	"video",
	"pdf",
]);

export type ModelModality = z.infer<typeof ModelModalitySchema>;

export const ModelModalitiesSchema = z.object({
	input: z.array(ModelModalitySchema),
	output: z.array(ModelModalitySchema),
});

export type ModelModalities = z.infer<typeof ModelModalitiesSchema>;

export type ChatModelModalities = {
	readonly input?: readonly ModelModality[];
	readonly output?: readonly ModelModality[];
};

/**
 * Returns whether a model can participate in a text chat turn.
 *
 * Missing modality metadata is treated as chat-compatible for backwards
 * compatibility. When a catalog does provide modalities, the model must both
 * accept text and produce text; dedicated transcription and media-generation
 * endpoints therefore stay available in the shared catalog without leaking
 * into chat model pickers.
 */
export function supportsChatModalities(
	modalities: ChatModelModalities | undefined,
): boolean {
	return (
		(modalities?.input === undefined || modalities.input.includes("text")) &&
		(modalities?.output === undefined || modalities.output.includes("text"))
	);
}

/**
 * Provider operation used to execute a model request.
 *
 * Modalities describe the values a model accepts and produces; they do not
 * identify the provider endpoint. Keeping the operation explicit prevents an
 * image-output model from being routed to a generic chat or compatible-image
 * endpoint merely because its catalog advertises an image modality.
 */
export const ModelOperationSchema = z.enum([
	"language",
	"image-generation",
	"speech-generation",
	"video-generation",
	"transcription",
	"realtime",
]);

export type ModelOperation = z.infer<typeof ModelOperationSchema>;

/** 语音输入要求恰好是音频输入和文本输出。模型名称和
 * 操作标签不能将其扩展为多模态实时会话。 */
export function isTranscriptionModel(model: {
	modalities?: { input?: readonly string[]; output?: readonly string[] };
}): boolean {
	const input = model.modalities?.input;
	const output = model.modalities?.output;
	return (
		input?.length === 1 &&
		input[0] === "audio" &&
		output?.length === 1 &&
		output[0] === "text"
	);
}

export type ChatCompatibleModelDescriptor = {
	readonly operation?: ModelOperation;
	readonly modalities?: ChatModelModalities;
};

/**
 * Returns whether a model uses the language operation and supports a text chat
 * turn. Missing operation and modality metadata remain chat-compatible for
 * backwards compatibility, while any explicitly non-language operation is
 * excluded even when its modalities are absent.
 */
export function isChatCompatibleModel(
	model: ChatCompatibleModelDescriptor,
): boolean {
	return (
		(model.operation === undefined || model.operation === "language") &&
		supportsChatModalities(model.modalities)
	);
}

/**
 * Execution modes supported by a non-language model operation.
 *
 * The operation selects the provider transport; the mode describes how that
 * transport is consumed. Keeping this separate from generic model
 * capabilities prevents transcription-specific flags from spreading through
 * otherwise modality-agnostic clients.
 */
export const ModelOperationModeSchema = z.enum(["batch", "streaming"]);

export type ModelOperationMode = z.infer<typeof ModelOperationModeSchema>;

interface ImageOutputModelDescriptor {
	operation?: ModelOperation;
	modalities?: ModelModalities;
}

export function modelProducesImages(
	model: ImageOutputModelDescriptor,
): boolean {
	return (
		model.modalities?.input.includes("text") === true &&
		model.modalities.output.includes("image")
	);
}

export function usesImageGenerationOperation(
	model: ImageOutputModelDescriptor,
): boolean {
	return model.operation === "image-generation";
}

/**
 * 模型的能力元数据是否声明了 `capability`。
 *
 * 能力列表到达此检查的来源保真度差异很大：
 * 生成的目录是完整的，但宿主边界（VS Code 旧版
 * ModelInfo、用户编写的覆盖、动态 provider 列表）可能携带
 * 从少量布尔标志重建的部分列表。因此缺失或
 * 空列表不携带任何信号，每个检查通过 `assumeWhenUnspecified`
 * 声明自己的默认值，而不是将缺失视为否认。
 *
 * 未来模型模式（音频、视频、转录……）添加的能力门
 * 应通过此辅助函数而非直接读取
 * `model.capabilities`，使未指定列表的语义在整个代码库中
 * 保持一致。
 */
export function modelHasCapability(
	model: { capabilities?: readonly string[] },
	capability: string,
	options?: { assumeWhenUnspecified?: boolean },
): boolean {
	const capabilities = model.capabilities;
	if (capabilities === undefined || capabilities.length === 0) {
		return options?.assumeWhenUnspecified ?? false;
	}
	return capabilities.includes(capability);
}

/**
 * 模型是否可接收函数/工具定义。当能力列表缺失或为空时
 *（用户输入和动态发现的模型）默认放行；
 * 有内容的能力列表不含 `tools` 则是权威的。
 */
export function modelSupportsToolCalling(model: {
	capabilities?: readonly string[];
}): boolean {
	return modelHasCapability(model, "tools", { assumeWhenUnspecified: true });
}

/**
 * 模型是否可在请求中接收图像部分。与
 * `modelSupportsToolCalling` 基于同样的理由默认放行：
 * 完全不报告任何能力的宿主边界并未声明该模型为纯文本，
 * 而从支持视觉的模型中剥离图像会静默丢失用户内容。
 * 有内容的列表不含 `images` 则是权威的。
 */
export function modelSupportsImageInput(model: {
	capabilities?: readonly string[];
}): boolean {
	return modelHasCapability(model, "images", { assumeWhenUnspecified: true });
}

export const ModelInfoSchema = z.object({
	id: z.string(),
	name: z.string().optional(),
	description: z.string().optional(),
	maxTokens: z.number().optional(),
	contextWindow: z.number().optional(),
	maxInputTokens: z.number().optional(),
	capabilities: z.array(ModelCapabilitySchema).optional(),
	operation: ModelOperationSchema.optional(),
	operationModes: z.array(ModelOperationModeSchema).optional(),
	modalities: ModelModalitiesSchema.optional(),
	reasoningOptions: z.array(ModelReasoningOptionSchema).optional(),
	apiFormat: ApiFormatSchema.optional(),
	systemRole: z.enum(["system", "developer"]).optional(),
	temperature: z.number().optional(),
	pricing: ModelPricingSchema.optional(),
	thinkingConfig: ThinkingConfigSchema.optional(),
	status: ModelStatusSchema.optional(),
	deprecationNotice: z.string().optional(),
	replacedBy: z.string().optional(),
	releaseDate: z.string().optional(),
	deprecationDate: z.string().optional(),
	family: z.string().optional(),
	metadata: ModelMetadataSchema.optional(),
});

export type ModelInfo = z.infer<typeof ModelInfoSchema>;
