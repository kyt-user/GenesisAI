/**
 * ═══════════════════════════════════════════════════════════════════════════
 * ⚠️  严重警告 ⚠️
 * ═══════════════════════════════════════════════════════════════════════════
 *
 * 只要更新此 schema，Api 服务器就必须重新部署！
 *
 * 此 schema 被扩展和 API 服务器共同用于验证。
 * 此处的任何更改都需要协调部署，以避免验证错误。
 *
 * ═══════════════════════════════════════════════════════════════════════════
 */

import { z } from "zod";

// OpenAI 兼容模型 schema，带每模型设置
export const OpenAiCompatibleModelSchema = z.object({
	id: z.string(), // 模型 ID 为必填项
	temperature: z.number().optional(),
	maxTokens: z.number().optional(),
	contextWindow: z.number().optional(),
	inputPrice: z.number().optional(),
	outputPrice: z.number().optional(),
	supportsImages: z.boolean().optional(),
});

// OpenAiCompatible 专属设置
export const OpenAiCompatibleSchema = z.object({
	// 允许的模型列表及其设置
	models: z.array(OpenAiCompatibleModelSchema).optional(),
	// OpenAiCompatible 专属设置：
	openAiBaseUrl: z.string().optional(),
	openAiHeaders: z.record(z.string(), z.string()).optional(),
	azureApiVersion: z.string().optional(),
	azureIdentity: z.boolean().optional(),
});

// AWS Bedrock 模型 schema，带每模型设置
export const AwsBedrockModelSchema = z.object({
	id: z.string(), // 模型 ID 为必填项
	thinkingBudgetTokens: z.number().optional(),
});

// AWS Bedrock 自定义模型 schema（与常规模型分开）
export const AwsBedrockCustomModelSchema = z.object({
	name: z.string(), // 模型名称为必填项
	baseModelId: z.string(), // 基础模型 ID 为必填项
	thinkingBudgetTokens: z.number().optional(),
});

// AWS Bedrock 专属设置
export const AwsBedrockSettingsSchema = z.object({
	// 允许的模型列表及其设置
	models: z.array(AwsBedrockModelSchema).optional(),
	// 自定义模型
	customModels: z.array(AwsBedrockCustomModelSchema).optional(),
	// AWS Bedrock 专属设置：
	awsRegion: z.string().optional(),
	awsUseCrossRegionInference: z.boolean().optional(),
	awsUseGlobalInference: z.boolean().optional(),
	awsBedrockUsePromptCache: z.boolean().optional(),
	awsBedrockEndpoint: z.string().optional(),
});

// Cline Provider 模型 schema，带每模型设置
export const ClineModelSchema = z.object({
	id: z.string(), // 模型 ID 为必填项
});

// Cline Provider 专属设置
export const ClineSettingsSchema = z.object({
	// 允许的模型列表及其设置
	models: z.array(ClineModelSchema).optional(),
});

// Vertex Provider 模型 schema，带每模型设置
export const VertexModelSchema = z.object({
	id: z.string(), // 模型 ID 为必填项
	thinkingBudgetTokens: z.number().optional(),
});

// GCP Vertex Provider 专属设置
export const VertexSettingsSchema = z.object({
	// 允许的模型列表及其设置
	models: z.array(VertexModelSchema).optional(),
	vertexProjectId: z.string().optional(),
	vertexRegion: z.string().optional(),
});

export const LiteLLMModelSchema = z.object({
	id: z.string(),
	thinkingBudgetTokens: z.number().optional(),
	promptCachingEnabled: z.boolean().optional(),
});

export const LiteLLMSchema = z.object({
	models: z.array(LiteLLMModelSchema).optional(),
	baseUrl: z.string().optional(),
});

export const AnthropicModelSchema = z.object({
	id: z.string(),
	thinkingBudgetTokens: z.number().optional(),
});

export const AnthropicSchema = z.object({
	models: z.array(AnthropicModelSchema).optional(),
	baseUrl: z.string().optional(),
});

// Provider 设置 schema
// 每个 provider 变为一个可选字段
const ProviderSettingsSchema = z.object({
	OpenAiCompatible: OpenAiCompatibleSchema.optional(),
	AwsBedrock: AwsBedrockSettingsSchema.optional(),
	Cline: ClineSettingsSchema.optional(),
	Vertex: VertexSettingsSchema.optional(),
	LiteLLM: LiteLLMSchema.optional(),
	Anthropic: AnthropicSchema.optional(),
});

export const AllowedMCPServerSchema = z.object({
	// MCP 的 ID 是其 github 仓库的 URL。
	id: z.string(),
});

export const RemoteMCPServerSchema = z.object({
	// MCP 服务器的名称
	name: z.string(),
	// MCP 服务器的 URL
	url: z.string(),
	// 当此项为 true 时，用户无法禁用此 MCP 服务器
	alwaysEnabled: z.boolean().optional(),
	// 允许自定义认证的请求头
	headers: z.record(z.string(), z.string()).optional(),
});

// 全局 cline 规则或工作流文件的设置。
export const GlobalInstructionsFileSchema = z.object({
	// 当此项启用时，用户无法关闭此规则或工作流。
	alwaysEnabled: z.boolean(),
	// 规则或工作流文件的名称。
	name: z.string(),
	// 规则或工作流文件的内容
	contents: z.string(),
});

export const S3AccessKeySettingsSchema = z.object({
	bucket: z.string(),
	accessKeyId: z.string(),
	secretAccessKey: z.string(),
	region: z.string().optional(),
	endpoint: z.string().optional(),
	accountId: z.string().optional(),
});

export const PromptUploadingSchema = z.object({
	enabled: z.boolean().optional(),
	type: z
		.union([
			z.literal("s3_access_keys"),
			z.literal("r2_access_keys"),
			z.literal("azure_access_keys"),
		])
		.optional(),
	s3AccessSettings: S3AccessKeySettingsSchema.optional(),
	r2AccessSettings: S3AccessKeySettingsSchema.optional(),
	azureAccessSettings: S3AccessKeySettingsSchema.optional(),
});

export const EnterpriseTelemetrySchema = z.object({
	promptUploading: PromptUploadingSchema.optional(),
});

export const RemoteConfigSchema = z.object({
	// 远程配置设置的版本，例如 v1
	// 此字段仅供内部使用，不会在 UI 中对管理员可见。
	version: z.string(),

	// Provider 专属设置
	providerSettings: ProviderSettingsSchema.optional(),

	// 不特定于任何 provider 的通用设置
	telemetryEnabled: z.boolean().optional(),
	kanbanEnabled: z.boolean().optional(),

	// MCP 设置
	// 旧版字段名。若为 false，则本地配置的 MCP 服务器被阻止。
	mcpMarketplaceEnabled: z.boolean().optional(),

	// 若配置此项，用户只能访问这些加入白名单的本地 MCP 服务器。
	allowedMCPServers: z.array(AllowedMCPServerSchema).optional(),

	// 预配置的远程 MCP 服务器列表。
	remoteMCPServers: z.array(RemoteMCPServerSchema).optional(),
	// 若为 true，用户无法使用或配置未通过远程配置的 MCP 服务器。
	blockPersonalRemoteMCPServers: z.boolean().optional(),

	// 是否允许用户启用 YOLO 模式。注意这与扩展设置
	// yoloModeEnabled 不同，因为我们不想强制为用户启用 YOLO。
	yoloModeAllowed: z.boolean().optional(),

	// OpenTelemetry 配置
	openTelemetryEnabled: z.boolean().optional(),
	openTelemetryMetricsExporter: z.string().optional(),
	openTelemetryLogsExporter: z.string().optional(),
	openTelemetryOtlpProtocol: z.string().optional(),
	openTelemetryOtlpEndpoint: z.string().optional(),
	openTelemetryOtlpHeaders: z.record(z.string(), z.string()).optional(),
	openTelemetryOtlpMetricsProtocol: z.string().optional(),
	openTelemetryOtlpMetricsEndpoint: z.string().optional(),
	openTelemetryOtlpMetricsHeaders: z.record(z.string(), z.string()).optional(),
	openTelemetryOtlpLogsProtocol: z.string().optional(),
	openTelemetryOtlpLogsEndpoint: z.string().optional(),
	openTelemetryOtlpLogsHeaders: z.record(z.string(), z.string()).optional(),
	openTelemetryMetricExportInterval: z.number().optional(),
	openTelemetryOtlpInsecure: z.boolean().optional(),
	openTelemetryLogBatchSize: z.number().optional(),
	openTelemetryLogBatchTimeout: z.number().optional(),
	openTelemetryLogMaxQueueSize: z.number().optional(),

	enterpriseTelemetry: EnterpriseTelemetrySchema.optional(),

	// 规则与工作流
	globalRules: z.array(GlobalInstructionsFileSchema).optional(),
	globalWorkflows: z.array(GlobalInstructionsFileSchema).optional(),
});

export const APIKeySchema = z.record(z.string(), z.string());

// 从 schema 进行类型推断
export type RemoteConfig = z.infer<typeof RemoteConfigSchema>;
export type MCPServer = z.infer<typeof AllowedMCPServerSchema>;
export type RemoteMCPServer = z.infer<typeof RemoteMCPServerSchema>;
export type GlobalInstructionsFile = z.infer<
	typeof GlobalInstructionsFileSchema
>;

export type ProviderSettings = z.infer<typeof ProviderSettingsSchema>;

export type OpenAiCompatible = z.infer<typeof OpenAiCompatibleSchema>;
export type OpenAiCompatibleModel = z.infer<typeof OpenAiCompatibleModelSchema>;

export type AwsBedrockSettings = z.infer<typeof AwsBedrockSettingsSchema>;
export type AwsBedrockModel = z.infer<typeof AwsBedrockModelSchema>;
export type AwsBedrockCustomModel = z.infer<typeof AwsBedrockCustomModelSchema>;

export type VertexSettings = z.infer<typeof VertexSettingsSchema>;
export type VertexModel = z.infer<typeof VertexModelSchema>;

export type LiteLLMSettings = z.infer<typeof LiteLLMSchema>;
export type LiteLLMModel = z.infer<typeof LiteLLMModelSchema>;

export type AnthropicSettings = z.infer<typeof AnthropicSchema>;
export type AnthropicModel = z.infer<typeof AnthropicModelSchema>;

export type APIKeySettings = z.infer<typeof APIKeySchema>;

export type EnterpriseTelemetry = z.infer<typeof EnterpriseTelemetrySchema>;
export type PromptUploading = z.infer<typeof PromptUploadingSchema>;
export type S3AccessKeySettings = z.infer<typeof S3AccessKeySettingsSchema>;
