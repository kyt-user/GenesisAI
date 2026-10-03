/**
 * Provider 配置类型
 *
 * 所有 provider 的统一配置接口。
 * 以单一结构取代过去按 provider 各自为政的配置乱象。
 */

import type {
	BasicLogger,
	ExtensionContext,
	ReasoningEffort,
} from "@cline/shared";
import type { ModelInfo, ProviderClient } from "../catalog/types";
import {
	BUILT_IN_PROVIDER,
	BUILT_IN_PROVIDER_IDS,
	type BuiltInProviderId,
	isBuiltInProviderId,
	normalizeProviderId,
} from "./ids";

// 为方便起见重新导出
export {
	BUILT_IN_PROVIDER,
	BUILT_IN_PROVIDER_IDS,
	type BuiltInProviderId,
	isBuiltInProviderId,
	normalizeProviderId,
};

/**
 * 所有支持的 provider ID（内置 + 自定义）
 *
 * 自定义 provider ID 可通过 `registerHandler()` 或 `registerAsyncHandler()` 注册。
 * 接受任意字符串，以支持扩展 BaseHandler 的自定义处理器。
 */
export type ProviderId = BuiltInProviderId | (string & {});

/**
 * 基于底层 SDK/协议的 provider 类别
 */
export type ProviderCategory =
	| "anthropic" // Anthropic SDK
	| "openai" // OpenAI SDK（原生特性）
	| "openai-compat" // OpenAI 兼容 API
	| "openai-responses" // OpenAI-Responses API
	| "gemini" // Google GenAI SDK
	| "bedrock" // AWS Bedrock SDK
	| "custom"; // 自定义实现

// =============================================================================
// Provider 能力
// =============================================================================

/**
 * provider/model 可能支持的能力
 */
export type ProviderCapability =
	| "reasoning" // 扩展思考/推理
	| "prompt-cache" // 提示词缓存
	| "streaming" // 流式响应
	| "tools" // 工具/函数调用
	| "vision" // 图片输入
	| "computer-use" // 电脑操作工具
	| "oauth"; // OAuth 认证流程

// =============================================================================
// 配置组件
// =============================================================================

/**
 * 认证配置
 */
export interface AuthConfig {
	/** API key（最常见） */
	apiKey?: string;
	/** OAuth 访问令牌 */
	accessToken?: string;
	/** OAuth 刷新令牌 */
	refreshToken?: string;
	/** 账户 ID（用于基于账户的认证） */
	accountId?: string;
	/** OAuth 回调路径（例如用于 Qwen Code） */
	oauthPath?: string;
}

/**
 * 端点配置
 */
export interface EndpointConfig {
	/** API 的 base URL */
	baseUrl?: string;
	/** 要包含的自定义请求头 */
	headers?: Record<string, string>;
	/** 请求超时（毫秒） */
	timeoutMs?: number;
	/**
	 * AI 网关 provider 发出请求时使用的自定义 `fetch` 实现。
	 * 提供后，它会被转发到 `GatewayProviderSettings.fetch`
	 *（并作为 `GatewayConfig.fetch` 的顶层回退），使宿主能注入
	 * 代理、重试、追踪或测试替身等自定义 HTTP 行为。
	 */
	fetch?: typeof fetch;
}

/**
 * 模型配置
 */
export interface ModelConfig {
	/** 模型标识符 */
	modelId: string;
	/** 预取的模型信息（可选——未提供时使用默认值） */
	modelInfo?: ModelInfo;
	/** 该 provider 的已知模型及其信息 */
	knownModels?: Record<string, ModelInfo>;
}

/**
 * Token 上限配置
 */
export interface TokenConfig {
	/** 最大输入 token（覆盖模型默认值） */
	maxInputTokens?: number;
	/** 最大输出 token（覆盖模型默认值） */
	maxOutputTokens?: number;
	/** 采样温度（覆盖模型默认值） */
	temperature?: number;
}

/**
 * 推理/思考模型配置
 */
export interface ReasoningConfig {
	/** 推理投入级别 */
	reasoningEffort?: ReasoningEffort;
	/** 扩展思考预算（token） */
	thinkingBudgetTokens?: number;
	/** 在支持时用 provider/模型默认值启用思考 */
	thinking?: boolean;
}

/**
 * 区域配置（云 provider 共享）
 */
export interface RegionConfig {
	/** 云区域（AWS、GCP、Azure，或 provider 特有值如 Qwen 的 china/international） */
	region?: string;
	/** 区域路由的 API 线路（例如 Qwen 的 "china" | "international"） */
	apiLine?: "china" | "international";
	/** 使用跨区域推理（Bedrock） */
	useCrossRegionInference?: boolean;
	/** 使用全局推理（Bedrock） */
	useGlobalInference?: boolean;
}

/**
 * AWS 特有配置（用于 Bedrock）
 */
export interface AwsConfig {
	accessKey?: string;
	secretKey?: string;
	sessionToken?: string;
	authentication?: "iam" | "api-key" | "apikey" | "profile";
	profile?: string;
	usePromptCache?: boolean;
	endpoint?: string;
	customModelBaseId?: string;
}

/**
 * Google Cloud 配置（用于 Vertex AI）
 */
export interface GcpConfig {
	projectId?: string;
	region?: string;
}

/**
 * Azure 配置（用于 Azure OpenAI）
 */
export interface AzureConfig {
	apiVersion?: string;
	useIdentity?: boolean;
}

/**
 * SAP AI Core 配置
 */
export interface SapConfig {
	clientId?: string;
	clientSecret?: string;
	tokenUrl?: string;
	resourceGroup?: string;
	deploymentId?: string;
	useOrchestrationMode?: boolean;
	api?: "orchestration" | "foundation-models";
	defaultSettings?: Record<string, unknown>;
}

/**
 * OCA（Oracle Cloud AI）配置
 */
export interface OcaConfig {
	mode?: "internal" | "external";
	usePromptCache?: boolean;
}

/**
 * Codex CLI provider 选项
 */
export interface CodexConfig {
	defaultSettings?: Record<string, unknown>;
	modelSettings?: Record<string, unknown>;
}

/**
 * Claude Code provider 选项
 */
export interface ClaudeCodeConfig {
	[key: string]: unknown;
}

/**
 * OpenCode provider 选项
 */
export interface OpenCodeConfig {
	hostname?: string;
	port?: number;
	autoStartServer?: boolean;
	serverTimeout?: number;
	defaultSettings?: Record<string, unknown>;
	modelSettings?: Record<string, unknown>;
}

/**
 * 云 provider 配置（分组）
 */
export interface CloudConfig {
	/** AWS/Bedrock 选项 */
	aws?: AwsConfig;
	/** Google Cloud/Vertex 选项 */
	gcp?: GcpConfig;
	/** Azure 选项 */
	azure?: AzureConfig;
	/** SAP AI Core 选项 */
	sap?: SapConfig;
	/** OCA 选项 */
	oca?: OcaConfig;
}

/**
 * 不适合其他类别的 provider 特有选项
 */
export interface ProviderOptions {
	/** OpenRouter provider 排序偏好 */
	openRouterProviderSorting?: string;
	/** 运行时模型目录刷新配置 */
	modelCatalog?: ModelCatalogConfig;
}

/**
 * 运行时模型目录刷新选项
 */
export interface ModelCatalogConfig {
	/** 在处理器初始化时获取最新目录 */
	loadLatestOnInit?: boolean;
	/**
	 * 包含仅对 Cline Cloud 会话有效的模型。
	 * 默认为 false；含云目录与本地目录使用独立缓存。
	 */
	includeClineCloudModels?: boolean;
	/** 认证可用时获取 provider 私有模型 */
	loadPrivateOnAuth?: boolean;
	/** 目录端点 URL */
	url?: string;
	/** 实时目录的缓存 TTL（毫秒） */
	cacheTtlMs?: number;
	/** 实时目录刷新失败时抛错 */
	failOnError?: boolean;
}

// =============================================================================
// 主配置接口
// =============================================================================

/**
 * 统一的 provider 配置接口
 *
 * 这是客户端提供的唯一配置接口。
 * 所有 provider 特有选项分组到逻辑子接口中。
 */
export interface ProviderConfig
	extends AuthConfig,
		EndpointConfig,
		ModelConfig,
		TokenConfig,
		ReasoningConfig,
		RegionConfig,
		CloudConfig,
		ProviderOptions {
	/** 客户端类型——决定使用哪个处理器——默认为 OpenAI 兼容 */
	clientType?: ProviderClient;

	/** Provider ID */
	providerId: ProviderId;

	/**
	 * 用于处理器路由的可选内置 provider 家族。
	 *
	 * 这让客户端可以暴露自定义 provider ID 和模型目录，
	 * 同时复用某个内置 provider 实现的运行时行为。
	 */
	routingProviderId?: ProviderId;

	/** 该 provider/model 支持的能力 */
	capabilities?: ProviderCapability[];

	/** 用于遥测的任务/会话 ID */
	taskId?: string;

	/** 用于取消请求的 AbortSignal */
	abortSignal?: AbortSignal;

	/** 用于 provider 级诊断的可选运行时日志器 */
	logger?: BasicLogger;

	/**
	 * 环境运行时上下文：用户身份、客户端表面、工作区信息、
	 * 日志器和遥测服务。可用时优先从这里读取日志器和遥测；
	 * 顶层 logger 字段为兼容性保留。
	 */
	extensionContext?: ExtensionContext;

	/** Codex CLI 特有选项 */
	codex?: CodexConfig;

	/** Claude Code 特有选项 */
	claudeCode?: ClaudeCodeConfig;

	/** OpenCode 特有选项 */
	opencode?: OpenCodeConfig;
}

/**
 * 常见用例的简化配置
 */
export interface SimpleProviderConfig {
	providerId: ProviderId;
	clientType: ProviderClient;
	apiKey: string;
	modelId: string;
	baseUrl?: string;
}

/**
 * 从简化配置创建完整的 ProviderConfig
 */
export function createConfig(simple: SimpleProviderConfig): ProviderConfig {
	return {
		providerId: simple.providerId,
		clientType: simple.clientType,
		apiKey: simple.apiKey,
		modelId: simple.modelId,
		baseUrl: simple.baseUrl,
	};
}

// =============================================================================
// 辅助函数
// =============================================================================

/**
 * 检查 provider 配置是否具有特定能力
 */
export function hasCapability(
	config: ProviderConfig,
	capability: ProviderCapability,
): boolean {
	return config.capabilities?.includes(capability) ?? false;
}

/**
 * 检查 provider 是否支持推理/思考
 */
export function supportsReasoning(config: ProviderConfig): boolean {
	return hasCapability(config, "reasoning");
}

/**
 * 检查 provider 是否支持提示词缓存
 */
export function supportsPromptCache(config: ProviderConfig): boolean {
	return hasCapability(config, "prompt-cache");
}

/**
 * 解析用于处理器选择和内置行为的 provider ID。
 */
export function resolveRoutingProviderId(
	config: Pick<ProviderConfig, "providerId" | "routingProviderId">,
): string {
	return normalizeProviderId(config.routingProviderId ?? config.providerId);
}

// =============================================================================
// 已废弃类型（向后兼容）
// =============================================================================

/**
 * @deprecated 直接使用 ProviderConfig——所有字段现已统一
 */
export type ProviderSpecificConfig = Pick<
	ProviderConfig,
	| "aws"
	| "gcp"
	| "azure"
	| "sap"
	| "oca"
	| "maxInputTokens"
	| "apiLine"
	| "oauthPath"
	| "openRouterProviderSorting"
>;

/**
 * @deprecated 直接使用 ProviderConfig
 */
export type ProviderDefaultsConfig = Pick<
	ProviderConfig,
	"baseUrl" | "modelId" | "knownModels" | "headers" | "capabilities"
>;
