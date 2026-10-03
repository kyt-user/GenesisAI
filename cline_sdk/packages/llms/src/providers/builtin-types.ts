import type {
	GatewayModelOperationCapability,
	GatewayModelToolCapability,
	GatewayProviderMetadata,
	GatewayProviderSettings,
	ProviderCapability,
	ProviderConfigField,
} from "@cline/shared";
import type {
	ModelInfo,
	ProviderClient,
	ProviderProtocol,
} from "../catalog/types";

export type ProviderFamily =
	| "cline"
	| "openai"
	| "openai-compatible"
	| "anthropic"
	| "google"
	| "vertex"
	| "bedrock"
	| "mistral"
	| "claude-code"
	| "openai-codex"
	| "opencode"
	| "dify"
	| "ollama"
	| "sap-ai-core";

export type ProviderApiLine = "china" | "international";

export interface BuiltinSpec {
	id: string;
	name: string;
	description: string;
	family: ProviderFamily;
	protocol?: ProviderProtocol;
	client?: ProviderClient;
	modelToolCapabilities?: readonly GatewayModelToolCapability[];
	modelOperationCapabilities?: readonly GatewayModelOperationCapability[];
	capabilities?: ProviderCapability[];
	popular?: number;
	modelsProviderId?: string;
	defaultModelId?: string;
	modelsFactory?: () => Record<string, ModelInfo>;
	env?: readonly ("browser" | "node")[];
	apiKeyEnv?: readonly string[];
	modelsSourceUrl?: string;
	docsUrl?: string;
	defaults?: GatewayProviderSettings;
	/**
	 * 区域端点路由事实：每个 API 线路的基础 URL。当调用方
	 * 选择 `apiLine` 但没有显式 base URL 时使用。与
	 * `defaults.baseUrl` 匹配的线路也被包含，使映射详尽
	 * 且自文档化。
	 */
	apiLineBaseUrls?: Readonly<Partial<Record<ProviderApiLine, string>>>;
	configFields?: readonly ProviderConfigField[];
	metadata?: GatewayProviderMetadata;
}
