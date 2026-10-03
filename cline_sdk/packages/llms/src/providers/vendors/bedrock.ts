import { createAmazonBedrock } from "@ai-sdk/amazon-bedrock";
import { fromNodeProviderChain } from "@aws-sdk/credential-providers";
import type { GatewayResolvedProviderConfig } from "@cline/shared";
import { getGeneratedModelsForProvider } from "../../catalog/catalog.generated-access";
import type { ProviderFactoryResult } from "./types";

type BedrockCredentials = {
	accessKeyId: string;
	secretAccessKey: string;
	sessionToken?: string;
};

type BedrockCredentialProvider = () => PromiseLike<BedrockCredentials>;

type BedrockAuthentication = "iam" | "api-key" | "apikey" | "profile";

// 文档：https://ai-sdk.dev/providers/ai-sdk-providers/amazon-bedrock
const NON_BEDROCK_API_KEY_ENV = new Set([
	"AWS_ACCESS_KEY_ID",
	"AWS_SECRET_ACCESS_KEY",
	"AWS_SESSION_TOKEN",
	"AWS_REGION",
	"AWS_DEFAULT_REGION",
	"AWS_PROFILE",
]);

// Bedrock 推理配置（inference-profile）模型 id 解析。
//
// AWS Bedrock 对较新的基础模型不提供按需吞吐（on-demand throughput）：
// 它们必须通过推理配置调用——要么是带地理前缀的系统配置 id
// （"us." / "eu." / "apac." / "jp." / "au." / "global."），要么是
// 预置配置 ARN。直接使用裸基础模型 id 调用会失败并报：
// "Invocation of model ID ... with on-demand throughput isn't supported.
// Retry your request with the ID or ARN of an inference profile that contains
// this model."
// https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html
//
// 这镜像了 legacy 扩展的跨区域推理支持
// （legacy-extension 分支，apps/vscode/src/core/api/providers/bedrock.ts）：
// 用户的 `useCrossRegionInference` / `useGlobalInference` 设置会被遵循，
// 且已知无按需吞吐的模型的裸 id 还会被自动加前缀，
// 使它们无需开关即可工作。

const BEDROCK_GEO_PROFILE_PREFIX_PATTERN =
	/^(?:us|us-gov|eu|apac|jp|au|in|ca|sa|global)\./;

// 有文档记录的回退启发式例外（见 packages/llms/AGENTS.md）：
// 一份受维护、刻意收窄的 id 模式列表，列出 Bedrock 上
// 无按需吞吐的基础模型家族。匹配只让该模型在未开启跨区域设置时
// 有资格参与配置路由；实际前缀始终取自目录确认存在的配置变体，
// 因此匹配不会凭空制造 id 或破坏可用的配置。
// 不在列表中的模型仍可通过跨区域推理设置继续工作。
const BEDROCK_INFERENCE_PROFILE_REQUIRED_PATTERNS: readonly RegExp[] = [
	// 自 Claude 3.7 起每个 Anthropic 模型都只以 profile 形式发布，且全部
	// 使用层级在前（tier-first）的命名（claude-sonnet-4-6、claude-opus-5、
	// claude-haiku-4-5-...、claude-fable-5 等）。通过排除冻结的旧命名方案集合
	// （claude-3-5-sonnet-...、claude-v2、claude-instant-v1）来匹配
	// 层级在前的 id，使未来新层级无需更新列表即可工作。
	/^anthropic\.claude-(?![0-9]|v[0-9]|instant)/,
	// Claude 3.7 早于层级在前命名，但同样只以 profile 形式发布。
	/^anthropic\.claude-3-7-/,
	/^amazon\.nova-(?:2|micro|lite|pro|premier)/,
	// OpenAI GPT-5.x / GPT-6（Sol、Luna、Astra、Terra）只以 profile 形式发布；
	// gpt-oss 有按需吞吐，保留其裸 id
	// （cline/cline#14468）。
	/^openai\.gpt-(?!oss-)/,
	/^deepseek\./,
	/^meta\.llama3-[23]-/,
	/^meta\.llama4-/,
	/^mistral\.pixtral-large-/,
];

const JP_INFERENCE_PROFILE_REGIONS = new Set([
	"ap-northeast-1",
	"ap-northeast-3",
]);

const AU_INFERENCE_PROFILE_REGIONS = new Set([
	"ap-southeast-2",
	"ap-southeast-4",
]);

const IN_INFERENCE_PROFILE_REGIONS = new Set(["ap-south-1", "ap-south-2"]);

interface BedrockModelIdOptions {
	region?: string;
	useCrossRegionInference?: boolean;
	useGlobalInference?: boolean;
	/** 目录成员资格探测；可在测试中覆盖。 */
	hasCatalogModel?: (modelId: string) => boolean;
}

/**
 * 解析发送给 Bedrock 的模型 id，在需要时根据配置的 AWS 区域
 * 前置地理推理配置前缀。
 *
 * 已带 profile 前缀的 id 和 ARN（预置吞吐、导入/自定义模型、
 * 应用推理配置）始终原样透传。没有已知匹配配置的 id 保持原样，
 * 而不是凭空制造一个可能不存在的配置 id——AWS 的按需吞吐错误
 * 比 "provided model identifier is invalid" 更可操作。
 */
export function resolveBedrockModelId(
	modelId: string,
	options: BedrockModelIdOptions,
): string {
	if (
		modelId.startsWith("arn:") ||
		BEDROCK_GEO_PROFILE_PREFIX_PATTERN.test(modelId)
	) {
		return modelId;
	}

	const hasCatalogModel = options.hasCatalogModel ?? hasBedrockCatalogModel;
	const useCrossRegionInference = options.useCrossRegionInference === true;
	const requiresInferenceProfile =
		BEDROCK_INFERENCE_PROFILE_REQUIRED_PATTERNS.some((pattern) =>
			pattern.test(modelId),
		);
	if (!useCrossRegionInference && !requiresInferenceProfile) {
		return modelId;
	}

	if (
		useCrossRegionInference &&
		options.useGlobalInference === true &&
		hasCatalogModel(`global.${modelId}`)
	) {
		return `global.${modelId}`;
	}

	// 使用目录确认存在、且与该区域候选项匹配的第一个配置变体。
	// AWS 按模型和地理区域记录推理配置的可用性，因此未确认的地理前缀
	// 绝不假定为有效；没有确认的变体时保留原始 id。
	// 这也让自定义/预置模型 id 在跨区域路径上保持原样：
	// 它们不是目录模型，因此没有变体可匹配。
	for (const prefix of geoProfileCandidates(options.region)) {
		if (hasCatalogModel(`${prefix}${modelId}`)) {
			return `${prefix}${modelId}`;
		}
	}
	return modelId;
}

function geoProfileCandidates(region: string | undefined): string[] {
	if (!region) {
		return [];
	}
	if (region.startsWith("us-gov-")) {
		return ["us-gov."];
	}
	if (region.startsWith("us-")) {
		return ["us."];
	}
	if (region.startsWith("eu-")) {
		return ["eu."];
	}
	if (region.startsWith("ap-")) {
		// AWS 为最新的 Claude 模型提供专用的 jp./au. 配置——而且常常
		// 没有 apac. 配置——因此在区域允许时优先使用国家级配置。
		if (JP_INFERENCE_PROFILE_REGIONS.has(region)) {
			return ["jp.", "apac."];
		}
		if (AU_INFERENCE_PROFILE_REGIONS.has(region)) {
			return ["au.", "apac."];
		}
		if (IN_INFERENCE_PROFILE_REGIONS.has(region)) {
			return ["in.", "apac."];
		}
		return ["apac."];
	}
	// 其他区域不映射地理推理配置。
	return [];
}

function hasBedrockCatalogModel(modelId: string): boolean {
	return modelId in getGeneratedModelsForProvider("bedrock");
}

export async function createBedrockProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	const authentication = readAuthentication(config.options?.authentication);
	const usesApiKeyAuth =
		authentication === "api-key" || authentication === "apikey";
	const hasDirectCredentials =
		readOptionalString(config.options?.accessKeyId) !== undefined &&
		readOptionalString(config.options?.secretAccessKey) !== undefined;
	const hasProfile = readOptionalString(config.options?.profile) !== undefined;
	const usesExplicitSigV4Auth =
		!usesApiKeyAuth &&
		(authentication === "iam" || authentication === "profile" || hasProfile);
	const apiKey = usesExplicitSigV4Auth
		? undefined
		: await resolveBedrockApiKey(config, {
				includeEnvironment: usesApiKeyAuth || !hasDirectCredentials,
			});
	const credentialProvider = resolveCredentialProvider(config, {
		authentication,
		apiKey,
		hasDirectCredentials,
		hasProfile,
	});
	const usesSigV4 =
		authentication === "iam" ||
		authentication === "profile" ||
		hasDirectCredentials ||
		credentialProvider !== undefined;

	const provider = createAmazonBedrock({
		region: readOptionalString(config.options?.region),
		apiKey: usesApiKeyAuth
			? (apiKey ?? "")
			: (apiKey ?? (usesSigV4 ? "" : undefined)),
		accessKeyId: credentialProvider
			? undefined
			: readOptionalString(config.options?.accessKeyId),
		secretAccessKey: credentialProvider
			? undefined
			: readOptionalString(config.options?.secretAccessKey),
		sessionToken: credentialProvider
			? undefined
			: readOptionalString(config.options?.sessionToken),
		baseURL: config.baseUrl ?? readOptionalString(config.options?.endpoint),
		headers: config.headers,
		fetch: config.fetch,
		credentialProvider,
	});

	const modelIdOptions: BedrockModelIdOptions = {
		region:
			readOptionalString(config.options?.region) ??
			readOptionalString(process.env.AWS_REGION) ??
			readOptionalString(process.env.AWS_DEFAULT_REGION),
		useCrossRegionInference: config.options?.useCrossRegionInference === true,
		useGlobalInference: config.options?.useGlobalInference === true,
	};

	return {
		operations: {
			language: (modelId) =>
				provider(resolveBedrockModelId(modelId, modelIdOptions)),
			imageGeneration: (modelId) => provider.image(modelId),
		},
	};
}

function resolveCredentialProvider(
	config: GatewayResolvedProviderConfig,
	options: {
		authentication: BedrockAuthentication | undefined;
		apiKey: string | undefined;
		hasDirectCredentials: boolean;
		hasProfile: boolean;
	},
): BedrockCredentialProvider | undefined {
	const region = readOptionalString(config.options?.region);
	if (typeof config.options?.credentialProvider === "function") {
		return config.options.credentialProvider as BedrockCredentialProvider;
	}

	if (
		options.authentication === "api-key" ||
		options.authentication === "apikey" ||
		options.apiKey
	) {
		return undefined;
	}

	if (options.authentication === "profile" || options.hasProfile) {
		const profile = readOptionalString(config.options?.profile);
		return fromNodeProviderChain({
			ignoreCache: true,
			...(profile ? { profile } : {}),
			...(region ? { clientConfig: { region } } : {}),
		});
	}

	if (options.hasDirectCredentials) {
		return undefined;
	}

	return region
		? fromNodeProviderChain({ clientConfig: { region } })
		: fromNodeProviderChain();
}

async function resolveBedrockApiKey(
	config: GatewayResolvedProviderConfig,
	options: { includeEnvironment: boolean },
): Promise<string | undefined> {
	const explicitApiKey =
		readOptionalString(config.apiKey) ??
		readOptionalString(config.options?.apiKey) ??
		readOptionalString(config.options?.bedrockApiKey) ??
		readOptionalString(config.options?.awsBedrockApiKey);
	if (explicitApiKey) {
		return explicitApiKey;
	}

	const resolvedApiKey = readOptionalString(await config.apiKeyResolver?.());
	if (resolvedApiKey) {
		return resolvedApiKey;
	}

	if (!options.includeEnvironment) {
		return undefined;
	}

	for (const key of config.apiKeyEnv ?? []) {
		if (NON_BEDROCK_API_KEY_ENV.has(key)) {
			continue;
		}
		const value = readOptionalString(process.env[key]);
		if (value) {
			return value;
		}
	}

	return readOptionalString(process.env.AWS_BEARER_TOKEN_BEDROCK);
}

function readAuthentication(value: unknown): BedrockAuthentication | undefined {
	return value === "iam" ||
		value === "api-key" ||
		value === "apikey" ||
		value === "profile"
		? value
		: undefined;
}

function readOptionalString(value: unknown): string | undefined {
	if (typeof value !== "string") {
		return undefined;
	}
	const trimmed = value.trim();
	return trimmed.length > 0 ? trimmed : undefined;
}
