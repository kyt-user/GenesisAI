import type { LanguageModelV4 } from "@ai-sdk/provider";
import type {
	AgentMessage,
	AgentModelEvent,
	AgentModelFinishReason,
	GatewayProviderContext,
	GatewayProviderFactory,
	GatewayResolvedProviderConfig,
	GatewayStreamRequest,
	GeneratedMedia,
	ImageMediaValidationFailure,
	ImageMediaValidationSuccess,
	MediaBudgetState,
	ModelToolExecution,
	ModelToolName,
	ProviderErrorClass,
} from "@cline/shared";
import {
	type AiSdkFormatterMessage,
	type AiSdkFormatterPart,
	captureSdkError,
	createMediaBudgetState,
	formatMessagesForAiSdk,
	GeneratedMediaSchema,
	generatedMediaModalityFromMediaType,
	modelProducesImages,
	modelSupportsToolCalling,
	parseJsonStream,
	sanitizeSurrogates,
	usesImageGenerationOperation,
	validateAndReserveBase64Media,
	validateAndReserveImageMedia,
	validateImageMedia,
} from "@cline/shared";
import {
	type CallSettings,
	generateImage,
	jsonSchema,
	NoSuchToolError,
	stepCountIs,
	streamText,
	type ToolSet,
	wrapLanguageModel,
} from "ai";
import { nanoid } from "nanoid";
import type { AiSdkTelemetryDecision } from "../services/langfuse-telemetry";
import {
	classifyProviderError,
	isRetryableBeyondSdkRetries,
} from "./error-classification";
import { extractErrorMessage } from "./format";
import { createRetryEmptyResponseMiddleware } from "./middleware/retry-empty-response";
import {
	isAnthropicCompatibleModel,
	isCerebrasProvider,
	modelSupportsImageInput,
	resolveModelFamily,
} from "./model-facts";
import {
	recordProviderRequestCapture,
	wrapFetchForProviderRequestCapture,
} from "./provider-request-capture";
import {
	applyPromptCacheToLastTextPart,
	shouldApplyPromptCache,
} from "./routing/anthropic-compatible";
import {
	applyBedrockCachePointToLastUserMessage,
	shouldApplyBedrockCachePoint,
} from "./routing/bedrock-cache-point";
import { resolvePortableReasoning } from "./routing/portable-reasoning";
import {
	type AiSdkProviderOptionsTarget,
	composeAiSdkProviderOptions,
} from "./routing/provider-options";
import type {
	AiSdkStreamPart,
	AiSdkStreamResult,
	AiSdkStreamTotalUsage,
	AiSdkStreamUsage,
	BuiltModelTools,
	ProviderFactoryResult,
	ProviderGeneratedMedia,
} from "./vendors/types";

interface GatewayNormalizedUsage {
	inputTokens: number;
	outputTokens: number;
	cacheReadTokens: number;
	cacheWriteTokens: number;
	reasoningTokenCount?: number;
	totalCost?: number;
}
type ProviderModuleKind = AiSdkProviderOptionsTarget;
type ImageGenerationInput = string | Uint8Array | ArrayBuffer;
type ImageGenerationPrompt =
	| string
	| {
			images: ImageGenerationInput[];
			text?: string;
	  };

function normalizeImageGenerationInput(
	part: Extract<AgentMessage["content"][number], { type: "image" }>,
): ImageGenerationInput {
	if (part.image instanceof URL) {
		return part.image.href;
	}
	if (typeof part.image !== "string") {
		return part.image;
	}
	if (part.image.startsWith("http://") || part.image.startsWith("https://")) {
		return part.image;
	}
	const validation = validateImageMedia(part.mediaType, part.image);
	if (!validation.ok) {
		throw new Error(validation.message);
	}
	return `data:${validation.mediaType};base64,${validation.base64}`;
}

function normalizeGeneratedImageInput(
	part: Extract<AgentMessage["content"][number], { type: "media" }>,
): ImageGenerationInput | undefined {
	if (part.media.modality !== "image") return undefined;
	switch (part.media.source.type) {
		case "url":
			return part.media.source.url;
		case "artifact":
			return undefined;
		case "base64": {
			const validation = validateImageMedia(
				part.media.mediaType,
				part.media.source.data,
			);
			return validation.ok
				? `data:${validation.mediaType};base64,${validation.base64}`
				: undefined;
		}
	}
}

function resolveImageGenerationPrompt(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): ImageGenerationPrompt {
	let latestUserMessageIndex = -1;
	for (let index = request.messages.length - 1; index >= 0; index -= 1) {
		if (request.messages[index]?.role === "user") {
			latestUserMessageIndex = index;
			break;
		}
	}
	if (latestUserMessageIndex < 0) {
		throw new Error("Image generation requires a text prompt or input image");
	}

	const message = request.messages[latestUserMessageIndex];
	if (!message || message.role !== "user") {
		throw new Error("Image generation requires a text prompt or input image");
	}
	const text = message.content
		.filter((part) => part.type === "text")
		.map((part) => part.text)
		.join("\n")
		.trim();
	const supportsImageInput =
		context.model.modalities?.input.includes("image") === true;
	if (supportsImageInput) {
		const explicitImages = message.content
			.filter((part) => part.type === "image")
			.map(normalizeImageGenerationInput);
		if (explicitImages.length > 0) {
			return {
				images: explicitImages,
				...(text ? { text } : {}),
			};
		}
	}
	if (!text) {
		throw new Error("Image generation requires a text prompt or input image");
	}
	if (!supportsImageInput) {
		return text;
	}

	// 只从紧邻的上一条 assistant 回合推断编辑。继续往前看
	// 可能悄悄把一次新的生成请求变成对会话中无关部分
	// 里一张过期图片的编辑。
	const previousMessage = request.messages[latestUserMessageIndex - 1];
	if (previousMessage?.role === "assistant") {
		const firstGeneratedImage = previousMessage.content.find(
			(part) =>
				part.type === "image" ||
				(part.type === "media" && part.media.modality === "image"),
		);
		if (firstGeneratedImage?.type === "image") {
			return {
				text,
				images: [normalizeImageGenerationInput(firstGeneratedImage)],
			};
		}
		if (firstGeneratedImage?.type === "media") {
			const input = normalizeGeneratedImageInput(firstGeneratedImage);
			if (input) return { text, images: [input] };
		}
	}
	return text;
}

type GeneratedImageExtraction =
	| { kind: "accepted"; image: ImageMediaValidationSuccess }
	| { kind: "rejected"; error: ImageMediaValidationFailure }
	| { kind: "unsupported" };

function toGeneratedImageMedia(
	image: ImageMediaValidationSuccess,
): GeneratedMedia {
	return {
		id: `media_${nanoid()}`,
		modality: "image",
		mediaType: image.mediaType,
		source: { type: "base64", data: image.base64 },
		sizeBytes: image.decodedBytes,
	};
}

function extractGeneratedImage(
	file: unknown,
	budgetState: MediaBudgetState,
): GeneratedImageExtraction {
	if (!file || typeof file !== "object") return { kind: "unsupported" };
	const record = file as Record<string, unknown>;
	if (
		typeof record.mediaType !== "string" ||
		!record.mediaType.startsWith("image/") ||
		typeof record.base64 !== "string"
	) {
		return { kind: "unsupported" };
	}
	// 生成的图片与附件和持久化历史使用同一个有界媒体包络。
	// 接受一张之后水合时会丢弃的图片，会让实时与
	// 回放的 assistant 记录不一致。
	const validation = validateAndReserveImageMedia(
		record.mediaType,
		record.base64,
		{},
		budgetState,
	);
	if (!validation.ok) {
		return { kind: "rejected", error: validation };
	}
	return { kind: "accepted", image: validation };
}

type ProjectedMediaNormalization =
	| { ok: true; media: GeneratedMedia }
	| { ok: false; error: string };

function normalizeProjectedModelToolMedia(
	candidate: ProviderGeneratedMedia,
	budgetState: MediaBudgetState,
): ProjectedMediaNormalization {
	if (candidate.modality === "image" && candidate.source.type === "base64") {
		const extracted = extractGeneratedImage(
			{
				base64: candidate.source.data,
				mediaType: candidate.mediaType,
			},
			budgetState,
		);
		if (extracted.kind === "accepted") {
			return { ok: true, media: toGeneratedImageMedia(extracted.image) };
		}
		return {
			ok: false,
			error:
				extracted.kind === "rejected"
					? extracted.error.message
					: "Model tool returned unsupported image media",
		};
	}

	let source = candidate.source;
	let sizeBytes: number | undefined;
	if (candidate.source.type === "base64") {
		const validation = validateAndReserveBase64Media(
			candidate.source.data,
			{},
			budgetState,
		);
		if (!validation.ok) {
			return { ok: false, error: validation.message };
		}
		source = { type: "base64", data: validation.base64 };
		sizeBytes = validation.decodedBytes;
	}

	const media = {
		...candidate,
		id: `media_${nanoid()}`,
		source,
		...(sizeBytes !== undefined ? { sizeBytes } : {}),
	};
	const parsed = GeneratedMediaSchema.safeParse(media);
	return parsed.success
		? { ok: true, media: parsed.data }
		: { ok: false, error: "Model tool returned invalid generated media" };
}

interface ActiveProjectedModelToolCall {
	toolName: ModelToolName;
	input?: unknown;
	execution: ModelToolExecution;
}

interface ProjectedModelToolResult {
	media: GeneratedMedia[];
	activityOutput: unknown;
}

function summarizeProjectedMedia(media: readonly GeneratedMedia[]): unknown {
	return {
		generatedMediaCount: media.length,
		mediaTypes: media.map((item) => item.mediaType),
		byteLength: media.reduce((total, item) => total + (item.sizeBytes ?? 0), 0),
	};
}

export function buildAiSdkStreamConfig(
	request: GatewayStreamRequest,
	_context: GatewayProviderContext,
): Partial<CallSettings> {
	const reasoning = resolvePortableReasoning(request);
	return {
		...(request.maxTokens !== undefined
			? { maxOutputTokens: request.maxTokens }
			: {}),
		temperature: request.temperature,
		...(reasoning ? { reasoning } : {}),
	};
}

function buildProviderModelTools(
	provider: ProviderFactoryResult,
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): BuiltModelTools | undefined {
	if (!request.modelTools?.length) {
		return undefined;
	}

	const requestedNames = [
		...new Set(request.modelTools.map((tool) => tool.name)),
	];
	if (!provider.buildModelTools) {
		throw new Error(
			`Provider adapter for "${context.provider.id}" does not implement requested model tool(s): ${requestedNames.join(", ")}.`,
		);
	}

	const modelTools = provider.buildModelTools(request.modelTools);
	const missingNames = requestedNames.filter(
		(toolName) => !Object.hasOwn(modelTools, toolName),
	);
	if (missingNames.length > 0) {
		throw new Error(
			`Provider adapter for "${context.provider.id}" did not build requested model tool(s): ${missingNames.join(", ")}.`,
		);
	}

	return modelTools;
}

function toAiSdkModelToolSet(
	modelTools: BuiltModelTools | undefined,
): ToolSet | undefined {
	if (!modelTools) return undefined;
	const entries = Object.entries(modelTools).flatMap(([name, adapter]) =>
		adapter ? [[name, adapter.tool] as const] : [],
	);
	return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

function buildAiSdkRequestMessages(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
	systemPrompt?: string,
) {
	const aiMessages = toAiSdkMessages(request.messages, systemPrompt, {
		includeReasoning: shouldIncludeReasoningHistory(request, context),
		supportedInputModalities:
			context.model.modalities?.input ??
			(context.model.capabilities
				? modelSupportsImageInput(context)
					? ["text", "image"]
					: ["text"]
				: undefined),
	}) as Array<Record<string, unknown>>;

	if (shouldApplyBedrockCachePoint(request, context)) {
		applyBedrockCachePointToLastUserMessage(aiMessages);
		return aiMessages;
	}

	if (!shouldApplyPromptCache(request, context)) {
		return aiMessages;
	}

	const includeAnthropic = isAnthropicCompatibleModel({
		modelId: request.modelId,
		family: resolveModelFamily(context),
	});

	for (let i = aiMessages.length - 1; i >= 0; i--) {
		if (aiMessages[i]?.role === "user") {
			applyPromptCacheToLastTextPart(
				aiMessages[i],
				request.providerId,
				includeAnthropic,
			);
			break;
		}
	}

	return aiMessages;
}

function resolveStickySession(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
):
	| {
			transport: "json-body" | "header";
			field: string;
			value: string;
	  }
	| undefined {
	const stickySession = context.provider.metadata?.stickySession;
	if (!stickySession) {
		return undefined;
	}
	const metadata = request.metadata;
	const value =
		metadata && typeof metadata === "object"
			? metadata[stickySession.metadataKey]
			: undefined;
	if (typeof value !== "string") {
		return undefined;
	}
	const trimmed = value.trim();
	if (!trimmed) {
		return undefined;
	}
	return {
		transport: stickySession.transport,
		field: stickySession.field,
		value: trimmed,
	};
}

type FetchBodyText =
	| { source: "init-body"; text: string }
	| { request: Request; source: "request"; text: string };

async function bodyTextFromFetchInput(
	input: Parameters<typeof fetch>[0],
	init: Parameters<typeof fetch>[1],
): Promise<FetchBodyText | undefined> {
	const body = init?.body;
	if (body === null) {
		return undefined;
	}
	if (typeof body === "string") {
		return { source: "init-body", text: body };
	}
	if (body instanceof URLSearchParams) {
		return { source: "init-body", text: body.toString() };
	}
	if (body instanceof ArrayBuffer) {
		return { source: "init-body", text: Buffer.from(body).toString("utf8") };
	}
	if (ArrayBuffer.isView(body)) {
		return {
			source: "init-body",
			text: Buffer.from(body.buffer, body.byteOffset, body.byteLength).toString(
				"utf8",
			),
		};
	}
	if (body !== undefined) {
		return undefined;
	}
	if (input instanceof Request) {
		try {
			return {
				request: input,
				source: "request",
				text: await input.clone().text(),
			};
		} catch {
			return undefined;
		}
	}
	return undefined;
}

async function injectJsonBodyStickySession(
	input: Parameters<typeof fetch>[0],
	init: Parameters<typeof fetch>[1],
	stickySession: { field: string; value: string },
): Promise<Parameters<typeof fetch>> {
	const bodyText = await bodyTextFromFetchInput(input, init);
	if (!bodyText?.text.trim().startsWith("{")) {
		return [input, init];
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(bodyText.text);
	} catch {
		return [input, init];
	}
	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		return [input, init];
	}
	const body = parsed as Record<string, unknown>;
	const existingValue = body[stickySession.field];
	if (typeof existingValue !== "string" || !existingValue.trim()) {
		body[stickySession.field] = stickySession.value;
	}
	const nextBody = JSON.stringify(body);
	if (bodyText.source === "init-body") {
		return [input, { ...init, body: nextBody }];
	}
	return [new Request(bodyText.request, { body: nextBody }), init];
}

function injectHeaderStickySession(
	input: Parameters<typeof fetch>[0],
	init: Parameters<typeof fetch>[1],
	stickySession: { field: string; value: string },
): Parameters<typeof fetch> {
	const headers = new Headers(
		input instanceof Request ? input.headers : undefined,
	);
	new Headers(init?.headers).forEach((value, key) => {
		headers.set(key, value);
	});
	if (!headers.get(stickySession.field)?.trim()) {
		headers.set(stickySession.field, stickySession.value);
	}
	return [input, { ...init, headers }];
}

function wrapFetchForStickySession(
	baseFetch: typeof fetch | undefined,
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): typeof fetch | undefined {
	const stickySession = resolveStickySession(request, context);
	if (!stickySession) {
		return baseFetch;
	}
	const delegate = baseFetch ?? globalThis.fetch;
	if (!delegate) {
		return baseFetch;
	}
	const sessionFetch = (async (input, init) => {
		const [nextInput, nextInit] =
			stickySession.transport === "json-body"
				? await injectJsonBodyStickySession(input, init, stickySession)
				: injectHeaderStickySession(input, init, stickySession);
		return delegate(nextInput, nextInit);
	}) as typeof fetch;
	const delegateWithPreconnect = delegate as typeof fetch & {
		preconnect?: (...args: unknown[]) => unknown;
	};
	if (typeof delegateWithPreconnect.preconnect === "function") {
		(
			sessionFetch as typeof fetch & {
				preconnect?: (...args: unknown[]) => unknown;
			}
		).preconnect = delegateWithPreconnect.preconnect.bind(delegate);
	}
	return sessionFetch;
}

function shouldIncludeReasoningHistory(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): boolean {
	return !isCerebrasProvider(request, context);
}

async function resolveGatewayAiSdkTelemetry(
	providerId: string,
	request: GatewayStreamRequest,
): Promise<AiSdkTelemetryDecision> {
	try {
		const runtime = await import("../services/langfuse-telemetry");
		return await runtime.resolveAiSdkTelemetry(
			providerId,
			resolveTraceSamplingKey(request),
		);
	} catch {
		return { isEnabled: false };
	}
}

/**
 * 整任务采样键：优先使用会话/任务 id，使一个任务中的每个请求
 * 都得到相同的采样决策，追踪保持完整。
 */
function resolveTraceSamplingKey(
	request: GatewayStreamRequest,
): string | undefined {
	const metadata =
		request.metadata && typeof request.metadata === "object"
			? (request.metadata as Record<string, unknown>)
			: {};
	for (const key of ["sessionId", "conversationId", "distinctId"]) {
		const value = metadata[key];
		if (typeof value === "string" && value.trim().length > 0) {
			return value;
		}
	}
	return undefined;
}

async function withAiSdkLangfuseTraceContext<T>(
	enabled: boolean,
	request: GatewayStreamRequest,
	callback: () => T | Promise<T>,
): Promise<T> {
	const metadata =
		request.metadata && typeof request.metadata === "object"
			? request.metadata
			: {};
	const tags = Array.isArray(metadata.tags)
		? metadata.tags.filter(
				(value): value is string =>
					typeof value === "string" && value.trim().length > 0,
			)
		: undefined;
	const distinctId =
		typeof metadata.distinctId === "string" ? metadata.distinctId : undefined;
	const sessionId =
		typeof metadata.sessionId === "string" ? metadata.sessionId : undefined;

	if (!enabled || (!distinctId && !sessionId && !tags?.length)) {
		return await callback();
	}

	const runtime = await import("../services/langfuse-telemetry");
	return await runtime.withLangfuseTraceAttributes(
		true,
		{
			...(distinctId ? { userId: distinctId } : {}),
			...(sessionId ? { sessionId } : {}),
			...(tags?.length ? { tags } : {}),
			metadata: {
				...(typeof metadata.conversationId === "string"
					? { conversationId: metadata.conversationId }
					: {}),
				...(typeof metadata.runId === "string"
					? { runId: metadata.runId }
					: {}),
			},
		},
		callback,
	);
}

function buildAiSdkRuntimeContext(
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
): Record<string, unknown> {
	const requestMetadata = request.metadata;
	const metadata =
		requestMetadata && typeof requestMetadata === "object"
			? requestMetadata
			: {};
	const tags = Array.isArray(metadata.tags)
		? metadata.tags.filter(
				(value): value is string =>
					typeof value === "string" && value.trim().length > 0,
			)
		: undefined;
	const distinctId =
		typeof metadata.distinctId === "string" ? metadata.distinctId : undefined;

	return {
		// `distinctId` 是 Cline 的规范身份字段。Langfuse 的数据
		// 模型把同一个值称为 `userId`，因此在运行时上下文中
		// 两者都暴露，并在下面把 distinctId 显式映射到 Langfuse 的 userId。
		...(distinctId ? { distinctId, userId: distinctId } : {}),
		...(typeof metadata.sessionId === "string"
			? { sessionId: metadata.sessionId }
			: {}),
		...(typeof metadata.clientName === "string"
			? { clientName: metadata.clientName }
			: {}),
		...(typeof metadata.clientVersion === "string"
			? { clientVersion: metadata.clientVersion }
			: {}),
		...(typeof metadata.clineCoreVersion === "string"
			? { clineCoreVersion: metadata.clineCoreVersion }
			: {}),
		...(tags && tags.length > 0 ? { tags } : {}),
		// 即使集成未把它们提升为一等 Langfuse 字段，也保持
		// Cline 关联字段可用。
		...(typeof metadata.conversationId === "string"
			? { conversationId: metadata.conversationId }
			: {}),
		...(typeof metadata.runId === "string" ? { runId: metadata.runId } : {}),
		...(typeof metadata.iteration === "number"
			? { iteration: metadata.iteration }
			: {}),
		providerId: request.providerId,
		modelId: request.modelId,
		resolvedModelId: context.model.id,
	};
}

function toAiSdkMessages(
	messages: readonly AgentMessage[],
	systemPrompt?: string,
	options?: {
		includeReasoning?: boolean;
		supportedInputModalities?: readonly string[];
	},
) {
	const includeReasoning = options?.includeReasoning ?? true;
	const normalizedMessages: AiSdkFormatterMessage[] = [];

	for (const message of messages) {
		const content: AiSdkFormatterPart[] = [];
		let skippedReasoning = false;
		for (const part of message.content) {
			if (part.type === "text") {
				content.push({ type: "text", text: sanitizeSurrogates(part.text) });
				continue;
			}

			if (part.type === "reasoning") {
				if (!includeReasoning) {
					skippedReasoning = true;
					continue;
				}
				const metadata = part.metadata as Record<string, unknown> | undefined;
				const signature = metadata?.signature;
				const redactedData = metadata?.redactedData;
				content.push({
					type: "reasoning",
					text: sanitizeSurrogates(part.text),
					...(typeof signature === "string" || typeof redactedData === "string"
						? {
								providerOptions: {
									anthropic: {
										...(typeof signature === "string" ? { signature } : {}),
										...(typeof redactedData === "string"
											? { redactedData }
											: {}),
									},
								},
							}
						: {}),
				});
				continue;
			}

			if (part.type === "file") {
				content.push({
					type: "file",
					path: part.path,
					content: part.content,
				});
				continue;
			}

			if (part.type === "image") {
				content.push({
					type: "image",
					image: part.image,
					mediaType: part.mediaType,
				});
				continue;
			}

			if (part.type === "media") {
				content.push({ type: "media", media: part.media });
				continue;
			}

			if (part.type === "tool-call") {
				const metadata = part.metadata as Record<string, unknown> | undefined;
				const thoughtSignature =
					metadata?.thoughtSignature ??
					metadata?.signature ??
					metadata?.thought_signature;
				content.push({
					type: "tool-call",
					toolCallId: part.toolCallId,
					toolName: part.toolName,
					input: part.input,
					...(typeof thoughtSignature === "string"
						? {
								providerOptions: {
									google: { thoughtSignature },
								},
							}
						: {}),
				});
				continue;
			}

			if (part.type === "tool-result") {
				content.push({
					type: "tool-result",
					toolCallId: part.toolCallId,
					toolName: part.toolName,
					output: part.output,
					isError: part.isError ?? false,
				});
			}
		}

		// 只因推理被丢弃而变空的消息会整体省略，
		// 而不是作为空回合转发。
		const emptiedByDroppedReasoning = !includeReasoning && skippedReasoning;
		if (content.length > 0) {
			normalizedMessages.push({ role: message.role, content });
		} else if (
			!emptiedByDroppedReasoning &&
			(message.role === "user" || message.role === "assistant")
		) {
			normalizedMessages.push({ role: message.role, content: "" });
		}
	}

	return formatMessagesForAiSdk(systemPrompt, normalizedMessages, {
		assistantToolCallArgKey: "input",
		supportedInputModalities: options?.supportedInputModalities,
	});
}

function toAiSdkTools(request: GatewayStreamRequest): ToolSet | undefined {
	if (!request.tools?.length) {
		return undefined;
	}

	// 有意不设 validate 回调：模式校验属于工具本身
	//（core 执行器用宽松的联合模式校验，能接受弱模型
	// 常见的形态，例如对 string[] 属性传裸字符串）。
	// 在这里拒绝会把错误返回给模型，而工具自身的
	// 输入处理根本看不到这次调用。
	const tools: ToolSet = {};
	for (const definition of request.tools) {
		tools[definition.name] = {
			description: definition.description,
			inputSchema: jsonSchema(
				normalizeAiSdkToolInputSchema(definition.inputSchema),
			),
		};
	}
	return tools;
}

function mergeAiSdkTools(
	runtimeTools: ToolSet | undefined,
	providerTools: ToolSet | undefined,
): ToolSet | undefined {
	// 运行时工具携带调用方的执行器契约，因此当 provider
	// 恰好注册了相同的公开名称时，它们保留所有权。
	const tools = {
		...(providerTools ?? {}),
		...(runtimeTools ?? {}),
	};
	return Object.keys(tools).length > 0 ? tools : undefined;
}

function hasAiSdkTool(tools: ToolSet | undefined, toolName: string): boolean {
	return tools !== undefined && Object.hasOwn(tools, toolName);
}

interface RepairableToolCall {
	toolCallId: string;
	toolName: string;
	input: string;
}

/**
 * 对参数不是有效 JSON 的工具调用做最后补救
 *（载荷被截断、单引号、未转义换行——在弱模型上很常见）。
 * 将原始参数字符串交给共享的 jsonrepair 策略处理；
 * 未知工具名和已经有效的 JSON 在此不可修复，
 * 返回 null 保留 AI SDK 原有的错误行为。
 */
export async function repairMalformedToolCall<T extends RepairableToolCall>({
	toolCall,
	error,
}: {
	toolCall: T;
	error: unknown;
}): Promise<T | null> {
	if (NoSuchToolError.isInstance(error)) {
		return null;
	}
	if (typeof toolCall.input !== "string" || toolCall.input.trim() === "") {
		return null;
	}
	try {
		JSON.parse(toolCall.input);
		// 有效 JSON 说明失败是模式不匹配而非解析
		// 错误。这留给工具执行器自己的宽松联合
		// 模式处理；此处没有什么可修复的。
		return null;
	} catch {
		// 不是有效 JSON——尝试在下面修复。
	}
	const repaired = parseJsonStream(toolCall.input);
	if (repaired === toolCall.input || typeof repaired === "string") {
		return null;
	}
	return { ...toolCall, input: JSON.stringify(repaired) };
}

function normalizeAiSdkToolInputSchema(
	inputSchema: Record<string, unknown>,
): Record<string, unknown> {
	if (inputSchema.type === "object") {
		return inputSchema;
	}

	return {
		type: "object",
		...inputSchema,
	};
}

function providerDisablesExternalToolExecution(
	context: GatewayProviderContext,
): boolean {
	return context.provider.capabilities?.includes("provider-tools") ?? false;
}

function mergeToolCallMetadata(
	current: unknown,
	patch: Record<string, unknown>,
): Record<string, unknown> {
	if (!current || typeof current !== "object" || Array.isArray(current)) {
		return patch;
	}
	return {
		...(current as Record<string, unknown>),
		...patch,
	};
}

function buildToolCallMetadata(input: {
	metadata: unknown;
	request: GatewayStreamRequest;
	context: GatewayProviderContext;
}): Record<string, unknown> {
	return mergeToolCallMetadata(input.metadata, {
		toolSource: {
			providerId: input.request.providerId,
			modelId: input.request.modelId,
			executionMode: providerDisablesExternalToolExecution(input.context)
				? "provider"
				: "runtime",
		},
	});
}

function buildRecoverableToolErrorMetadata(input: {
	part: AiSdkStreamPart;
	errorMessage: string;
	request: GatewayStreamRequest;
	context: GatewayProviderContext;
	toolName: string;
}): Record<string, unknown> {
	return buildToolCallMetadata({
		metadata: mergeToolCallMetadata(extractGoogleThoughtMetadata(input.part), {
			inputParseError: `Tool call ${input.toolName} was rejected before execution: ${input.errorMessage}`,
			aiSdkToolError: input.errorMessage,
		}),
		request: input.request,
		context: input.context,
	});
}

function resolveAiSdkSystemPrompt(
	request: GatewayStreamRequest,
): string | undefined {
	return request.providerId === "openai-codex"
		? undefined
		: request.systemPrompt;
}

function mapFinishReason(
	value: unknown,
	sawToolCalls: boolean,
): AgentModelFinishReason {
	if (value === "tool-calls" || value === "tool_calls" || sawToolCalls) {
		return "tool-calls";
	}
	if (value === "length" || value === "max_tokens") {
		return "max-tokens";
	}
	if (value === "error") {
		return "error";
	}
	return "stop";
}

function getUsageValue(
	usage: Record<string, unknown>,
	...keys: string[]
): number {
	for (const key of keys) {
		const value = usage[key];
		if (typeof value === "number" && Number.isFinite(value)) {
			return value;
		}
		if (
			typeof value === "string" &&
			value.trim().length > 0 &&
			Number.isFinite(Number(value))
		) {
			return Number(value);
		}
	}
	return 0;
}

function getNumericValue(value: unknown): number | undefined {
	if (typeof value === "number" && Number.isFinite(value)) {
		return value;
	}
	if (
		typeof value === "string" &&
		value.trim().length > 0 &&
		Number.isFinite(Number(value))
	) {
		return Number(value);
	}
	return undefined;
}

function getNestedUsageValue(
	usage: Record<string, unknown>,
	...path: string[]
): number {
	let current: unknown = usage;
	for (const key of path) {
		if (!current || typeof current !== "object") {
			return 0;
		}
		current = (current as Record<string, unknown>)[key];
	}
	return getNumericValue(current) ?? 0;
}

/**
 * 每次模型调用的 AI SDK 请求级重试次数（SDK 默认为 2）。SDK 在
 * 瞬时失败——429/5xx/网络——时重试*初始*请求，采用尊重
 * `retry-after` 头的指数退避。它永远看不到 provider 在
 * *流中*发出的错误（OpenRouter 的 "Provider returned error"
 * 在 200 之后作为流分片到达），因此 agent 循环为这类错误
 * 保留自己的回合级重试。
 *
 * 每个失败类别恰好只有一层重试，计数永远不会
 * 相乘：请求启动失败属于此项设置（`RetryError` 对
 * 回合级重试是终止性的，见 `isRetryableBeyondSdkRetries`）；
 * 输出前的套接字死亡和空响应属于
 * `withEmptyResponseRetry`，它永远看不到请求启动阶段的拒绝；
 * 流中 provider 错误则只属于回合级重试。
 */
const MODEL_REQUEST_MAX_RETRIES = 5;

type UsagePath = readonly [string] | readonly [string, string];

const REASONING_TOKEN_PATHS: UsagePath[] = [
	["outputTokenDetails", "reasoningTokens"],
	["output_tokens_details", "reasoning_tokens"],
	["completion_tokens_details", "reasoning_tokens"],
	// AI SDK v4 的嵌套 outputTokens 结构（{ total, text, reasoning, ... }）。
	["outputTokens", "reasoning"],
	["reasoningTokens"],
	["reasoning_tokens"],
];

function getUsageValueByPath(source: unknown, path: UsagePath): number {
	let current: unknown = source;
	for (const key of path) {
		if (!current || typeof current !== "object") {
			return 0;
		}
		current = (current as Record<string, unknown>)[key];
	}
	return getNumericValue(current) ?? 0;
}

function firstUsageValue(sources: unknown[], paths: UsagePath[]): number {
	for (const source of sources) {
		for (const path of paths) {
			const value = getUsageValueByPath(source, path);
			if (value > 0) {
				return value;
			}
		}
	}
	return 0;
}

function extractProviderNestedUsage(
	value: unknown,
): Record<string, unknown> | undefined {
	if (!value || typeof value !== "object") {
		return undefined;
	}

	const providerMetadata = value as Record<string, unknown>;
	for (const nestedValue of Object.values(providerMetadata)) {
		if (!nestedValue || typeof nestedValue !== "object") {
			continue;
		}

		const nestedMetadata = nestedValue as Record<string, unknown>;
		if (nestedMetadata.usage && typeof nestedMetadata.usage === "object") {
			return nestedMetadata.usage as Record<string, unknown>;
		}
	}

	return undefined;
}

function calculateUsageCostFromPricing(
	usage: Omit<GatewayNormalizedUsage, "totalCost">,
	pricingValue: unknown,
): number | undefined {
	if (!pricingValue || typeof pricingValue !== "object") {
		return undefined;
	}

	const pricing = pricingValue as Record<string, unknown>;
	const inputPrice = getNumericValue(pricing.input);
	const outputPrice = getNumericValue(pricing.output);

	if (inputPrice === undefined || outputPrice === undefined) {
		return undefined;
	}

	const cacheReadPrice = getNumericValue(pricing.cacheRead) ?? 0;
	const cacheWritePrice =
		getNumericValue(pricing.cacheWrite) ?? inputPrice * 1.25;
	const billableInputTokens = Math.max(
		0,
		usage.inputTokens - usage.cacheReadTokens - usage.cacheWriteTokens,
	);

	return (
		(billableInputTokens / 1_000_000) * inputPrice +
		(usage.outputTokens / 1_000_000) * outputPrice +
		(usage.cacheReadTokens / 1_000_000) * cacheReadPrice +
		(usage.cacheWriteTokens / 1_000_000) * cacheWritePrice
	);
}

/**
 * 将各种 provider 格式的用量规范化为标准结构。
 * 同时接受 AI SDK 的规范化形态（AiSdkStreamTotalUsage、AiSdkStreamUsage）
 * 和原始 provider 响应。处理多种命名约定（camelCase 与 snake_case），
 * 从 provider 特有字段提取成本，并回退到基于定价的计算。
 * provider 报告的实际计费成本优先于市场成本，使网关折扣
 * 反映在用户可见的合计中。无计费成本时市场成本作为回退。
 *
 * @param usageValue - AI SDK 规范化用量或原始 provider 响应对象
 * @param providerMetadata - 用于成本提取的 provider 特有元数据
 * @param pricingValue - 未找到显式成本时的回退定价配置（每 1M token）
 */
export function normalizeUsage(
	usageValue:
		| AiSdkStreamUsage
		| AiSdkStreamTotalUsage
		| Record<string, unknown>
		| undefined,
	providerMetadata?: unknown,
	pricingValue?: unknown,
	selection?: Pick<GatewayStreamRequest, "providerId" | "modelId">,
): GatewayNormalizedUsage {
	const usage =
		usageValue && typeof usageValue === "object"
			? (usageValue as Record<string, unknown>)
			: {};
	const providerUsage = extractProviderNestedUsage(providerMetadata);
	const providerMetadataRecord =
		providerMetadata && typeof providerMetadata === "object"
			? (providerMetadata as Record<string, unknown>)
			: {};
	const gatewayMetadata =
		providerMetadataRecord.gateway &&
		typeof providerMetadataRecord.gateway === "object"
			? (providerMetadataRecord.gateway as Record<string, unknown>)
			: {};
	const rawUsage =
		usage.raw && typeof usage.raw === "object"
			? (usage.raw as Record<string, unknown>)
			: usage;
	const upstreamInferenceCost =
		getNumericValue(
			(rawUsage.cost_details as Record<string, unknown> | undefined)
				?.upstream_inference_cost,
		) ?? getNumericValue(rawUsage.upstream_inference_cost);
	const marketCost =
		getNumericValue(rawUsage.market_cost) ??
		getNumericValue(rawUsage.marketCost) ??
		getNumericValue(gatewayMetadata.marketCost);
	const baseCost =
		getNumericValue(rawUsage.cost) ?? getNumericValue(gatewayMetadata.cost);
	const hasExplicitCost =
		marketCost !== undefined ||
		baseCost !== undefined ||
		upstreamInferenceCost !== undefined;
	const isByokUsage =
		rawUsage.is_byok === true ||
		rawUsage.isByok === true ||
		gatewayMetadata.is_byok === true ||
		gatewayMetadata.isByok === true;
	const shouldAddUpstreamCost =
		isByokUsage &&
		baseCost !== undefined &&
		upstreamInferenceCost !== undefined;
	const costOrUpstream =
		baseCost !== undefined && baseCost > 0
			? baseCost
			: (upstreamInferenceCost ?? baseCost);
	const billedCost = shouldAddUpstreamCost
		? baseCost + upstreamInferenceCost
		: costOrUpstream;
	const totalCost =
		billedCost !== undefined && billedCost !== 0
			? billedCost
			: (marketCost ?? billedCost);
	const normalizedUsage = {
		inputTokens:
			getNestedUsageValue(usage, "inputTokens", "total") ||
			getUsageValue(usage, "inputTokens", "input_tokens", "prompt_tokens") ||
			getUsageValue(rawUsage, "promptTokenCount", "prompt_token_count"),
		outputTokens:
			getNestedUsageValue(usage, "outputTokens", "total") ||
			getUsageValue(
				usage,
				"outputTokens",
				"output_tokens",
				"completion_tokens",
			) ||
			getUsageValue(rawUsage, "candidatesTokenCount", "candidates_token_count"),
		cacheReadTokens:
			getNestedUsageValue(usage, "inputTokens", "cacheRead") ||
			getNestedUsageValue(usage, "inputTokenDetails", "cacheReadTokens") ||
			getUsageValue(
				usage,
				"cachedInputTokens",
				"cacheReadTokens",
				"cache_read_tokens",
				"cache_read_input_tokens",
			) ||
			getNestedUsageValue(usage, "prompt_tokens_details", "cached_tokens") ||
			getNestedUsageValue(rawUsage, "prompt_tokens_details", "cached_tokens") ||
			getUsageValue(rawUsage, "cachedContentTokenCount") ||
			getUsageValue(
				providerUsage ?? {},
				"cachedInputTokens",
				"cacheReadTokens",
				"cache_read_tokens",
				"cache_read_input_tokens",
			),
		cacheWriteTokens:
			getNestedUsageValue(usage, "inputTokens", "cacheWrite") ||
			getNestedUsageValue(usage, "inputTokenDetails", "cacheWriteTokens") ||
			getNestedUsageValue(
				usage,
				"prompt_tokens_details",
				"cache_write_tokens",
			) ||
			getUsageValue(
				usage,
				"cacheWriteTokens",
				"cache_write_tokens",
				"cache_creation_input_tokens",
			) ||
			getNestedUsageValue(
				rawUsage,
				"prompt_tokens_details",
				"cache_write_tokens",
			) ||
			getUsageValue(
				rawUsage,
				"cacheWriteTokens",
				"cache_write_tokens",
				"cache_creation_input_tokens",
			) ||
			getUsageValue(
				providerUsage ?? {},
				"cacheWriteTokens",
				"cache_write_tokens",
				"cache_creation_input_tokens",
			),
	};
	const reasoningTokenCount = firstUsageValue(
		[usage, rawUsage, providerUsage ?? {}],
		REASONING_TOKEN_PATHS,
	);
	const pricing = pricingValue as Record<string, unknown> | undefined;
	// Cline 的包含型模型没有按请求收费，即使响应中
	// 带有上游推理或市场成本。
	const includedClineUsage =
		selection?.providerId === "cline-pass" ||
		(selection?.providerId === "cline" &&
			(selection.modelId.startsWith("cline-pass/") ||
				selection.modelId.startsWith("cline-free/") ||
				selection.modelId.endsWith(":free") ||
				(pricing?.input === 0 &&
					pricing?.output === 0 &&
					(pricing.cacheRead ?? 0) === 0 &&
					(pricing.cacheWrite ?? 0) === 0)));
	const resolvedTotalCost = includedClineUsage
		? 0
		: totalCost !== undefined
			? totalCost
			: hasExplicitCost
				? undefined
				: calculateUsageCostFromPricing(normalizedUsage, pricingValue);

	return {
		...normalizedUsage,
		// provider 报告的推理 token 是 outputTokens 的子集（例如
		// OpenAI 的 completion_tokens_details.reasoning_tokens），而非额外
		// 叠加。这里把它们剥离出来，使 outputTokens 反映实际的
		// 非推理输出，而 reasoningTokenCount 单独跟踪——
		// 否则每个下游消费者（会话合计、遥测、
		// Harbor 的 n_output_tokens）会把推理同时计入
		// 自身计数和 "output" 的一部分，造成双重记账。
		// 上面的成本基于减法前的 outputTokens 计算，
		// 因为推理 token 仍按输出费率计费。
		outputTokens: Math.max(0, normalizedUsage.outputTokens - reasoningTokenCount),
		...(reasoningTokenCount > 0 ? { reasoningTokenCount } : {}),
		...(typeof resolvedTotalCost === "number"
			? { totalCost: resolvedTotalCost }
			: {}),
	};
}

/**
 * 抑制 AI SDK 流 promise（usage、finishReason 等）的未处理拒绝——
 * 当流遇到错误时它们会以 NoOutputGeneratedError 拒绝。
 *
 * AI SDK 的 streamText 结果暴露惰性 promise getter（finishReason、totalUsage、
 * steps、text、usage 等），由内部 DelayedPromise 实例支撑。当流
 * 以 0 个已记录步骤出错时，flush 回调会拒绝所有这些 promise。我们必须在
 * Bun/Node 将其暴露为未处理拒绝之前访问每个 getter 获取 promise
 * 并附加空操作的拒绝处理器。
 */
function suppressDanglingStreamPromises(
	stream: AiSdkStreamResult | undefined,
): void {
	if (!stream) return;
	const noop = () => {};
	const suppress = (val: unknown) => {
		if (val && typeof (val as Promise<unknown>).catch === "function") {
			(val as Promise<unknown>).catch(noop);
		}
	};

	// 访问 AI SDK StreamTextResult 对象上已知的惰性 promise getter。
	const s = stream as Record<string, unknown>;

	// 兜底处理其余任何值为 promise 的自有属性。
	for (const key of Object.keys(stream)) {
		try {
			suppress(s[key]);
		} catch {
			// 忽略
		}
	}
}

function extractGoogleThoughtMetadata(
	part: AiSdkStreamPart,
): Record<string, unknown> | undefined {
	const metadata: Record<string, unknown> = {};

	if (typeof part.thoughtSignature === "string") {
		metadata.thoughtSignature = part.thoughtSignature;
	}
	if (typeof part.thought_signature === "string") {
		metadata.thought_signature = part.thought_signature;
	}

	const providerMetadata =
		part.providerMetadata && typeof part.providerMetadata === "object"
			? (part.providerMetadata as Record<string, unknown>)
			: undefined;
	const googleMetadata =
		providerMetadata?.google && typeof providerMetadata.google === "object"
			? (providerMetadata.google as Record<string, unknown>)
			: undefined;
	const vertexMetadata =
		providerMetadata?.vertex && typeof providerMetadata.vertex === "object"
			? (providerMetadata.vertex as Record<string, unknown>)
			: undefined;

	if (
		typeof metadata.thoughtSignature !== "string" &&
		typeof (
			googleMetadata?.thoughtSignature ?? vertexMetadata?.thoughtSignature
		) === "string"
	) {
		metadata.thoughtSignature =
			googleMetadata?.thoughtSignature ?? vertexMetadata?.thoughtSignature;
	}
	if (
		typeof metadata.thought_signature !== "string" &&
		typeof googleMetadata?.thought_signature === "string"
	) {
		metadata.thought_signature = googleMetadata.thought_signature;
	}

	return Object.keys(metadata).length > 0 ? metadata : undefined;
}

/**
 * 在原始 provider 错误对象仍在手中时捕获的流错误：
 * 展平的展示消息加上其分类。两者都在这里派生，
 * 因为分类所需的结构无法在 `extractErrorMessage`
 * 之后存活。
 */
interface CapturedStreamError {
	message: string;
	errorClass: ProviderErrorClass;
	/**
	 * agent 循环的回合级重试是否可以重跑本回合——在结构化错误
	 * 仍在手中时判定，并作为 `errorRetryable` 在 `finish` 事件上
	 * 转发（agent 循环收到的展平消息无法携带它）。按 AI SDK
	 * 自带的类型化 `isRetryable` 标志判断瞬时性，但 `RetryError`
	 * 是终止性的：SDK 已经用掉了请求启动阶段的重试，
	 * 回合级重试不能再叠加。
	 */
	retryable: boolean;
	/**
	 * 本层已为该失败记录过 `sdk.error` 遥测。
	 * 作为 `errorReported` 在 `finish` 事件上转发，使 agent 循环
	 * 不会第二次上报同一失败。
	 */
	reported?: boolean;
}

function captureStreamError(error: unknown): CapturedStreamError {
	return {
		message: extractErrorMessage(error),
		errorClass: classifyProviderError(error),
		retryable: isRetryableBeyondSdkRetries(error),
	};
}

async function* emitAiSdkEvents(
	stream: AiSdkStreamResult,
	request: GatewayStreamRequest,
	context: GatewayProviderContext,
	pricingValue?: unknown,
	capturedError?: { current: CapturedStreamError | undefined },
	modelToolAdapters?: BuiltModelTools,
): AsyncIterable<AgentModelEvent> {
	let sawToolCalls = false;
	const emittedToolCallIds = new Set<string>();
	let finishReason: unknown;
	let requestId: string | undefined;
	let streamError: CapturedStreamError | undefined;
	let finishUsage: unknown;
	let finishProviderMetadata: unknown;
	let streamAborted = false;
	let sawVisibleContent = false;
	const mediaBudget = createMediaBudgetState();
	const rejectedMediaErrors: string[] = [];
	const activeProjectedModelToolCalls = new Map<
		string,
		ActiveProjectedModelToolCall
	>();
	const projectedModelToolResults = new Map<string, ProjectedModelToolResult>();
	const pendingProjectedModelToolOutputs = new Map<string, unknown>();
	const projectedModelToolErrors = new Map<string, string>();
	// provider 在本次推理请求内执行的工具调用（例如
	// Claude Code CLI 自带的工具）。它们作为观察性活动呈现，
	// 绝不能进入 AgentRuntime 的本地执行/审批循环。结果和
	// 错误分片按 ID 匹配，因为有些 provider 在配对的结果
	// 一半上省略 providerExecuted 标志。
	const observationalProviderToolCallIds = new Set<string>();

	try {
		if (stream.fullStream) {
			for await (const part of stream.fullStream) {
				if (part.type === "start-step") {
					requestId = undefined;
					continue;
				}
				if (part.type === "finish-step") {
					requestId = Object.entries(part.response?.headers ?? {}).find(
						([name]) => name.toLowerCase() === "x-request-id",
					)?.[1];
					continue;
				}
				if (part.type === "text-delta") {
					const text =
						(part.textDelta as string | undefined) ??
						(part.text as string | undefined) ??
						(part.delta as string | undefined);
					if (text) {
						sawVisibleContent = true;
						yield { type: "text-delta", text };
					}
					continue;
				}

				if (part.type === "reasoning-delta" || part.type === "reasoning") {
					const text =
						(part.textDelta as string | undefined) ??
						(part.text as string | undefined) ??
						(part.reasoning as string | undefined);
					if (text) {
						sawVisibleContent = true;
						yield {
							type: "reasoning-delta",
							text,
							metadata: extractGoogleThoughtMetadata(part),
						};
					}
					continue;
				}

				if (part.type === "file") {
					const extracted = extractGeneratedImage(part.file, mediaBudget);
					if (extracted.kind === "accepted") {
						sawVisibleContent = true;
						yield {
							type: "media",
							media: toGeneratedImageMedia(extracted.image),
						};
						continue;
					}
					if (extracted.kind === "rejected") {
						rejectedMediaErrors.push(extracted.error.message);
						continue;
					}
					// 在通用事件路径上保留非图片模型文件。
					const file = part.file as
						| { base64?: string; mediaType?: string }
						| undefined;
					const data = file?.base64;
					if (typeof data === "string" && data.length > 0) {
						const mediaType = file?.mediaType ?? "application/octet-stream";
						const validation = validateAndReserveBase64Media(
							data,
							{},
							mediaBudget,
						);
						if (!validation.ok) {
							rejectedMediaErrors.push(validation.message);
							continue;
						}
						sawVisibleContent = true;
						yield {
							type: "media",
							media: {
								id: `media_${nanoid()}`,
								modality: generatedMediaModalityFromMediaType(mediaType),
								mediaType,
								source: { type: "base64", data: validation.base64 },
								sizeBytes: validation.decodedBytes,
							},
						};
					}
					continue;
				}

				if (part.type === "tool-call") {
					const toolName =
						(part.toolName as string | undefined) ??
						(part.name as string | undefined) ??
						"tool";
					// provider 执行的工具在本次推理请求内完成。它们
					// 绝不能进入 AgentRuntime 的本地执行/审批循环。
					// provider 定义的客户端工具同理：streamText 执行它们
					// 并在返回控制权之前继续内部模型步骤。
					const modelTool = request.modelTools?.find(
						(tool) => tool.name === toolName,
					);
					if (modelTool) {
						const explicitToolCallId =
							(part.toolCallId as string | undefined) ??
							(part.id as string | undefined);
						const adapter = modelToolAdapters?.[modelTool.name];
						if (adapter?.projectResult && !explicitToolCallId) {
							throw new Error(
								`Model tool "${modelTool.name}" call is missing a valid tool-call ID`,
							);
						}
						const toolCallId = explicitToolCallId ?? `model_tool_${nanoid()}`;
						const execution =
							part.providerExecuted === true ? "provider" : "client";
						if (adapter?.projectResult) {
							activeProjectedModelToolCalls.set(toolCallId, {
								toolName: modelTool.name,
								input: part.input ?? part.args,
								execution,
							});
						}
						yield {
							type: "tool-call-delta",
							toolCallId,
							toolName: modelTool.name,
							execution,
							input: part.input ?? part.args,
						};
						continue;
					}
					if (part.providerExecuted === true) {
						const toolCallId =
							(part.toolCallId as string | undefined) ??
							(part.id as string | undefined) ??
							`provider_tool_${nanoid()}`;
						observationalProviderToolCallIds.add(toolCallId);
						sawVisibleContent = true;
						yield {
							type: "tool-call-delta",
							toolCallId,
							toolName,
							execution: "provider",
							input: part.input ?? part.args,
						};
						continue;
					}
					sawToolCalls = true;
					sawVisibleContent = true;
					const toolCallId =
						(part.toolCallId as string | undefined) ??
						(part.id as string | undefined) ??
						`tool_${nanoid()}`;
					emittedToolCallIds.add(toolCallId);
					const input = (part.input ?? part.args ?? {}) as unknown;
					const inputText =
						typeof input === "string" ? input : JSON.stringify(input);
					yield {
						type: "tool-call-delta",
						toolCallId,
						toolName,
						input: typeof input === "string" ? undefined : input,
						inputText,
						metadata: buildToolCallMetadata({
							metadata: extractGoogleThoughtMetadata(part),
							request,
							context,
						}),
					};
					continue;
				}

				if (part.type === "tool-result") {
					const toolName =
						(part.toolName as string | undefined) ??
						(part.name as string | undefined) ??
						"tool";
					const modelTool = request.modelTools?.find(
						(tool) => tool.name === toolName,
					);
					if (modelTool) {
						const explicitToolCallId =
							(part.toolCallId as string | undefined) ??
							(part.id as string | undefined);
						const adapter = modelToolAdapters?.[modelTool.name];
						if (adapter?.projectResult && !explicitToolCallId) {
							throw new Error(
								`Model tool "${modelTool.name}" result is missing a valid tool-call ID`,
							);
						}
						const toolCallId = explicitToolCallId ?? `model_tool_${nanoid()}`;
						if (adapter?.projectResult) {
							if (part.preliminary !== true) {
								if (!activeProjectedModelToolCalls.has(toolCallId)) {
									throw new Error(
										`Model tool "${modelTool.name}" returned a result without a matching call`,
									);
								}
								// provider SDK 可能重复发送终态工具结果。缓冲
								// 最新值并只校验一次，使重复既不会发出
								// 重复媒体，也不会两次消耗聚合媒体预算。
								pendingProjectedModelToolOutputs.set(
									toolCallId,
									part.output ?? part.result,
								);
								projectedModelToolErrors.delete(toolCallId);
							}
							continue;
						}
						if (part.preliminary !== true) {
							yield {
								type: "tool-result",
								toolCallId,
								toolName: modelTool.name,
								execution:
									part.providerExecuted === true ? "provider" : "client",
								input: part.input ?? part.args,
								output: part.output ?? part.result,
							};
						}
						continue;
					}
					const toolCallId =
						(part.toolCallId as string | undefined) ??
						(part.id as string | undefined);
					if (
						part.providerExecuted === true ||
						(toolCallId && observationalProviderToolCallIds.has(toolCallId))
					) {
						if (part.preliminary !== true) {
							sawVisibleContent = true;
							yield {
								type: "tool-result",
								toolCallId: toolCallId ?? `provider_tool_${nanoid()}`,
								toolName,
								execution: "provider",
								input: part.input ?? part.args,
								output: part.output ?? part.result,
							};
						}
						continue;
					}
				}

				if (part.type === "tool-error") {
					const toolName =
						(part.toolName as string | undefined) ??
						(part.name as string | undefined) ??
						"tool";
					const modelTool = request.modelTools?.find(
						(tool) => tool.name === toolName,
					);
					if (modelTool) {
						const explicitToolCallId =
							(part.toolCallId as string | undefined) ??
							(part.id as string | undefined);
						const adapter = modelToolAdapters?.[modelTool.name];
						if (adapter?.projectResult && !explicitToolCallId) {
							throw new Error(
								`Model tool "${modelTool.name}" error is missing a valid tool-call ID`,
							);
						}
						const toolCallId = explicitToolCallId ?? `model_tool_${nanoid()}`;
						if (adapter?.projectResult) {
							pendingProjectedModelToolOutputs.delete(toolCallId);
							projectedModelToolErrors.set(
								toolCallId,
								`Model tool "${modelTool.name}" failed: ${extractErrorMessage(part.error)}`,
							);
							continue;
						}
						yield {
							type: "tool-result",
							toolCallId,
							toolName: modelTool.name,
							execution: part.providerExecuted === true ? "provider" : "client",
							input: part.input ?? part.args,
							output: { error: extractErrorMessage(part.error) },
							isError: true,
						};
						continue;
					}
					{
						const errorToolCallId =
							(part.toolCallId as string | undefined) ??
							(part.id as string | undefined);
						if (
							part.providerExecuted === true ||
							(errorToolCallId &&
								observationalProviderToolCallIds.has(errorToolCallId))
						) {
							yield {
								type: "tool-result",
								toolCallId: errorToolCallId ?? `provider_tool_${nanoid()}`,
								toolName,
								execution: "provider",
								input: part.input ?? part.args,
								output: { error: extractErrorMessage(part.error) },
								isError: true,
							};
							continue;
						}
					}
					sawToolCalls = true;
					const toolCallId =
						(part.toolCallId as string | undefined) ??
						(part.id as string | undefined) ??
						`tool_${nanoid()}`;
					const alreadyEmitted = emittedToolCallIds.has(toolCallId);
					emittedToolCallIds.add(toolCallId);
					const input = (part.input ?? part.args ?? {}) as unknown;
					const inputText =
						typeof input === "string" ? input : JSON.stringify(input);
					const errorMessage =
						part.error === undefined
							? "Tool input was rejected by the model adapter"
							: extractErrorMessage(part.error);
					yield {
						type: "tool-call-delta",
						toolCallId,
						toolName,
						input: alreadyEmitted
							? undefined
							: typeof input === "string"
								? undefined
								: input,
						inputText: alreadyEmitted ? undefined : inputText,
						metadata: buildRecoverableToolErrorMetadata({
							part,
							errorMessage,
							request,
							context,
							toolName,
						}),
					};
					continue;
				}

				if (part.type === "finish") {
					finishUsage = part.usage ?? part.totalUsage;
					finishProviderMetadata = part.providerMetadata;
					finishReason =
						part.finishReason ?? part.rawFinishReason ?? part.reason;
				}

				if (part.type === "error") {
					streamError =
						capturedError?.current ?? captureStreamError(part.error);
					break;
				}

				if (part.type === "abort") {
					streamAborted = true;
					break;
				}
			}
		} else if (stream.textStream) {
			for await (const text of stream.textStream) {
				yield { type: "text-delta", text };
			}
		}
	} catch (error) {
		// 优先使用 onError 中的真实 provider 错误，而不是 AI SDK
		// 在记录 0 个步骤时抛出的通用 NoOutputGeneratedError。
		streamError = capturedError?.current ?? captureStreamError(error);
	}

	if (!streamError) {
		for (const [toolCallId, output] of pendingProjectedModelToolOutputs) {
			const active = activeProjectedModelToolCalls.get(toolCallId);
			const adapter = active ? modelToolAdapters?.[active.toolName] : undefined;
			if (!active || !adapter?.projectResult) continue;
			try {
				const projection = adapter.projectResult(output);
				const media: GeneratedMedia[] = [];
				const errors: string[] = [];
				for (const candidate of projection.media) {
					const normalized = normalizeProjectedModelToolMedia(
						candidate,
						mediaBudget,
					);
					if (normalized.ok) media.push(normalized.media);
					else errors.push(normalized.error);
				}
				if (media.length === 0) {
					projectedModelToolErrors.set(
						toolCallId,
						errors[0] ??
							`Model tool "${active.toolName}" returned no supported media`,
					);
					continue;
				}
				projectedModelToolResults.set(toolCallId, {
					media,
					activityOutput:
						projection.activityOutput ?? summarizeProjectedMedia(media),
				});
				projectedModelToolErrors.delete(toolCallId);
			} catch (error) {
				projectedModelToolErrors.set(toolCallId, extractErrorMessage(error));
			}
		}
	}

	if (!streamError && !streamAborted) {
		for (const toolCallId of activeProjectedModelToolCalls.keys()) {
			if (
				!projectedModelToolResults.has(toolCallId) &&
				!projectedModelToolErrors.has(toolCallId)
			) {
				const active = activeProjectedModelToolCalls.get(toolCallId);
				projectedModelToolErrors.set(
					toolCallId,
					`Model tool "${active?.toolName ?? "unknown"}" completed without a final result`,
				);
			}
		}
	}

	if (!streamError) {
		for (const [toolCallId, projection] of projectedModelToolResults) {
			const active = activeProjectedModelToolCalls.get(toolCallId);
			if (!active) continue;
			sawVisibleContent = true;
			for (const media of projection.media) {
				yield { type: "media", media };
			}
			yield {
				type: "tool-result",
				toolCallId,
				toolName: active.toolName,
				execution: active.execution,
				input: active.input,
				output: projection.activityOutput,
			};
		}
		for (const [toolCallId, error] of projectedModelToolErrors) {
			const active = activeProjectedModelToolCalls.get(toolCallId);
			if (!active) continue;
			yield {
				type: "tool-result",
				toolCallId,
				toolName: active.toolName,
				execution: active.execution,
				input: active.input,
				output: { error },
				isError: true,
			};
		}
		if (
			!sawVisibleContent &&
			(projectedModelToolErrors.size > 0 || rejectedMediaErrors.length > 0)
		) {
			streamError = captureStreamError(
				new Error(
					projectedModelToolErrors.values().next().value ??
						rejectedMediaErrors[0] ??
						"Model returned no supported media",
				),
			);
		}
	}

	// 优先使用 stream.usage（含原始成本数据），而非 finish 分片的用量。
	// 在模拟/测试场景中 stream.usage 可能为 undefined，回退到 finish 分片及其 providerMetadata。
	let usageToEmit: unknown;
	let metadataToUse: unknown;
	if (streamError) {
		usageToEmit = finishUsage;
		metadataToUse = finishProviderMetadata;
	} else if (stream.usage) {
		try {
			usageToEmit = await stream.usage;
		} catch (error) {
			if (!streamError) {
				streamError = capturedError?.current ?? captureStreamError(error);
			}
			usageToEmit = finishUsage;
			metadataToUse = finishProviderMetadata;
		}
	} else {
		usageToEmit = finishUsage;
		metadataToUse = finishProviderMetadata;
	}

	if (usageToEmit) {
		yield {
			type: "usage",
			usage: normalizeUsage(usageToEmit, metadataToUse, pricingValue, request),
		};
	}

	yield {
		type: "finish",
		reason: streamError ? "error" : mapFinishReason(finishReason, sawToolCalls),
		...(requestId ? { requestId } : {}),
		error: streamError?.message,
		errorClass: streamError?.errorClass,
		errorRetryable: streamError?.retryable,
		errorReported: streamError?.reported,
	};
}

async function createProviderModule(
	kind: ProviderModuleKind,
	config: GatewayResolvedProviderConfig,
	context: GatewayProviderContext,
): Promise<ProviderFactoryResult> {
	switch (kind) {
		case "cline": {
			const { createClineProviderModule } = await import("./vendors/cline");
			return createClineProviderModule(config, context);
		}
		case "openai": {
			const { createOpenAIProviderModule } = await import("./vendors/openai");
			return createOpenAIProviderModule(config, context);
		}
		case "openai-compatible": {
			const { createOpenAICompatibleProviderModule } = await import(
				"./vendors/openai-compatible"
			);
			return createOpenAICompatibleProviderModule(config, context);
		}
		case "anthropic": {
			const { createAnthropicProviderModule } = await import(
				"./vendors/anthropic"
			);
			return createAnthropicProviderModule(config, context);
		}
		case "google": {
			const { createGoogleProviderModule } = await import("./vendors/google");
			return createGoogleProviderModule(config, context);
		}
		case "vertex": {
			const { createVertexProviderModule } = await import("./vendors/vertex");
			return createVertexProviderModule(config, context);
		}
		case "bedrock": {
			const { createBedrockProviderModule } = await import("./vendors/bedrock");
			return createBedrockProviderModule(config);
		}
		case "mistral": {
			const { createMistralProviderModule } = await import("./vendors/mistral");
			return createMistralProviderModule(config);
		}
		case "claude-code": {
			const { createClaudeCodeProviderModule } = await import(
				"./vendors/community"
			);
			return createClaudeCodeProviderModule(config);
		}
		case "openai-codex": {
			const { createOpenAICodexProviderModule } = await import(
				"./vendors/community"
			);
			return createOpenAICodexProviderModule(config);
		}
		case "opencode": {
			const { createOpenCodeProviderModule } = await import(
				"./vendors/community"
			);
			return createOpenCodeProviderModule(config);
		}
		case "dify": {
			const { createDifyProviderModule } = await import("./vendors/community");
			return createDifyProviderModule(config);
		}
		case "ollama": {
			const { createOllamaProviderModule } = await import("./vendors/ollama");
			return createOllamaProviderModule(config, context);
		}
		case "sapaicore": {
			const { createSapAiCoreProviderModule } = await import(
				"./vendors/community"
			);
			return createSapAiCoreProviderModule(config);
		}
	}
}

/**
 * 用瞬时故障重试中间件（空响应 + 内容前的网络中断）包裹
 * vendor 构造的模型。
 *
 * 全空回合（无文本、无推理、无工具调用）是跨 provider 的
 * 普遍现象：生产遥测在托管后端（openrouter、
 * cline、通用 OpenAI 兼容端点）上都观察到过，并非只有本地 Ollama。
 * 空的 assistant 回合在 agent 运行时中是硬失败（"Model
 * returned empty response"），因此一次瞬时抖动就会杀死任务。
 * 同一份遥测还显示，流中网络死亡（UND_ERR_SOCKET、
 * body/headers 超时、ECONNRESET）是主要的网络类运行
 * 杀手——AI SDK 自带的重试只覆盖请求发起阶段，因此一旦
 * 流已经开始，没有任何其他层会重试。在这里重试——每个 AI SDK
 * vendor 都会经过的唯一组装配点——把这些抖动变成
 * 无事发生，同时对持续为空的模型或真正断开的连接，
 * 仍保留运行时的显式失败。
 *
 * 作为*最外层*包裹应用，使每次重试都重跑 vendor 的完整
 * 请求管线，包括挂在 `provider.operations.language(...)` 内部的
 * 任何 vendor 级中间件。Vendor 可通过
 * `ProviderFactoryResult.retryEmptyResponses` 选择退出或调整尝试次数。
 */
export function withEmptyResponseRetry(
	model: unknown,
	retryEmptyResponses: ProviderFactoryResult["retryEmptyResponses"],
	logger: GatewayProviderContext["logger"],
): unknown {
	if (retryEmptyResponses === false) {
		return model;
	}
	return wrapLanguageModel({
		model: model as LanguageModelV4,
		middleware: createRetryEmptyResponseMiddleware({
			...retryEmptyResponses,
			logger,
		}),
	});
}

function createAiSdkProvider(
	defaultKind: ProviderModuleKind,
): GatewayProviderFactory {
	return async (config) => ({
		async *stream(request, context) {
			// 多协议 HTTP 网关在 models.dev 中声明模型适配器。
			// 保持原生和本地 CLI 传输对其模型具有权威性。
			const kind = resolveModelProviderKind(defaultKind, context);
			const log = context.logger;
			let stream: AiSdkStreamResult | undefined;
			const capturedError: { current: CapturedStreamError | undefined } = {
				current: undefined,
			};
			try {
				const provider = await createProviderModule(
					kind,
					{
						...config,
						fetch: wrapFetchForStickySession(
							wrapFetchForProviderRequestCapture(config.fetch, request),
							request,
							context,
						),
					},
					context,
				);
				const composedProviderOptions = composeAiSdkProviderOptions(
					request,
					context,
					kind,
				);
				const googleImageProviderKey =
					kind === "google"
						? "google"
						: kind === "vertex"
							? "vertex"
							: undefined;
				const providerOptions =
					context.provider.metadata?.imageTransport === "openrouter" &&
					modelProducesImages(context.model)
						? {
								...composedProviderOptions,
								openrouter: {
									...((composedProviderOptions[
										request.providerId as keyof typeof composedProviderOptions
									] ??
										composedProviderOptions.openaiCompatible ??
										{}) as Record<string, unknown>),
									// OpenRouter 兼容的图片生成底层使用 chat
									// completions，并要求显式的输出
									// 模态。
									modalities: ["image", "text"],
								},
							}
						: googleImageProviderKey !== undefined &&
								modelProducesImages(context.model) &&
								!usesImageGenerationOperation(context.model)
							? {
									...composedProviderOptions,
									[googleImageProviderKey]: {
										...((composedProviderOptions[googleImageProviderKey] ??
											{}) as Record<string, unknown>),
										responseModalities: ["TEXT", "IMAGE"],
									},
								}
							: composedProviderOptions;
				const modelOperation = context.model.operation ?? "language";
				if (
					modelOperation !== "language" &&
					modelOperation !== "image-generation"
				) {
					throw new Error(
						`Provider "${context.provider.id}" does not implement the "${modelOperation}" model operation`,
					);
				}
				if (usesImageGenerationOperation(context.model)) {
					if (!provider.operations.imageGeneration) {
						throw new Error(
							`Provider "${context.provider.id}" does not support image generation models`,
						);
					}
					const prompt = resolveImageGenerationPrompt(request, context);
					recordProviderRequestCapture({
						stage: "ai_sdk_prompt",
						request,
						payload: {
							operation: "generate_image",
							prompt:
								typeof prompt === "string"
									? prompt
									: {
											text: prompt.text,
											imageCount: prompt.images.length,
										},
							providerOptions,
						},
					});
					const result = await generateImage({
						model: provider.operations.imageGeneration(
							context.model.id,
						) as never,
						prompt,
						abortSignal: request.signal,
						providerOptions: providerOptions as never,
					});
					let emittedImages = 0;
					let rejectedImageError: string | undefined;
					const mediaBudget = createMediaBudgetState();
					for (const file of result.images) {
						const extracted = extractGeneratedImage(file, mediaBudget);
						if (extracted.kind === "rejected") {
							rejectedImageError = extracted.error.message;
							continue;
						}
						if (extracted.kind !== "accepted") continue;
						emittedImages += 1;
						yield {
							type: "media",
							media: toGeneratedImageMedia(extracted.image),
						};
					}
					if (emittedImages === 0) {
						throw new Error(
							rejectedImageError ?? "Image model returned no supported images",
						);
					}
					if (result.usage) {
						yield {
							type: "usage",
							usage: normalizeUsage(
								result.usage as Record<string, unknown>,
								result.providerMetadata,
								context.model.metadata?.pricing,
								request,
							),
						};
					}
					yield { type: "finish", reason: "stop" };
					return;
				}
				const aiSdkTelemetry = await resolveGatewayAiSdkTelemetry(
					config.providerId,
					request,
				);
				const externalToolExecutionDisabled =
					providerDisablesExternalToolExecution(context);
				const toolCallingDisabled =
					externalToolExecutionDisabled ||
					!modelSupportsToolCalling(context.model);
				const runtimeTools = toolCallingDisabled
					? undefined
					: toAiSdkTools(request);
				const activeModelTools = toolCallingDisabled
					? []
					: (request.modelTools ?? []).filter(
							(tool) => !hasAiSdkTool(runtimeTools, tool.name),
						);
				const modelToolRequest = {
					...request,
					modelTools: activeModelTools,
				};
				const modelToolAdapters = buildProviderModelTools(
					provider,
					modelToolRequest,
					context,
				);
				const modelTools = toAiSdkModelToolSet(modelToolAdapters);
				const tools = mergeAiSdkTools(runtimeTools, modelTools);
				const systemPrompt = resolveAiSdkSystemPrompt(request);
				const useSystemOption =
					typeof systemPrompt === "string" && systemPrompt.trim().length > 0;
				const messagesSystemPrompt = useSystemOption ? undefined : systemPrompt;
				const messages = buildAiSdkRequestMessages(
					request,
					context,
					messagesSystemPrompt,
				);
				const portableReasoning = resolvePortableReasoning(request);
				const requestConfig = provider.buildStreamConfig
					? provider.buildStreamConfig(request, context)
					: buildAiSdkStreamConfig(request, context);
				recordProviderRequestCapture({
					stage: "ai_sdk_prompt",
					request,
					payload: {
						messages,
						...(useSystemOption ? { system: systemPrompt } : {}),
						tools,
						providerOptions,
						...requestConfig,
						...(portableReasoning ? { reasoning: portableReasoning } : {}),
					},
				});
				stream = await withAiSdkLangfuseTraceContext(
					aiSdkTelemetry.isEnabled,
					request,
					() =>
						streamText({
							model: withEmptyResponseRetry(
								provider.operations.language(context.model.id),
								provider.retryEmptyResponses,
								context.logger,
							) as never,
							messages: messages as never,
							...(useSystemOption ? { system: systemPrompt } : {}),
							...(tools ? { tools } : {}),
							abortSignal: request.signal,
							maxRetries: MODEL_REQUEST_MAX_RETRIES,
							experimental_repairToolCall: repairMalformedToolCall as never,
							telemetry: {
								...aiSdkTelemetry,
								functionId: "cline-agent-turn",
								includeRuntimeContext: {
									distinctId: true,
									userId: true,
									sessionId: true,
									clientName: true,
									clientVersion: true,
									clineCoreVersion: true,
									tags: true,
									conversationId: true,
									runId: true,
									iteration: true,
									providerId: true,
									modelId: true,
									resolvedModelId: true,
								},
							},
							runtimeContext: buildAiSdkRuntimeContext(request, context),
							providerOptions: providerOptions as never,
							...(provider.executesModelTools && activeModelTools.length
								? { stopWhen: stepCountIs(8) }
								: {}),
							...requestConfig,
							...(portableReasoning ? { reasoning: portableReasoning } : {}),
							onError: ({ error: streamError }) => {
								const captured = captureStreamError(streamError);
								const msg = captured.message;
								capturedError.current = captured;
								if (log?.error) {
									log.error("[ai-sdk] stream error", {
										providerId: request.providerId,
										error: streamError,
										severity: "error",
									});
								} else if (log) {
									log.log(`[ai-sdk] stream error: ${msg}`, {
										providerId: request.providerId,
										severity: "error",
									});
								}
								captured.reported = captureSdkError(context.telemetry, {
									component: "llms",
									operation: "provider.stream",
									error: streamError,
									errorMessage: msg,
									severity: "error",
									handled: true,
									context: {
										providerId: request.providerId,
										modelId: request.modelId,
										providerKind: kind,
									},
								});
							},
						}) as unknown as AiSdkStreamResult,
				);

				// 在开始迭代之前抑制悬空 promise 拒绝（finishReason、totalUsage、steps 等）。
				// AI SDK 在流的 flush 回调内拒绝这些 DelayedPromise，
				// 而 flush 回调在迭代期间运行，因此必须提前附加 .catch()
				// 处理器，否则 Bun/Node 会把它们暴露为未处理拒绝。
				suppressDanglingStreamPromises(stream);

				yield* emitAiSdkEvents(
					stream,
					modelToolRequest,
					context,
					context.model.metadata?.pricing,
					capturedError,
					modelToolAdapters,
				);
			} catch (error) {
				suppressDanglingStreamPromises(stream);
				// 优先使用 onError 中捕获的真实 provider 错误，而不是 AI SDK
				// 在记录 0 个步骤时抛出的通用 NoOutputGeneratedError。
				const captured = capturedError.current ?? captureStreamError(error);
				const msg = captured.message;
				if (log?.error) {
					log.error("[ai-sdk] provider error", {
						providerId: request.providerId,
						error,
						severity: "error",
					});
				} else if (log) {
					log.log(`[ai-sdk] provider error: ${msg}`, {
						providerId: request.providerId,
						severity: "error",
					});
				}
				const reported = captureSdkError(context.telemetry, {
					component: "llms",
					operation: "provider.create_or_stream",
					error,
					errorMessage: msg,
					severity: "error",
					handled: true,
					context: {
						providerId: request.providerId,
						modelId: request.modelId,
						providerKind: kind,
					},
				});
				yield {
					type: "finish",
					reason: "error",
					error: msg,
					errorClass: captured.errorClass,
					errorRetryable: captured.retryable,
					errorReported: reported || captured.reported,
				};
			}
		},
	});
}

function resolveModelProviderKind(
	defaultKind: ProviderModuleKind,
	context: GatewayProviderContext,
): ProviderModuleKind {
	if (
		defaultKind !== "openai-compatible" ||
		!context.provider.metadata?.routing?.modelApiProtocol
	)
		return defaultKind;
	switch (context.model.metadata?.apiProtocol) {
		case "openai-responses":
			return "openai";
		case "anthropic":
			return "anthropic";
		case "gemini":
			return "google";
		default:
			return defaultKind;
	}
}

export const createOpenAIProvider = createAiSdkProvider("openai");
export const createClineProvider = createAiSdkProvider("cline");
export const createOpenAICompatibleProvider =
	createAiSdkProvider("openai-compatible");
export const createAnthropicProvider = createAiSdkProvider("anthropic");
export const createGoogleProvider = createAiSdkProvider("google");
export const createVertexProvider = createAiSdkProvider("vertex");
export const createBedrockProvider = createAiSdkProvider("bedrock");
export const createMistralProvider = createAiSdkProvider("mistral");
export const createClaudeCodeProvider = createAiSdkProvider("claude-code");
export const createOpenAICodexProvider = createAiSdkProvider("openai-codex");
export const createOpenCodeProvider = createAiSdkProvider("opencode");
export const createDifyProvider = createAiSdkProvider("dify");
export const createOllamaProvider = createAiSdkProvider("ollama");
export const createSapAiCoreProvider = createAiSdkProvider("sapaicore");
