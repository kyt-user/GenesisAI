import { isTranscriptionModel } from "@cline/shared";
import type { ModelOperation, ModelOperationMode } from "./types";

interface CatalogOperationDescriptor {
	id?: string;
	name?: string;
	tags?: readonly string[];
	operation?: ModelOperation;
	family?: string;
	modalities?: {
		input?: readonly string[];
		output?: readonly string[];
	};
}

// models.dev 缺少实时传输标签。在此归一化其有界的身份
// 标记，仅适用于接受音频的模型；普通多模态聊天
// 保持语言操作。运行时消费者使用显式 operation。
function hasRealtimeIdentity(model: CatalogOperationDescriptor): boolean {
	return [model.id, model.name, model.family].some((value) =>
		/(?:^|[\s/_.-])(?:realtime|live)(?:$|[\s/_.-])/i.test(value ?? ""),
	);
}

/**
 * 在摄取外部目录事实时对 provider 操作进行分类。
 * 这是有意放在目录边界的归一化；运行时路由读取
 * 结果中的显式 `operation`，绝不从 provider 家族
 * 或模态推断端点。
 */
export function resolveCatalogModelOperation(
	model: CatalogOperationDescriptor,
): ModelOperation {
	if (isTranscriptionModel(model)) {
		return "transcription";
	}
	if (
		model.operation === "realtime" ||
		(model.modalities?.input?.includes("audio") &&
			(model.operation === "transcription" ||
				model.tags?.includes("websocket-realtime") ||
				model.tags?.includes("websocket-transcription") ||
				hasRealtimeIdentity(model)))
	) {
		return "realtime";
	}
	if (model.operation) {
		return model.operation;
	}
	const output = model.modalities?.output;
	if (
		output?.includes("image") === true &&
		(output.includes("text") !== true ||
			model.family?.trim().toLowerCase() === "gpt-image")
	) {
		return "image-generation";
	}
	if (output?.includes("audio") === true && output.includes("text") !== true) {
		return "speech-generation";
	}
	if (output?.includes("video") === true && output.includes("text") !== true) {
		return "video-generation";
	}
	return "language";
}

/**
 * 在目录边界归一化操作特有的执行模式。
 * models.dev 目前不暴露批量/流式字段，因此实时
 * 转录标识符在这里被识别一次，并持久化为对每个
 * 运行时和客户端而言的显式事实。
 */
export function resolveCatalogModelOperationModes(
	modelId: string,
	model: CatalogOperationDescriptor,
): ModelOperationMode[] | undefined {
	const descriptor = { ...model, id: modelId };
	const operation = resolveCatalogModelOperation(descriptor);
	if (operation === "realtime") return ["streaming"];
	if (operation !== "transcription") {
		return undefined;
	}
	return [
		model.tags?.includes("websocket-transcription") ||
		hasRealtimeIdentity(descriptor)
			? "streaming"
			: "batch",
	];
}
