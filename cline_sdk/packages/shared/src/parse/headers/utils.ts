export function parseKeyPairsIntoRecord(
	value?: string,
): Record<string, string> {
	const result: Record<string, string> = {};

	if (!value) {
		return result;
	}

	value.split(",").forEach((entry) => {
		const separatorIndex = entry.indexOf("=");
		if (separatorIndex <= 0) return;

		try {
			// 从开头到等号是键，从等号到结尾是值
			const key = decodeURIComponent(entry.substring(0, separatorIndex).trim());
			const value = decodeURIComponent(
				entry.substring(separatorIndex + 1).trim(),
			);

			if (!key || !value) return;

			result[key] = value;
		} catch {
			// 跳过单个格式错误的条目（例如使 decodeURIComponent 抛错的
			// 无效百分号编码），而不是中止整个列表并静默
			// 丢弃所有剩余键值对。
		}
	});

	return result;
}
