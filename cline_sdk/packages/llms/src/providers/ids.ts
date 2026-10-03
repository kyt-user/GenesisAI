/**
 * 内置 provider ID
 *
 * 所有内置 provider 标识符的唯一事实来源。
 * 运行时操作（校验、迭代）使用 BUILT_IN_PROVIDER_IDS
 * 编译期类型安全使用 BuiltInProviderId 类型
 */
import { GENERATED_PROVIDER_IDS } from "./provider-ids.generated";

export enum BUILT_IN_PROVIDER {
	// 第一方
	ANTHROPIC = "anthropic",
	CLAUDE_CODE = "claude-code",
	CLINE = "cline",
	CLINE_PASS = "cline-pass",
	ELEVENLABS = "elevenlabs",
	// OpenAI 变体
	OPENAI_COMPATIBLE = "openai-compatible",
	OPENAI_NATIVE = "openai-native",
	OPENAI_CODEX = "openai-codex",
	OPENAI_CODEX_CLI = "openai-codex-cli",
	// CLI / 订阅制 provider
	OPENCODE = "opencode",
	// 云 provider
	BEDROCK = "bedrock",
	VERTEX = "vertex",
	GEMINI = "gemini",
	// 本地/自托管
	OLLAMA = "ollama",
	LMSTUDIO = "lmstudio",
	// OpenAI 兼容
	DEEPSEEK = "deepseek",
	XAI = "xai",
	TOGETHER = "together",
	FIREWORKS = "fireworks",
	GROQ = "groq",
	POOLSIDE = "poolside",
	CEREBRAS = "cerebras",
	SAMBANOVA = "sambanova",
	NEBIUS = "nebius",
	CRUSOE = "crusoe",
	BASETEN = "baseten",
	REQUESTY = "requesty",
	LITELLM = "litellm",
	HUGGINGFACE = "huggingface",
	VERCEL_AI_GATEWAY = "vercel-ai-gateway",
	V0 = "v0",
	AIAND = "aiand",
	AIHUBMIX = "aihubmix",
	HICAP = "hicap",
	NOUS_RESEARCH = "nousResearch",
	HUAWEI_CLOUD_MAAS = "huawei-cloud-maas",
	WANDB = "wandb",
	XIAOMI = "xiaomi",
	TENCENT_TOKENHUB = "tencent-tokenhub",
	KILO = "kilo",
	ZAI = "zai",
	ZAI_CODING_PLAN = "zai-coding-plan",
	// 区域/专用
	QWEN = "qwen",
	QWEN_CODE = "qwen-code",
	DOUBAO = "doubao",
	MISTRAL = "mistral",
	MOONSHOT = "moonshot",
	ASKSAGE = "asksage",
	MINIMAX = "minimax",
	DIFY = "dify",
	OCA = "oca",
	SAPAICORE = "sapaicore",
	// 聚合器
	OPENROUTER = "openrouter",
}

/**
 * 归一化为规范内置 ID 的 provider ID 别名。
 *
 * 保持此映射为别名处理的唯一事实来源。
 */
export type GeneratedBuiltInProviderId =
	(typeof GENERATED_PROVIDER_IDS)[number];

export type BuiltInProviderId = BUILT_IN_PROVIDER | GeneratedBuiltInProviderId;

export const PROVIDER_ID_ALIASES: Record<string, BuiltInProviderId> = {
	openai: BUILT_IN_PROVIDER.OPENAI_COMPATIBLE,
	togetherai: BUILT_IN_PROVIDER.TOGETHER,
	"sap-ai-core": BUILT_IN_PROVIDER.SAPAICORE,
};

export const BUILT_IN_PROVIDER_IDS = [
	...new Set([...Object.values(BUILT_IN_PROVIDER), ...GENERATED_PROVIDER_IDS]),
] as readonly BuiltInProviderId[];

/** 检查字符串是否为有效的内置 provider ID */
export function isBuiltInProviderId(id: string): id is BuiltInProviderId {
	return BUILT_IN_PROVIDER_IDS.includes(id as BuiltInProviderId);
}

/** 将 provider 别名归一化为规范 ID */
export function normalizeProviderId(providerId: string): string {
	const normalized = providerId.trim();
	return PROVIDER_ID_ALIASES[normalized] ?? normalized;
}
