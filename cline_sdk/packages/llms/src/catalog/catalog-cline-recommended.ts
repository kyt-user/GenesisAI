import { getClineEnvironmentConfig } from "@cline/shared";
import { buildClineClientHeaders } from "../providers/cline-client-headers";
import type { ModelInfo } from "./types";

export interface ClineRecommendedModelEntry {
	id: string;
	name?: string;
	description?: string;
	tags?: string[];
}

export interface ClineRecommendedModelsPayload {
	recommended?: ClineRecommendedModelEntry[];
	clinePass?: ClineRecommendedModelEntry[];
	free?: ClineRecommendedModelEntry[];
	clineCloud?: ClineRecommendedModelEntry[];
}

type ModelCapabilities = Pick<
	ModelInfo,
	| "contextWindow"
	| "maxInputTokens"
	| "maxTokens"
	| "capabilities"
	| "reasoningOptions"
	| "pricing"
>;

const CLINE_PASS_PROVIDER_ID = "cline-pass";
const CLINE_PROVIDER_ID = "cline";

const CLINE_PASS_MODEL_DEFAULTS = {
	contextWindow: 128_000,
	maxInputTokens: 128_000,
	maxTokens: 8_192,
	capabilities: ["tools", "reasoning", "temperature"],
	pricing: {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
	},
} as const satisfies ModelCapabilities;

function findORModelCapabilities(
	entry: ClineRecommendedModelEntry,
	openRouterModels: Record<string, ModelInfo>,
): ModelCapabilities {
	if (!openRouterModels) {
		return CLINE_PASS_MODEL_DEFAULTS;
	}

	const modelSlug = entry.id.split("/").at(-1) ?? entry.id;

	return openRouterModels[modelSlug] || CLINE_PASS_MODEL_DEFAULTS;
}

// Cline-Pass 模型只有模型名称（没有实验室前缀），
// 因此我们需要用 glm-5.2 而不是 cline-pass/glm-5.2 来查找
function buildModelsNameMap(
	openrouterModels: Record<string, ModelInfo>,
): Record<string, ModelInfo> {
	const nameMap: Record<string, ModelInfo> = {};

	for (const model of Object.values(openrouterModels)) {
		const modelSlugWithoutProvider = model.id.split("/").at(-1) ?? model.id;

		nameMap[modelSlugWithoutProvider] = model;
	}

	return nameMap;
}

export function normalizeClineRecommendedProviderModels(
	payload: ClineRecommendedModelsPayload,
	openRouterModels: Record<string, ModelInfo>,
	options: { includeClineCloudModels?: boolean } = {},
): Record<string, Record<string, ModelInfo>> {
	const clinePass = payload.clinePass ?? [];
	const models: Record<string, ModelInfo> = {};
	const clineModels: Record<string, ModelInfo> = {};
	const openRouterModelsByName = buildModelsNameMap(openRouterModels);

	clinePass.forEach((entry) => {
		const capabilities = findORModelCapabilities(entry, openRouterModelsByName);

		models[entry.id] = {
			// 应使用 OR 名称，除非没有（例如使用默认值时）
			name: entry.name,
			...capabilities,
			pricing: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			id: entry.id,
			description: entry.description,
		};
	});

	const addClineModel = (
		entry: ClineRecommendedModelEntry,
		includeInClinePass: boolean,
	) => {
		const capabilities =
			openRouterModels?.[entry.id] ??
			findORModelCapabilities(entry, openRouterModelsByName);
		// recommended-models 端点只发送类 slug 名称（例如
		// "deepseek-v4-flash"），因此对每个免费条目优先使用 OpenRouter
		// 目录的显示名。否则，免费覆盖层会覆盖合并后的
		// cline/cline-pass 目录中漂亮的 OpenRouter 名称，
		// 选择器最终为 Free 区域渲染原始模型 id。
		const entryName =
			capabilities.name?.trim() || entry.name?.trim() || entry.id;
		// feed 桶决定免费访问权，与 ID 命名空间无关。
		// 即使客户端没有 featured 层元数据也保持可见。
		const name =
			!includeInClinePass || /\(free\)$/i.test(entryName)
				? entryName
				: `${entryName} (free)`;

		const modelInfo = {
			...capabilities,
			name,
			id: entry.id,
			description: entry.description,
		};

		clineModels[entry.id] = {
			...modelInfo,
			pricing: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		};

		if (!includeInClinePass || models[entry.id]) {
			return;
		}

		models[entry.id] = {
			...modelInfo,
			pricing: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		};
	};

	(payload.free ?? []).forEach((entry) => {
		addClineModel(entry, true);
	});
	if (options.includeClineCloudModels) {
		(payload.clineCloud ?? []).forEach((entry) => {
			addClineModel(entry, false);
		});
	}

	const result: Record<string, Record<string, ModelInfo>> = {};
	if (Object.keys(clineModels).length > 0) {
		result[CLINE_PROVIDER_ID] = clineModels;
	}
	if (clinePass.length > 0) {
		result[CLINE_PASS_PROVIDER_ID] = models;
	}
	return result;
}

export async function fetchClineRecommendedModelsPayload(
	fetcher: typeof fetch = fetch,
): Promise<ClineRecommendedModelsPayload> {
	const url = `${getClineEnvironmentConfig().apiBaseUrl}/api/v1/ai/cline/recommended-models`;
	const response = await fetcher(url, { headers: buildClineClientHeaders() });
	if (!response.ok) {
		throw new Error(
			`Failed to load Cline recommended models from ${url}: HTTP ${response.status}`,
		);
	}

	return (await response.json()) as ClineRecommendedModelsPayload;
}

export async function fetchClineRecommendedProviderModels(
	fetcher: typeof fetch = fetch,
	openRouterModels: Record<string, ModelInfo>,
): Promise<Record<string, Record<string, ModelInfo>>> {
	const payload = await fetchClineRecommendedModelsPayload(fetcher);
	return normalizeClineRecommendedProviderModels(payload, openRouterModels);
}
