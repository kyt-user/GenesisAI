// LanguageModelV4 中间件：从 `role:"tool"` 消息中抢救图片字节——
// 否则下游 chat-completions 转换器会销毁这些消息的 content 数组。
//
// 背景：AI SDK 的 `LanguageModelV4ToolResultOutput`（类型 `'content'`）
// 可能包含 `image-data`、`image-url`、`file-data`、`file-url` 或
// `image-file-id` 分片。OpenAI Chat Completions 线路格式并不
// 支持多模态工具消息——`role:"tool"` 的 content 必须是单个
// 字符串。因此 `@ai-sdk/openai-compatible` 的 chat-messages 转换器
// 只是对分片数组做 `JSON.stringify`。图片字节以转义 base64 的
// 形式存活在字符串中，模型会把它当作约 50KB 的不透明文本，
// 并臆想出图片的实际内容。
//
// 本中间件在下游转换器运行之前，对类型化的 `LanguageModelV4Prompt`
// 操作。对于每条在工具结果的 `output.type === 'content'` 值中包含
// 图片/文件分片的 `role:"tool"` 消息，它会：
//
//   1. 把工具结果中的媒体分片替换为占位
//      文本分片：`(see following user message for image)`。
//      工具结果于是只携带文本——对任何线路格式都安全。
//   2. 紧跟在工具消息之后插入一条合成的 `role:"user"` 消息，
//      以 `LanguageModelV4FilePart` 的形式携带媒体分片。
//
// 由于合成用户消息是类型化的（不是原始 JSON），每个
// 下游转换器——Chat Completions、Mistral、Anthropic、Bedrock
// 等——都能把它翻译成自己的原生多模态用户内容形态，
// 无需额外帮助。
//
// 该模式记载于 OpenAI Chat Completions 规范
//（连续 `user` 消息会被模型拼接），并且是经典 Cline 的
// `convertToOpenAiMessages` 已验证线路模式的直接移植
//（见 origin/main 中的 `src/core/api/transform/openai-format.ts`）。
//
// 取代 fetch 拦截器 `vendors/openai-compatible-image-rewrite.ts`，
// 后者在线路层（转换器之后）做同样的重写。中间件方案更
// 受青睐，因为：
//   * 每次 chat-completions 请求都不需要 JSON 解析 / 重新序列化的
//     往返。
//   * 对任何转换器同样有效——`@ai-sdk/mistral`（有自己的
//     chat-messages 转换器）此前未被覆盖。
//   * 与 AI SDK 的线路输出形态解耦：即使 SDK 将来改变
//     内容数组的序列化方式，本层也不关心。

import type {
	LanguageModelV4CallOptions,
	LanguageModelV4FilePart,
	LanguageModelV4Message,
	LanguageModelV4Middleware,
	LanguageModelV4TextPart,
	LanguageModelV4ToolResultOutput,
	LanguageModelV4ToolResultPart,
} from "@ai-sdk/provider";
import {
	createMediaBudgetState,
	DEFAULT_MAX_IMAGE_DECODED_BYTES,
	DEFAULT_MAX_IMAGE_ENCODED_BYTES,
	IMAGE_OMITTED_PLACEHOLDER,
	isCanonicalBase64,
	type MediaBudgetState,
	reserveImageMediaBytes,
	validateAndReserveImageMedia,
} from "@cline/shared";

const IMAGE_PLACEHOLDER = "(see following user message for image)";

type ContentOutput = Extract<
	LanguageModelV4ToolResultOutput,
	{ type: "content" }
>;
type ContentPart = ContentOutput["value"][number];

function isMediaContentPart(
	part: ContentPart,
): part is Extract<ContentPart, { type: "file" }> {
	return part.type === "file";
}

/**
 * 将 `ToolResultOutput`（类型 `'content'`）中的媒体分片转换为
 * 等价的 `LanguageModelV4FilePart`，用于用户消息。
 * 对 `image-file-id` 分片返回 `null`，因为 `LanguageModelV4FilePart`
 * 没有 provider-file-id 槽位——这些分片保留在
 * 工具结果中，原样传给转换器。（Image-file-id 是
 * OpenAI 特有的引用；使用它的调用方已经
 * 走在多模态感知路径上，不需要本重写。）
 */
function mediaPartToFilePart(
	part: Extract<ContentPart, { type: "file" }>,
): LanguageModelV4FilePart | null {
	if (part.data.type === "reference") {
		return null;
	}
	return {
		type: "file",
		data: part.data,
		mediaType: part.mediaType,
		...(part.filename ? { filename: part.filename } : {}),
		...(part.providerOptions ? { providerOptions: part.providerOptions } : {}),
	};
}

interface SplitResult {
	stripped: ContentOutput;
	media: LanguageModelV4FilePart[];
}

function imageOmittedTextPart(): LanguageModelV4TextPart {
	return {
		type: "text",
		text: IMAGE_OMITTED_PLACEHOLDER,
	};
}

function reserveUnknownUrlMediaBudget(
	url: string,
	mediaState: MediaBudgetState,
): boolean {
	let parsed: URL;
	try {
		parsed = new URL(url);
	} catch {
		return false;
	}

	if (parsed.protocol === "data:") {
		const commaIndex = url.indexOf(",");
		if (commaIndex === -1) {
			return false;
		}
		const metadata = url.slice("data:".length, commaIndex).toLowerCase();
		if (!metadata.endsWith(";base64")) {
			return false;
		}
		const base64 = url.slice(commaIndex + 1);
		if (!isCanonicalBase64(base64)) {
			return false;
		}
		return (
			reserveImageMediaBytes(
				base64.length,
				0,
				{
					maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
					maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
				},
				mediaState,
			) === null
		);
	}

	if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
		return false;
	}

	// 远程 URL 的字节大小在格式化时未知，因此按保守的
	// 单图上限计入，而不是让 URL 媒体免费通过。
	return (
		reserveImageMediaBytes(
			DEFAULT_MAX_IMAGE_ENCODED_BYTES,
			0,
			{
				maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
				maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
			},
			mediaState,
		) === null
	);
}

function isDataUrl(url: string): boolean {
	try {
		return new URL(url).protocol === "data:";
	} catch {
		return false;
	}
}

function estimateMediaDataEncodedBytes(data: unknown): number {
	if (typeof data === "string") {
		return data.length;
	}
	if (data instanceof Uint8Array) {
		return data.byteLength;
	}
	if (data instanceof ArrayBuffer) {
		return data.byteLength;
	}
	return Number.POSITIVE_INFINITY;
}

function reserveGenericMediaDataBudget(
	data: unknown,
	mediaState: MediaBudgetState,
): boolean {
	return (
		reserveImageMediaBytes(
			estimateMediaDataEncodedBytes(data),
			0,
			{
				maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
				maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
			},
			mediaState,
		) === null
	);
}

/**
 * 将工具结果的 `output`（类型 `'content'`）拆分为：
 *   - 一个 `stripped` 输出，其中每个媒体分片都被替换为
 *     占位文本分片，以及
 *   - 转换为 `LanguageModelV4FilePart` 的媒体分片列表。
 *
 * 如果输出不是 `'content'` 类型，或不携带任何可提取的
 * 媒体分片（此时无需重写），返回 `null`。
 */
function splitContentOutputMedia(
	output: LanguageModelV4ToolResultOutput,
	mediaState: MediaBudgetState,
): SplitResult | null {
	if (output.type !== "content") {
		return null;
	}
	const media: LanguageModelV4FilePart[] = [];
	const newValue: ContentOutput["value"] = [];
	let mutated = false;
	for (const part of output.value) {
		if (!isMediaContentPart(part)) {
			newValue.push(part);
			continue;
		}
		let currentPart = part;
		if (
			currentPart.mediaType.startsWith("image/") &&
			currentPart.data.type === "data" &&
			typeof currentPart.data.data === "string"
		) {
			const validation = validateAndReserveImageMedia(
				currentPart.mediaType,
				currentPart.data.data,
				{
					maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
					maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
				},
				mediaState,
			);
			if (!validation.ok) {
				newValue.push({
					type: "text",
					text: IMAGE_OMITTED_PLACEHOLDER,
				});
				mutated = true;
				continue;
			}
			currentPart = {
				...currentPart,
				data: { type: "data", data: validation.base64 },
				mediaType: validation.mediaType,
			};
		} else if (
			currentPart.mediaType.startsWith("image/") &&
			currentPart.data.type === "url"
		) {
			const url = currentPart.data.url.toString();
			if (isDataUrl(url)) {
				const validation = validateAndReserveImageMedia(
					undefined,
					url,
					{
						maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
						maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
					},
					mediaState,
				);
				if (!validation.ok) {
					newValue.push(imageOmittedTextPart());
					mutated = true;
					continue;
				}
				currentPart = {
					...currentPart,
					data: {
						type: "url",
						url: new URL(
							`data:${validation.mediaType};base64,${validation.base64}`,
						),
					},
				};
			} else if (!reserveUnknownUrlMediaBudget(url, mediaState)) {
				newValue.push(imageOmittedTextPart());
				mutated = true;
				continue;
			}
		} else if (currentPart.data.type === "url") {
			if (
				!reserveUnknownUrlMediaBudget(
					currentPart.data.url.toString(),
					mediaState,
				)
			) {
				newValue.push(imageOmittedTextPart());
				mutated = true;
				continue;
			}
		} else if (currentPart.data.type === "data") {
			if (!reserveGenericMediaDataBudget(currentPart.data.data, mediaState)) {
				newValue.push(imageOmittedTextPart());
				mutated = true;
				continue;
			}
		}
		const filePart = mediaPartToFilePart(currentPart);
		if (!filePart) {
			// 未处理的媒体种类（image-file-id）——原样通过。
			newValue.push(currentPart);
			continue;
		}
		media.push(filePart);
		mutated = true;
		const placeholder: LanguageModelV4TextPart = {
			type: "text",
			text: IMAGE_PLACEHOLDER,
		};
		newValue.push(placeholder);
	}
	if (!mutated) {
		return null;
	}
	return {
		stripped: {
			type: "content",
			value: newValue,
		},
		media,
	};
}

/**
 * 遍历 `LanguageModelV4Prompt`，重写每条在工具结果 `output` 中
 * 包含图片/文件分片的 `role:"tool"` 消息。重写形态见
 * 文件级注释。
 *
 * 返回（可能是新的）prompt 数组以及供测试/观察使用的
 * `mutated` 标志。
 */
export function rewritePromptToolImages(prompt: LanguageModelV4Message[]): {
	prompt: LanguageModelV4Message[];
	mutated: boolean;
} {
	const newPrompt: LanguageModelV4Message[] = [];
	let mutated = false;
	const mediaState = createMediaBudgetState();

	for (const message of prompt) {
		if (message.role !== "tool") {
			newPrompt.push(message);
			continue;
		}

		const collectedMedia: LanguageModelV4FilePart[] = [];
		const newContent: typeof message.content = message.content.map((part) => {
			if (part.type !== "tool-result") {
				return part;
			}
			const split = splitContentOutputMedia(part.output, mediaState);
			if (!split) {
				return part;
			}
			collectedMedia.push(...split.media);
			mutated = true;
			const newPart: LanguageModelV4ToolResultPart = {
				...part,
				output: split.stripped,
			};
			return newPart;
		});

		newPrompt.push({ ...message, content: newContent });

		if (collectedMedia.length > 0) {
			newPrompt.push({
				role: "user",
				content: collectedMedia,
			});
		}
	}

	return { prompt: newPrompt, mutated };
}

/**
 * 拆分携带图片的工具结果消息的 `LanguageModelV4Middleware`，
 * 使 chat-completions 风格转换器不会丢失字节。
 *
 * 通过 `wrapLanguageModel({ model, middleware: splitToolImagesMiddleware })`
 * 应用于任何下游转换器不原生处理多模态 `role:"tool"` 内容的
 * provider（目前为：`@ai-sdk/openai-compatible`、
 * `@ai-sdk/mistral`）。
 *
 * Anthropic 的转换器原生渲染工具结果消息上的 content 数组，
 * 不应使用本中间件——它会不必要地把结构忠实的工具结果
 * 替换为占位文本 + 相邻用户消息模式。
 */
export const splitToolImagesMiddleware: LanguageModelV4Middleware = {
	specificationVersion: "v4",
	transformParams: async ({ params }) => {
		const { prompt: newPrompt, mutated } = rewritePromptToolImages(
			params.prompt,
		);
		if (!mutated) {
			return params;
		}
		const next: LanguageModelV4CallOptions = {
			...params,
			prompt: newPrompt,
		};
		return next;
	},
};
