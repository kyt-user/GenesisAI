import type { ModelInfo } from "../catalog/types";

/**
 * ChatGPT/Codex 后端在达到模型声明输入上限的约 95% 时
 * 开始拒绝请求，因此通过此 provider 暴露的每个模型
 * 的 maxInputTokens 都会缩减到有效预算。
 *
 * REF: https://github.com/openai/codex/issues/19319
 */
export const CODEX_EFFECTIVE_CONTEXT_WINDOW_PERCENT = 0.95;

/**
 * 无论 OpenAI API 目录为同一模型声明什么，Codex 都提供
 * 400K 上下文 / 272K 输入 / 128K 输出预算（API 对 GPT-5.5+ 列出
 * 1.05M）。镜像 Codex CLI，使上下文跟踪在
 * 各客户端间保持一致。
 */
const CODEX_CONTEXT_WINDOW = 400_000;
const CODEX_MAX_INPUT_TOKENS = 272_000;
const CODEX_MAX_OUTPUT_TOKENS = 128_000;

/**
 * 资格判定镜像 opencode 针对共享 OpenAI 目录的 ChatGPT-plan 规则：
 * 显式的允许/拒绝列表加上「比 GPT-5.4 更新」的版本
 * 规则。GPT-5.4 和 GPT-5.4 mini 于
 * 2026-08-31 对 ChatGPT 账户退役（替代：GPT-5.6 Terra 和 GPT-5.6 Luna）。
 *
 * REF: https://github.com/anomalyco/opencode/blob/v2/packages/core/src/plugin/provider/openai.ts
 * REF: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
 */
const OPENAI_CODEX_ALLOWED_MODELS = new Set(["gpt-5.5", "gpt-5.3-codex-spark"]);
// `gpt-5.6` 是 Sol 变体的 API 别名；Codex 只提供具名的
// GPT-5.6 变体。
const OPENAI_CODEX_DISALLOWED_MODELS = new Set(["gpt-5.5-pro", "gpt-5.6"]);

const GPT_VERSION_REGEX = /^gpt-(\d+)(?:\.(\d+))?/;

function isOpenAICodexAllowedModel(id: string, model: ModelInfo): boolean {
	// O、pro 和 nano 变体不受支持
	const family = model.family;
	if (
		family &&
		(family.startsWith("o") ||
			family.includes("pro") ||
			family.includes("nano"))
	) {
		return false;
	}
	if (OPENAI_CODEX_ALLOWED_MODELS.has(id)) return true;
	if (OPENAI_CODEX_DISALLOWED_MODELS.has(id)) return false;
	// 必须比 5.4 更新；省略的次版本号视为零（例如 gpt-6-astra）
	const match = id.match(GPT_VERSION_REGEX);
	if (!match) return false;
	const major = Number(match[1]);
	const minor = Number(match[2] ?? 0);
	return major > 5 || (major === 5 && minor > 4);
}

function toOpenAICodexModel(model: ModelInfo): ModelInfo {
	return {
		...model,
		contextWindow: model.contextWindow
			? Math.min(model.contextWindow, CODEX_CONTEXT_WINDOW)
			: model.contextWindow,
		maxInputTokens: model.maxInputTokens
			? Math.min(model.maxInputTokens, CODEX_MAX_INPUT_TOKENS) *
				CODEX_EFFECTIVE_CONTEXT_WINDOW_PERCENT
			: model.maxInputTokens,
		maxTokens: model.maxTokens
			? Math.min(model.maxTokens, CODEX_MAX_OUTPUT_TOKENS)
			: model.maxTokens,
	};
}

export function filterOpenAICodexModels(
	models: Record<string, ModelInfo>,
): Record<string, ModelInfo> {
	const result: Record<string, ModelInfo> = {};
	for (const [id, model] of Object.entries(models)) {
		if (isOpenAICodexAllowedModel(id, model)) {
			result[id] = toOpenAICodexModel(model);
		}
	}
	return result;
}
