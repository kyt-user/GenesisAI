/**
 * 模型 Schema 定义
 *
 * 从 @cline/shared（规范源）重新导出模型信息类型，
 * 并定义 @cline/llms 本地的提供商级 schema。
 */

import { z } from "zod";

// ModelInfo 及其依赖的规范源在 @cline/shared
export {
	ApiFormat,
	ApiFormatSchema,
	type ModelCapability,
	ModelCapabilitySchema,
	type ModelInfo,
	ModelInfoSchema,
	type ModelMetadata,
	ModelMetadataSchema,
	type ModelModalities,
	ModelModalitiesSchema,
	type ModelModality,
	ModelModalitySchema,
	type ModelOperation,
	type ModelOperationMode,
	ModelOperationModeSchema,
	ModelOperationSchema,
	type ModelPricing,
	ModelPricingSchema,
	type ModelStatus,
	ModelStatusSchema,
	type ThinkingConfig,
	ThinkingConfigSchema,
} from "@cline/shared";

// 重新导入用于本地 schema
import { ModelInfoSchema, ProviderCapabilitySchema } from "@cline/shared";

export const ModelEntrySchema = z.object({
	id: z.string(),
	info: ModelInfoSchema,
});

export type ModelEntry = z.infer<typeof ModelEntrySchema>;
export type ProviderCapability = z.infer<typeof ProviderCapabilitySchema>;

export const ProviderProtocolSchema = z.enum([
	"anthropic",
	"gemini",
	"openai-chat",
	"openai-responses",
	"openai-r1",
	"ai-sdk",
]);

const ProviderClientSchema = z.enum([
	"anthropic",
	"ai-sdk",
	"ai-sdk-community",
	"openai",
	"openai-compatible",
	"openai-r1",
	"gemini",
	"bedrock",
	"custom",
	"fetch",
	"vertex",
]);

/**
 * ProviderSource 指示提供商如何被添加到系统，
 * 这对于确定信任级别以及是否在使用前提示用户确认很有用。
 * 例如，source 为 "system" 的提供商是内置的，可以信任，
 * 而 source 为 "file" 的提供商是用户使用本地 JSON 文件添加的，
 * source 为 "discovery" 的提供商是通过网络发现找到的。
 */
const ProviderSourceSchema = z.enum(["system", "file", "discovery"]);

export type ProviderClient = z.infer<typeof ProviderClientSchema>;
export type ProviderProtocol = z.infer<typeof ProviderProtocolSchema>;
export type ProviderSource = z.infer<typeof ProviderSourceSchema>;

export const ProviderInfoSchema = z.object({
	id: z.string(),
	name: z.string(),
	description: z.string().optional(),
	protocol: ProviderProtocolSchema.optional(),
	baseUrl: z.string().optional(),
	modelsSourceUrl: z.string().optional(),
	/** 提供商文档（设置、安装、API 参考）供宿主 UI 使用。 */
	docsUrl: z.string().optional(),
	defaultModelId: z.string(),
	capabilities: z.array(ProviderCapabilitySchema).optional(),
	env: z.array(z.string()).optional(),
	client: ProviderClientSchema,
	source: ProviderSourceSchema.default("system"),
	metadata: z.record(z.string(), z.unknown()).optional(),
});

export type ProviderInfo = z.infer<typeof ProviderInfoSchema>;

export const ModelCollectionSchema = z.object({
	provider: ProviderInfoSchema,
	models: z.record(z.string(), ModelInfoSchema),
});

export type ModelCollection = z.infer<typeof ModelCollectionSchema>;
