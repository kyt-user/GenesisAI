/**
 * 关联目录生成各阶段与运行时 provider 选择中使用的
 * provider 标识符：
 *
 * - `modelsDevKey` 是 provider 在 models.dev API 载荷中的键。
 * - `generatedProviderId` 是 Cline 为该载荷生成的 provider spec 和
 *   模型目录所使用的规范 ID。生成过程映射
 *   `modelsDevKey -> generatedProviderId`，使上游名称不会泄漏到
 *   Cline 的公开 provider ID 中。
 * - `runtimeProviderId` 是已配置 provider 实现的 ID，该实现
 *   应从生成的目录中读取模型。
 *
 * 生成 ID 与运行时 ID 分离，因为多个运行时传输或
 * 认证方式可以共享一个目录。例如，`openai-native`、
 * `openai-codex` 和 `openai-codex-cli` 是不同的运行时 provider，但都
 * 解析到由 models.dev 的 `openai` 条目生成的 `openai-native` 目录。
 * `resolveProviderModelCatalogKeys` 执行该运行时到目录的
 * 查找。省略生成的或运行时 ID 意味着该行只参与
 * 另一个适用的查找。
 */
const PROVIDER_IDS_MAP: ReadonlyArray<{
	modelsDevKey: string;
	generatedProviderId?: string;
	runtimeProviderId?: string;
}> = [
	{
		modelsDevKey: "openai",
		generatedProviderId: "openai-native",
		runtimeProviderId: "openai-native",
	},
	{
		modelsDevKey: "openai",
		generatedProviderId: "openai-native",
		runtimeProviderId: "openai-codex-cli",
	},
	{
		modelsDevKey: "openai",
		generatedProviderId: "openai-native",
		runtimeProviderId: "openai-codex",
	},
	{ modelsDevKey: "anthropic", generatedProviderId: "anthropic" },
	{
		modelsDevKey: "anthropic",
		generatedProviderId: "anthropic",
		runtimeProviderId: "claude-code",
	},
	{ modelsDevKey: "google", generatedProviderId: "gemini" },
	{
		modelsDevKey: "deepseek",
		generatedProviderId: "deepseek",
		runtimeProviderId: "deepseek",
	},
	{ modelsDevKey: "xai", generatedProviderId: "xai" },
	{
		modelsDevKey: "togetherai",
		generatedProviderId: "together",
		runtimeProviderId: "together",
	},
	{
		modelsDevKey: "sap-ai-core",
		generatedProviderId: "sapaicore",
		runtimeProviderId: "sapaicore",
	},
	{ modelsDevKey: "ollama", runtimeProviderId: "ollama-cloud" },
	{ modelsDevKey: "ollama-cloud", generatedProviderId: "ollama" },
	{
		modelsDevKey: "fireworks-ai",
		generatedProviderId: "fireworks",
		runtimeProviderId: "fireworks",
	},
	{
		modelsDevKey: "groq",
		generatedProviderId: "groq",
		runtimeProviderId: "groq",
	},
	{
		modelsDevKey: "poolside",
		generatedProviderId: "poolside",
		runtimeProviderId: "poolside",
	},
	{
		modelsDevKey: "cerebras",
		generatedProviderId: "cerebras",
		runtimeProviderId: "cerebras",
	},
	{
		modelsDevKey: "sambanova",
		generatedProviderId: "sambanova",
		runtimeProviderId: "sambanova",
	},
	{
		modelsDevKey: "nebius",
		generatedProviderId: "nebius",
		runtimeProviderId: "nebius",
	},
	{
		modelsDevKey: "crusoe",
		generatedProviderId: "crusoe",
		runtimeProviderId: "crusoe",
	},
	{
		modelsDevKey: "huggingface",
		generatedProviderId: "huggingface",
		runtimeProviderId: "huggingface",
	},
	{
		modelsDevKey: "openrouter",
		generatedProviderId: "openrouter",
	},
	{
		modelsDevKey: "vercel",
		generatedProviderId: "vercel-ai-gateway",
		runtimeProviderId: "dify",
	},
	{
		modelsDevKey: "vercel",
		generatedProviderId: "vercel-ai-gateway",
	},
	{
		modelsDevKey: "openrouter",
		generatedProviderId: "openrouter",
		runtimeProviderId: "cline",
	},
	{
		modelsDevKey: "aiand",
		generatedProviderId: "aiand",
		runtimeProviderId: "aiand",
	},
	{
		modelsDevKey: "aihubmix",
		generatedProviderId: "aihubmix",
		runtimeProviderId: "aihubmix",
	},
	{ modelsDevKey: "hicap", runtimeProviderId: "hicap" },
	{ modelsDevKey: "nous-research", runtimeProviderId: "nousResearch" },
	{ modelsDevKey: "huawei-cloud-maas", runtimeProviderId: "huawei-cloud-maas" },
	{
		modelsDevKey: "baseten",
		generatedProviderId: "baseten",
		runtimeProviderId: "baseten",
	},
	{ modelsDevKey: "zai-coding-plan", generatedProviderId: "zai-coding-plan" },
	{ modelsDevKey: "google-vertex", generatedProviderId: "vertex" },
	{ modelsDevKey: "lmstudio", generatedProviderId: "lmstudio" },
	{ modelsDevKey: "zai", generatedProviderId: "zai" },
	{ modelsDevKey: "requesty", generatedProviderId: "requesty" },
	{ modelsDevKey: "amazon-bedrock", generatedProviderId: "bedrock" },
	{ modelsDevKey: "mistral", generatedProviderId: "mistral" },
	{ modelsDevKey: "moonshotai", generatedProviderId: "moonshot" },
	{ modelsDevKey: "minimax", generatedProviderId: "minimax" },
	{ modelsDevKey: "opencode", generatedProviderId: "opencode" },
	{ modelsDevKey: "wandb", generatedProviderId: "wandb" },
	{ modelsDevKey: "kilo", generatedProviderId: "kilo" },
	{ modelsDevKey: "xiaomi", generatedProviderId: "xiaomi" },
	{
		modelsDevKey: "tencent-tokenhub",
		generatedProviderId: "tencent-tokenhub",
	},
	{ modelsDevKey: "v0", generatedProviderId: "v0" },
];

function dedupe(values: readonly string[]): string[] {
	return [...new Set(values)];
}

export const MODELS_DEV_PROVIDER_KEY_MAP = Object.fromEntries(
	PROVIDER_IDS_MAP.flatMap((entry) =>
		entry.generatedProviderId
			? [[entry.modelsDevKey, entry.generatedProviderId]]
			: [],
	),
);

/**
 * 即使其 models.dev 条目使用受支持的 AI SDK 包，也必须保持
 * 排除的 Provider。ID 使用应用 MODELS_DEV_PROVIDER_KEY_MAP
 * 之后的 Cline 生成 provider 标识符。
 */
export const MODELS_DEV_BLOCKED_PROVIDER_IDS: ReadonlySet<string> = new Set();

export const MODELS_DEV_CURRENT_BUILTIN_PROVIDER_KEYS = new Set(
	PROVIDER_IDS_MAP.map((entry) => entry.modelsDevKey),
);

export function resolveGeneratedProviderIdForModelsDevKey(
	modelsDevKey: string,
): string | undefined {
	return MODELS_DEV_PROVIDER_KEY_MAP[modelsDevKey];
}

export function resolveProviderModelCatalogKeys(providerId: string): string[] {
	const mapped = PROVIDER_IDS_MAP.flatMap((entry) => {
		if (!entry.generatedProviderId) {
			return [];
		}
		if (
			entry.generatedProviderId === providerId ||
			entry.runtimeProviderId === providerId
		) {
			return [entry.generatedProviderId];
		}
		return [];
	});

	if (providerId === "nousResearch") {
		return dedupe([...mapped, "nousresearch", providerId]);
	}

	return dedupe([...mapped, providerId]);
}
