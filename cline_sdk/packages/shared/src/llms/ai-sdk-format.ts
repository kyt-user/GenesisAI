import { formatFileContentBlock } from "../prompt/format";
import {
	createMediaBudgetState,
	DEFAULT_MAX_IMAGE_DECODED_BYTES,
	DEFAULT_MAX_IMAGE_ENCODED_BYTES,
	type GeneratedMedia,
	type GeneratedMediaModality,
	IMAGE_OMITTED_PLACEHOLDER,
	IMAGE_UNSUPPORTED_PLACEHOLDER,
	imageBase64LengthForDecodedBytes,
	type MediaBudgetState,
	reserveImageMediaBytes,
	SUPPORTED_IMAGE_MEDIA_TYPES,
	validateAndReserveImageMedia,
} from "./media";

/**
 * 清理文本内容中未配对的/孤立的 Unicode 代理项。
 *
 * 孤立代理项（没有匹配低代理项的高代理项，或反之）
 * 在向 LLM provider 发送文本时可能导致 JSON 序列化问题和
 * 下游处理错误。此函数将它们替换为 Unicode 替换
 * 字符（U+FFFD）。
 *
 * @param content - 要清理的字符串
 * @returns 孤立代理项被替换为 U+FFFD 的字符串
 */
export function sanitizeSurrogates(content: string): string {
	return content.replace(
		/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/g,
		"\uFFFD",
	);
}

export type AiSdkFormatterMessageRole = "user" | "assistant" | "tool";

export type AiSdkFormatterPart =
	| {
			type: "text";
			text: string;
			providerOptions?: Record<string, Record<string, unknown>>;
	  }
	| {
			type: "reasoning";
			text: string;
			providerOptions?: Record<string, Record<string, unknown>>;
	  }
	| {
			type: "image";
			image: string | Uint8Array | ArrayBuffer | URL;
			mediaType?: string;
	  }
	| {
			type: "media";
			media: GeneratedMedia;
	  }
	| {
			type: "file";
			path: string;
			content: string;
	  }
	| {
			type: "tool-call";
			toolCallId: string;
			toolName: string;
			input: unknown;
			providerOptions?: Record<string, Record<string, unknown>>;
	  }
	| {
			type: "tool-result";
			toolCallId: string;
			toolName: string;
			output: unknown;
			isError?: boolean;
	  };

export interface AiSdkFormatterMessage {
	role: AiSdkFormatterMessageRole;
	content: string | AiSdkFormatterPart[];
}

export const EMPTY_CONTENT_TEXT = "ERROR: EMPTY CONTENT";
const IMAGE_ATTACHED_TEXT = "[image attached]";
const GENERATED_IMAGE_TEXT = "[generated image]";

function generatedMediaText(modality: GeneratedMediaModality): string {
	return `[generated ${modality}]`;
}

function generatedMediaUnavailableText(
	media: GeneratedMedia,
): AiSdkMessagePart {
	return {
		type: "text",
		text: `[generated ${media.modality} unavailable to this model]`,
	};
}

export type AiSdkMessagePart = Record<string, unknown>;
export type AiSdkMessage = {
	role: "system" | "user" | "assistant" | "tool";
	content: string | AiSdkMessagePart[];
};

type AiSdkContentBlock =
	| { type: "text"; text: string }
	| { type: "image"; data: string; mediaType: string };
type AiSdkImageContentBlock = Extract<AiSdkContentBlock, { type: "image" }>;

/**
 * AI SDK 7 tool-result media part (`LanguageModelV4`-era canonical shape).
 * `data` is the tagged file-data union rather than a bare base64 string.
 */
type ToolResultImagePart = {
	type: "file";
	data: { type: "data"; data: string };
	mediaType: string;
};

interface StripImagesResult {
	value: unknown;
	changed: boolean;
	mediaChanged: boolean;
}

function pushAiSdkMessage(result: AiSdkMessage[], message: AiSdkMessage): void {
	const previous = result[result.length - 1];
	if (
		message.role === "tool" &&
		previous?.role === "tool" &&
		Array.isArray(previous.content) &&
		Array.isArray(message.content)
	) {
		previous.content.push(...message.content);
		return;
	}

	result.push(message);
}

/**
 * 用于工具输出内容块的类型守卫：这些块应作为原生多模态部分
 * 传递给模型（而非 JSON 编码）。我们接受 `formatStructuredToolResult`
 * 使用的 cline `image` 和 `text` 块形状。
 */
function isAiSdkContentBlockArray(
	value: unknown,
): value is AiSdkContentBlock[] {
	if (!Array.isArray(value) || value.length === 0) {
		return false;
	}
	return value.every((block) => {
		if (!block || typeof block !== "object") {
			return false;
		}
		const b = block as Record<string, unknown>;
		if (b.type === "text") {
			return typeof b.text === "string";
		}
		if (b.type === "image") {
			return typeof b.data === "string" && typeof b.mediaType === "string";
		}
		return false;
	});
}

function imageOmittedTextPart(): { type: "text"; text: string } {
	return { type: "text", text: IMAGE_OMITTED_PLACEHOLDER };
}

function reserveRemoteImageUrlBudget(state: MediaBudgetState): boolean {
	// 远程 URL 的字节大小在格式化时未知，因此按保守的
	// 每图像上限计费，而不是让 URL 媒体算作免费。
	return (
		reserveImageMediaBytes(
			DEFAULT_MAX_IMAGE_ENCODED_BYTES,
			0,
			{
				maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
				maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
			},
			state,
		) === null
	);
}

function parseUrlProtocol(value: string): string | undefined {
	try {
		return new URL(value).protocol;
	} catch {
		return undefined;
	}
}

/**
 * 构建工具结果的 `content` 媒体部分。AI SDK 7 将
 * `image-*`/`file-*` 工具结果变体合并为单个 `file` 部分，其
 * `data` 是带标签的联合类型；旧变体仍通过运行时
 * shim 往返，但每个请求都会记录弃用警告。
 */
function toToolResultImagePart(
	image: AiSdkImageContentBlock,
	state: MediaBudgetState,
): ToolResultImagePart | { type: "text"; text: string } {
	const validation = validateAndReserveImageMedia(
		image.mediaType,
		image.data,
		{
			maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
			maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
		},
		state,
	);
	if (!validation.ok) {
		return imageOmittedTextPart();
	}
	return toolResultImagePart(validation.base64, validation.mediaType);
}

function toolResultImagePart(
	base64: string,
	mediaType: string,
): ToolResultImagePart {
	return {
		type: "file",
		data: { type: "data", data: base64 },
		mediaType,
	};
}

/**
 * 构建用户消息的媒体部分。AI SDK 7 弃用了 `image` 消息
 * 部分，改用携带图像 `mediaType` 的 `file` 部分，因此这里
 * 总是发射 `file`。`file` 部分要求 `mediaType`；当
 * 来源未携带时，回退到裸 `image` 顶层类型，
 * AI SDK 7 会按 provider 解析它（尽可能从
 * 内联字节自动检测子类型）。
 */
function userMediaPart(
	data: string | Uint8Array | ArrayBuffer | URL,
	mediaType: string | undefined,
): AiSdkMessagePart {
	return {
		type: "file",
		data,
		mediaType: mediaType ?? "image",
	};
}

function toUserImagePart(
	image: Extract<AiSdkFormatterPart, { type: "image" }>,
	state: MediaBudgetState,
): AiSdkMessagePart {
	if (image.image instanceof URL) {
		if (image.image.protocol === "data:") {
			const validation = validateAndReserveImageMedia(
				image.mediaType,
				image.image.href,
				{
					maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
					maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
				},
				state,
			);
			if (!validation.ok) {
				return imageOmittedTextPart();
			}
			return userMediaPart(
				`data:${validation.mediaType};base64,${validation.base64}`,
				validation.mediaType,
			);
		}
		if (image.image.protocol !== "http:" && image.image.protocol !== "https:") {
			return imageOmittedTextPart();
		}
		if (!reserveRemoteImageUrlBudget(state)) {
			return imageOmittedTextPart();
		}
		return userMediaPart(image.image, image.mediaType);
	}

	if (typeof image.image === "string") {
		const protocol = parseUrlProtocol(image.image);
		if (protocol === "http:" || protocol === "https:") {
			if (!reserveRemoteImageUrlBudget(state)) {
				return imageOmittedTextPart();
			}
			return userMediaPart(image.image, image.mediaType);
		}
		const isDataUrl = protocol === "data:";

		const validation = validateAndReserveImageMedia(
			image.mediaType ?? (isDataUrl ? undefined : "image/png"),
			image.image,
			{
				maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
				maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
			},
			state,
		);
		if (!validation.ok) {
			return imageOmittedTextPart();
		}
		return userMediaPart(
			isDataUrl
				? `data:${validation.mediaType};base64,${validation.base64}`
				: validation.base64,
			validation.mediaType,
		);
	}

	const decodedBytes = image.image.byteLength;
	const encodedBytes = imageBase64LengthForDecodedBytes(decodedBytes);
	const mediaType = image.mediaType?.toLowerCase() ?? "image/png";
	const supportedMediaTypes: readonly string[] = SUPPORTED_IMAGE_MEDIA_TYPES;
	if (
		!supportedMediaTypes.includes(mediaType) ||
		reserveImageMediaBytes(
			encodedBytes,
			decodedBytes,
			{
				maxImageEncodedBytes: DEFAULT_MAX_IMAGE_ENCODED_BYTES,
				maxImageDecodedBytes: DEFAULT_MAX_IMAGE_DECODED_BYTES,
			},
			state,
		)
	) {
		return imageOmittedTextPart();
	}

	return userMediaPart(image.image, mediaType);
}

function supportsGeneratedMediaInput(
	media: GeneratedMedia,
	supportedInputModalities: readonly string[] | undefined,
): boolean {
	if (!supportedInputModalities) return true;
	if (media.modality === "file") {
		return (
			media.mediaType === "application/pdf" &&
			supportedInputModalities.includes("pdf")
		);
	}
	return supportedInputModalities.includes(media.modality);
}

function toUserGeneratedMediaPart(
	media: GeneratedMedia,
	state: MediaBudgetState,
	supportedInputModalities: readonly string[] | undefined,
): AiSdkMessagePart {
	if (!supportsGeneratedMediaInput(media, supportedInputModalities)) {
		return generatedMediaUnavailableText(media);
	}

	if (media.modality === "image") {
		if (media.source.type === "artifact") {
			return generatedMediaUnavailableText(media);
		}
		return toUserImagePart(
			{
				type: "image",
				image:
					media.source.type === "url" ? media.source.url : media.source.data,
				mediaType: media.mediaType,
			},
			state,
		);
	}

	if (media.source.type === "artifact") {
		return generatedMediaUnavailableText(media);
	}
	if (media.source.type === "url") {
		const protocol = parseUrlProtocol(media.source.url);
		if (protocol !== "http:" && protocol !== "https:" && protocol !== "data:") {
			return generatedMediaUnavailableText(media);
		}
	}
	return userMediaPart(
		media.source.type === "url" ? media.source.url : media.source.data,
		media.mediaType,
	);
}

interface StripImagesOptions {
	/**
	 * 为 true 时，有效的图像块被收集到 `images` 中，
	 * 由调用方提升为原生多模态部分。为 false 时，它们
	 * 就地替换为 `inlineImagePlaceholder` 文本（用于
	 * 错误输出和不支持图像输入的模型）。
	 */
	hoistImages: boolean;
	inlineImagePlaceholder: string;
}

/**
 * 递归遍历工具结果的 `output` 值，移除所有 AI-SDK 图像
 * 内容块（`{type:'image', data, mediaType}`）并将它们收集
 * 到 `images` 中。内联文本块（`{type:'text', text}`）被解包
 * 为裸字符串，使结果结构能干净地 JSON 序列化
 * 供模型使用。
 *
 * 返回移除图像后的剥离值（其他结构
 * 保留）。原始输入不会被修改。
 */
function stripImagesFromOutput(
	value: unknown,
	images: AiSdkImageContentBlock[],
	state: MediaBudgetState,
	options: StripImagesOptions,
): StripImagesResult {
	const { hoistImages, inlineImagePlaceholder } = options;
	if (value == null || typeof value !== "object") {
		return { value, changed: false, mediaChanged: false };
	}

	if (Array.isArray(value)) {
		const out: unknown[] = [];
		let changed = false;
		let mediaChanged = false;
		for (const item of value) {
			if (item && typeof item === "object") {
				const obj = item as Record<string, unknown>;
				if (
					obj.type === "image" &&
					typeof obj.data === "string" &&
					typeof obj.mediaType === "string"
				) {
					if (!hoistImages) {
						out.push(inlineImagePlaceholder);
						changed = true;
						mediaChanged = true;
						continue;
					}
					const image = {
						type: "image",
						data: obj.data,
						mediaType: obj.mediaType,
					} satisfies AiSdkImageContentBlock;
					const part = toToolResultImagePart(image, state);
					if (part.type === "file") {
						images.push({
							type: "image",
							data: part.data.data,
							mediaType: part.mediaType,
						});
					} else {
						out.push(part.text);
					}
					changed = true;
					mediaChanged = true;
					continue;
				}
				if (obj.type === "image") {
					out.push(IMAGE_OMITTED_PLACEHOLDER);
					changed = true;
					mediaChanged = true;
					continue;
				}
				if (obj.type === "text" && typeof obj.text === "string") {
					out.push(obj.text);
					changed = true;
					continue;
				}
			}
			const stripped = stripImagesFromOutput(item, images, state, options);
			out.push(stripped.value);
			changed ||= stripped.changed;
			mediaChanged ||= stripped.mediaChanged;
		}
		return { value: changed ? out : value, changed, mediaChanged };
	}

	const obj = value as Record<string, unknown>;
	if (obj.type === "image") {
		if (typeof obj.data === "string" && typeof obj.mediaType === "string") {
			if (!hoistImages) {
				return {
					value: inlineImagePlaceholder,
					changed: true,
					mediaChanged: true,
				};
			}
			const image = {
				type: "image",
				data: obj.data,
				mediaType: obj.mediaType,
			} satisfies AiSdkImageContentBlock;
			const part = toToolResultImagePart(image, state);
			if (part.type === "file") {
				images.push({
					type: "image",
					data: part.data.data,
					mediaType: part.mediaType,
				});
				return {
					value: IMAGE_ATTACHED_TEXT,
					changed: true,
					mediaChanged: true,
				};
			}
			return { value: part.text, changed: true, mediaChanged: true };
		}
		return {
			value: IMAGE_OMITTED_PLACEHOLDER,
			changed: true,
			mediaChanged: true,
		};
	}

	const out: Record<string, unknown> = {};
	let changed = false;
	let mediaChanged = false;
	for (const [k, v] of Object.entries(obj)) {
		const stripped = stripImagesFromOutput(v, images, state, options);
		out[k] = stripped.value;
		changed ||= stripped.changed;
		mediaChanged ||= stripped.mediaChanged;
	}
	return { value: changed ? out : value, changed, mediaChanged };
}

/** 深度清理任意对象/数组中所有嵌套的字符串值。 */
function sanitizeDeepStrings(value: unknown): unknown {
	if (typeof value === "string") {
		return sanitizeSurrogates(value);
	}
	if (Array.isArray(value)) {
		return value.map((item) => sanitizeDeepStrings(item));
	}
	if (value !== null && typeof value === "object") {
		const obj = value as Record<string, unknown>;
		const out: Record<string, unknown> = {};
		for (const [k, v] of Object.entries(obj)) {
			out[k] = sanitizeDeepStrings(v);
		}
		return out;
	}
	return value;
}

export function toAiSdkToolResultOutput(
	output: unknown,
	isError = false,
	mediaState: MediaBudgetState = createMediaBudgetState(),
	options?: { supportsImages?: boolean },
): Record<string, unknown> {
	const supportsImages = options?.supportsImages ?? true;
	if (typeof output === "string") {
		return {
			type: isError ? "error-text" : "text",
			value: sanitizeSurrogates(output),
		};
	}

	// `text` / `image` 内容块数组（例如来自 read_file 图像
	// 结果）必须作为 AI SDK `content` 部分转发，provider 才能
	// 将它们翻译为真正的多模态输入。否则，数组
	// 会落入下面的 `json` 分支，base64 图像数据
	// 会作为 JSON 字符串发送给模型——模型看不到它，
	// 从而幻觉出图像内容。
	// 当目标模型不支持图像输入时，图像块
	// 被替换为占位文本，使模型知道那里
	// 曾有一张图像。
	if (!isError && isAiSdkContentBlockArray(output)) {
		return {
			type: "content",
			value: output.map((block) =>
				block.type === "image"
					? supportsImages
						? toToolResultImagePart(block, mediaState)
						: { type: "text", text: IMAGE_UNSUPPORTED_PLACEHOLDER }
					: { type: "text", text: sanitizeSurrogates(block.text) },
			),
		};
	}

	// 包含嵌套图像块的结构化输出（例如 `read_files` 为图像路径
	// 产生的
	// `[{query, result: ['Successfully read image', {type:'image',...}], success}]`
	// 形状）也必须以原生多模态部分到达模型。遍历结构，
	// 拉出图像块，将其余元数据作为 JSON 字符串化的
	// 文本块转发，后跟提取的图像。否则，线路
	// 转换器会将整棵树 JSON 序列化，模型收到的是
	// 不透明的文本形式的 base64 字节。
	// 对于不支持图像输入的模型，图像会就地替换为
	// 占位文本而非被提升。
	if (output !== null && typeof output === "object") {
		const images: AiSdkImageContentBlock[] = [];
		const stripped = stripImagesFromOutput(output, images, mediaState, {
			hoistImages: !isError && supportsImages,
			inlineImagePlaceholder: supportsImages
				? IMAGE_OMITTED_PLACEHOLDER
				: IMAGE_UNSUPPORTED_PLACEHOLDER,
		});
		if (!isError && images.length > 0) {
			const headerText =
				typeof stripped.value === "string"
					? sanitizeSurrogates(stripped.value)
					: JSON.stringify(sanitizeDeepStrings(stripped.value));
			return {
				type: "content",
				value: [
					{ type: "text", text: headerText },
					...images.map((image) =>
						toolResultImagePart(image.data, image.mediaType),
					),
				],
			};
		}
		if (stripped.mediaChanged) {
			return {
				type: isError ? "error-json" : "json",
				value: sanitizeDeepStrings(stripped.value),
			};
		}
	}

	if (
		output === null ||
		typeof output === "boolean" ||
		typeof output === "number" ||
		typeof output === "object"
	) {
		return {
			type: isError ? "error-json" : "json",
			value: sanitizeDeepStrings(output),
		};
	}

	return {
		type: isError ? "error-text" : "text",
		value: sanitizeSurrogates(String(output)),
	};
}

export function formatMessagesForAiSdk(
	systemContent: string | AiSdkMessagePart[] | undefined,
	messages: readonly AiSdkFormatterMessage[],
	options?: {
		assistantToolCallArgKey?: "args" | "input";
		/**
		 * 目标模型是否声明支持图像输入。为 false 时，图像
		 * 部分（用户附加的和工具结果内的）被替换为
		 * `IMAGE_UNSUPPORTED_PLACEHOLDER` 文本，使请求对
		 * 纯文本模型保持有效，同时模型仍知道曾有一张图像。
		 * 默认为 true。替换仅发生在请求构建
		 * 时——存储的对话历史永远不会被修改。
		 */
		supportedInputModalities?: readonly string[];
	},
): AiSdkMessage[] {
	const toolCallArgKey = options?.assistantToolCallArgKey ?? "input";
	const supportedInputModalities = options?.supportedInputModalities;
	const supportsImages =
		!supportedInputModalities || supportedInputModalities.includes("image");
	const result: AiSdkMessage[] = [];
	const mediaState = createMediaBudgetState();
	const pendingAssistantMedia: Array<
		| Extract<AiSdkFormatterPart, { type: "image" }>
		| Extract<AiSdkFormatterPart, { type: "media" }>
	> = [];
	const takePendingAssistantMedia = (): AiSdkMessagePart[] => {
		const pending = pendingAssistantMedia.splice(0);
		return pending.map((part) =>
			part.type === "image"
				? supportsImages
					? toUserImagePart(part, mediaState)
					: { type: "text", text: IMAGE_UNSUPPORTED_PLACEHOLDER }
				: toUserGeneratedMediaPart(
						part.media,
						mediaState,
						supportedInputModalities,
					),
		);
	};

	if (
		(typeof systemContent === "string" && systemContent.trim().length > 0) ||
		(Array.isArray(systemContent) && systemContent.length > 0)
	) {
		result.push({
			role: "system",
			content:
				typeof systemContent === "string"
					? sanitizeSurrogates(systemContent)
					: systemContent,
		});
	}

	for (const message of messages) {
		const contentParts = message.content;

		if (typeof contentParts === "string") {
			const movedAssistantMedia =
				message.role === "user" && pendingAssistantMedia.length > 0
					? takePendingAssistantMedia()
					: [];
			if (movedAssistantMedia.length > 0) {
				result.push({
					role: message.role,
					content: [
						{
							type: "text",
							text:
								contentParts.trim().length > 0
									? sanitizeSurrogates(contentParts)
									: EMPTY_CONTENT_TEXT,
						},
						...movedAssistantMedia,
					],
				});
				continue;
			}
			if (contentParts.trim().length === 0) {
				result.push({
					role: message.role,
					content: [{ type: "text", text: EMPTY_CONTENT_TEXT }],
				});
				continue;
			}
			result.push({
				role: message.role,
				content: sanitizeSurrogates(contentParts),
			});
			continue;
		}

		const messageParts: AiSdkMessagePart[] = [];
		const toolResultParts: AiSdkMessagePart[] = [];
		if (contentParts.length === 0) {
			result.push({
				role: message.role,
				content: [{ type: "text", text: EMPTY_CONTENT_TEXT }],
			});
			continue;
		}

		for (const part of contentParts) {
			switch (part.type) {
				case "text":
					messageParts.push({
						type: "text",
						text: sanitizeSurrogates(part.text),
						...(part.providerOptions
							? { providerOptions: part.providerOptions }
							: {}),
					});
					break;
				case "reasoning":
					messageParts.push({
						type: "reasoning",
						text: sanitizeSurrogates(part.text),
						...(part.providerOptions
							? { providerOptions: part.providerOptions }
							: {}),
					});
					break;
				case "image":
					if (message.role === "assistant") {
						// AI SDK ModelMessage only accepts generated media as an
						// assistant `file` part, but common provider wire formats
						// (including Anthropic and OpenAI chat) only accept images on
						// user turns. Preserve the assistant output marker and move the
						// validated image to the following user turn so vision models
						// can reliably inspect generated images in conversation history.
						pendingAssistantMedia.push(part);
						messageParts.push({
							type: "text",
							text: GENERATED_IMAGE_TEXT,
						});
					} else {
						messageParts.push(
							supportsImages
								? toUserImagePart(part, mediaState)
								: { type: "text", text: IMAGE_UNSUPPORTED_PLACEHOLDER },
						);
					}
					break;
				case "media":
					if (message.role === "assistant") {
						pendingAssistantMedia.push(part);
						messageParts.push({
							type: "text",
							text: generatedMediaText(part.media.modality),
						});
					} else {
						messageParts.push(
							toUserGeneratedMediaPart(
								part.media,
								mediaState,
								supportedInputModalities,
							),
						);
					}
					break;
				case "file":
					messageParts.push({
						type: "text",
						text: formatFileContentBlock(
							part.path,
							sanitizeSurrogates(part.content),
						),
					});
					break;
				case "tool-call":
					if (message.role === "assistant") {
						messageParts.push({
							type: "tool-call",
							toolCallId: part.toolCallId,
							toolName: part.toolName,
							[toolCallArgKey]: part.input,
							...(part.providerOptions
								? { providerOptions: part.providerOptions }
								: {}),
						});
					}
					break;
				case "tool-result": {
					toolResultParts.push({
						type: "tool-result",
						toolCallId: part.toolCallId,
						toolName: part.toolName,
						output: toAiSdkToolResultOutput(
							part.output,
							part.isError ?? false,
							mediaState,
							{ supportsImages },
						),
					});
					break;
				}
			}
		}

		const hasToolResults = toolResultParts.length > 0;
		if (
			message.role === "user" &&
			!hasToolResults &&
			pendingAssistantMedia.length > 0
		) {
			messageParts.push(...takePendingAssistantMedia());
		}

		// A message whose parts are all empty text is effectively empty: the AI SDK
		// strips empty text parts before sending, and providers like Vercel reject
		// the resulting `content: []` ("user message must have content").
		if (
			messageParts.length > 0 &&
			messageParts.every(
				(part) =>
					part.type === "text" &&
					typeof part.text === "string" &&
					part.text.trim().length === 0,
			)
		) {
			messageParts.splice(0, messageParts.length, {
				type: "text",
				text: EMPTY_CONTENT_TEXT,
			});
		}
		if (messageParts.length > 0) {
			pushAiSdkMessage(result, { role: message.role, content: messageParts });
		}
		if (hasToolResults) {
			pushAiSdkMessage(result, { role: "tool", content: toolResultParts });
		}
	}

	if (pendingAssistantMedia.length > 0) {
		// 工具结果必须与其助手工具调用保持连续。如果
		// 没有后续的用户回合来接收生成的图像，则在完整的
		// 工具结果序列之后追加一个合成的用户回合。
		pushAiSdkMessage(result, {
			role: "user",
			content: takePendingAssistantMedia(),
		});
	}

	return result;
}
