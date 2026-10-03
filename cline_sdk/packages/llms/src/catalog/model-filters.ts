import type { ModelInfo } from "./types";

/**
 * 从模型目录中移除声明图片输出的模型。
 *
 * 包括专用图片生成模型和能返回文本与图片混合输出的语言模型。
 * Cline 有意在 `buildClineModels`（捆绑目录）和
 * `mergeKnownModels`（合并的运行时来源，包括用户模型覆盖
 * ——无论模型在哪里配置，后端都拒绝图片输出）
 * 两处应用此临时后端限制。当推理后端支持图片输出时，
 * 一同移除这两处过滤调用点。
 */
export function filterImageOutputModels(
	models: Record<string, ModelInfo>,
): Record<string, ModelInfo> {
	return Object.fromEntries(
		Object.entries(models).filter(
			([, model]) =>
				model.operation !== "image-generation" &&
				model.modalities?.output.includes("image") !== true,
		),
	);
}
