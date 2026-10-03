export interface ModelIdAliasRule {
	canonicalPrefix: string;
	aliasPrefix: string;
}

// 一些上游目录以不同 provider 命名空间暴露同一个路由模型。
// 优先选择我们运行时能接收的命名空间，使精确的模型信息
// 查找保持正确的上下文窗口和 token 上限。
export const VERCEL_OPENROUTER_MODEL_ID_ALIAS_RULES = [
	{ canonicalPrefix: "zai/", aliasPrefix: "z-ai/" },
] as const satisfies readonly ModelIdAliasRule[];

export function isCanonicalModelIdForAliasRules(
	modelId: string,
	rules: readonly ModelIdAliasRule[],
): boolean {
	return rules.some((rule) => modelId.startsWith(rule.canonicalPrefix));
}

export function preferCanonicalModelIds<T>(
	models: Record<string, T>,
	rules: readonly ModelIdAliasRule[],
): Record<string, T> {
	return Object.fromEntries(
		Object.entries(models).filter(([modelId]) => {
			for (const rule of rules) {
				if (!modelId.startsWith(rule.aliasPrefix)) continue;
				const canonicalModelId = `${rule.canonicalPrefix}${modelId.slice(rule.aliasPrefix.length)}`;
				if (canonicalModelId in models) return false;
			}
			return true;
		}),
	);
}
