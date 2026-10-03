import type {
	GatewayProviderContext,
	GatewayStreamRequest,
	GeneratedMedia,
	ModelTool,
	ModelToolName,
} from "@cline/shared";
import type { CallSettings, ToolSet } from "ai";
import type { RetryEmptyResponseOptions } from "../middleware/retry-empty-response";

export type ProviderGeneratedMedia = Omit<GeneratedMedia, "id" | "sizeBytes">;

export interface ModelToolResultProjection {
	media: readonly ProviderGeneratedMedia[];
	activityOutput?: unknown;
}

export interface BuiltModelTool {
	tool: ToolSet[string];
	/** 将 provider 原生工具结果投影为规范的助手媒体。 */
	projectResult?: (output: unknown) => ModelToolResultProjection;
}

export type BuiltModelTools = Partial<Record<ModelToolName, BuiltModelTool>>;

export interface ProviderFactoryResult {
	operations: {
		language: (modelId: string) => unknown;
		imageGeneration?: (modelId: string) => unknown;
		speechGeneration?: (modelId: string) => unknown;
		videoGeneration?: (modelId: string) => unknown;
		transcription?: (modelId: string) => unknown;
	};
	/** 将可移植的模型工具意图转换为 provider 定义的 AI SDK 工具。 */
	buildModelTools?: (tools: readonly ModelTool[]) => BuiltModelTools;
	/** AI SDK 执行 provider 定义的客户端工具并继续模型步骤。 */
	executesModelTools?: boolean;
	/**
	 * 网关级瞬时失败重试的策略。每个 vendor
	 * 模型都在 `ai-sdk.ts` 的中央组合点被
	 * `createRetryEmptyResponseMiddleware` 包装，它在一次尝试预算内重试两种
	 * 瞬时失败模式：全空回合（无文本、
	 * 无推理、无工具调用——否则会以
	 * "Model returned empty response" 硬失败任务）和任何模型输出前的
	 * 流中途网络中断（socket 关闭、body/headers
	 * 超时、ECONNRESET——否则会杀死运行；AI SDK 自身的重试
	 * 仅覆盖请求发起）。设为 `false` 让 vendor 退出重试，或
	 * 提供选项调整尝试次数/延迟，而无需分叉
	 * 中间件。留空使用默认值。
	 */
	retryEmptyResponses?: false | Omit<RetryEmptyResponseOptions, "logger">;
	buildStreamConfig?: (
		request: GatewayStreamRequest,
		context: GatewayProviderContext,
	) => Partial<CallSettings>;
}

export interface AiSdkStreamPart {
	type?: string;
	response?: { headers?: Record<string, string> };
	[key: string]: unknown;
}

/**
 * AI SDK 在 finish 流部分发射的规范化用量结构。
 * 这是流完成前可用的中间表示。
 * 所有 token 计数使用 camelCase 命名约定。
 *
 * @property inputTokens - 总输入/提示词 token（所有 provider）
 * @property inputTokenDetails - 输入 token 的细分：
 *   - noCacheTokens：新鲜（未缓存）输入 token（Anthropic、OpenRouter、Gemini）
 *   - cacheReadTokens：从缓存读取的 token（Anthropic、OpenRouter）
 *   - cacheWriteTokens：写入缓存的 token（Anthropic、OpenRouter）
 * @property outputTokens - 总输出/补全 token（所有 provider）
 * @property outputTokenDetails - 输出 token 的细分：
 *   - textTokens：常规文本 token（OpenAI、OpenRouter、Gemini）
 *   - reasoningTokens：用于推理的 token（带 o1 的 OpenAI、OpenRouter、带扩展思考的 Anthropic）
 * @property totalTokens - 输入和输出 token 之和（所有 provider）
 * @property reasoningTokens - 总推理 token（OpenAI、OpenRouter）
 * @property cachedInputTokens - cache-read token 的别名（便捷字段）
 */
export interface AiSdkStreamTotalUsage {
	inputTokens?: number;
	inputTokenDetails?: {
		noCacheTokens?: number;
		cacheReadTokens?: number;
		cacheWriteTokens?: number;
	};
	outputTokens?: number;
	outputTokenDetails?: {
		textTokens?: number;
		reasoningTokens?: number;
	};
	totalTokens?: number;
	reasoningTokens?: number;
	cachedInputTokens?: number;
}

/**
 * 流完成后通过 stream.usage promise 可用的 AI SDK 完整用量结构。
 * 扩展 AiSdkStreamTotalUsage 并添加原始 provider 专属响应。
 * raw 字段包含未修改的 provider 响应，支持成本提取和详细计费信息。
 *
 * @property raw - Provider 专属的原始响应字段：
 *   **Anthropic**：input_tokens、cache_creation_input_tokens、cache_read_input_tokens、
 *     cache_creation.ephemeral_5m_input_tokens、cache_creation.ephemeral_1h_input_tokens、
 *     output_tokens、service_tier、inference_geo
 *   **Gemini**：promptTokenCount、candidatesTokenCount、totalTokenCount、promptTokensDetails
 *   **OpenAI/Vercel**：input_tokens、input_tokens_details.cached_tokens、output_tokens、
 *     output_tokens_details.reasoning_tokens
 *   **OpenRouter**：prompt_tokens、completion_tokens、total_tokens、prompt_tokens_details.cached_tokens、
 *     completion_tokens_details.reasoning_tokens、cost、is_byok、cost_details、market_cost
 */
export interface AiSdkStreamUsage extends AiSdkStreamTotalUsage {
	raw?: {
		input_tokens?: number;
		cache_creation_input_tokens?: number;
		cache_read_input_tokens?: number;
		cache_creation?: {
			ephemeral_5m_input_tokens?: number;
			ephemeral_1h_input_tokens?: number;
		};
		input_tokens_details?: {
			cached_tokens?: number;
			cache_write_tokens?: number;
		};
		output_tokens?: number;
		output_tokens_details?: {
			reasoning_tokens?: number;
		};
		service_tier?: string;
		inference_geo?: string;
		promptTokenCount?: number;
		candidatesTokenCount?: number;
		totalTokenCount?: number;
		promptTokensDetails?: {
			cached_tokens?: number;
			cache_write_tokens?: number;
		};
		prompt_tokens?: number;
		completion_tokens?: number;
		total_tokens?: number;
		prompt_tokens_details?: {
			cached_tokens?: number;
			cache_write_tokens?: number;
		};
		completion_tokens_details?: {
			reasoning_tokens?: number;
		};
		cost?: number;
		is_byok?: boolean;
		cost_details?: {
			upstream_inference_cost?: number | null;
			upstream_inference_prompt_cost?: number;
			upstream_inference_completions_cost?: number;
		};
		market_cost?: number;
	};
	reasoningTokens?: number;
	cachedInputTokens?: number;
}

/**
 * 流完成时发射的 finish 事件部分。
 * 包含早期用量数据（不含原始 provider 响应）和 finish 元数据。
 *
 * @property type - 始终为 "finish"
 * @property finishReason - 规范化完成原因（stop、max_tokens、tool-calls、error）
 * @property rawFinishReason - Provider 的原始完成原因字符串
 * @property totalUsage - 流结束时的用量快照（AiSdkStreamTotalUsage 结构）
 */
export interface AiSdkStreamFinishPart {
	type: "finish";
	finishReason?: string;
	rawFinishReason?: string;
	totalUsage?: AiSdkStreamTotalUsage | Record<string, unknown>;
}

/**
 * AI SDK streamText() 调用的完整结果。
 * 通过 promise 提供流式内容（文本、工具调用、推理）和用量数据。
 *
 * @property fullStream - 原始流部分（text-delta、tool-call、finish 等）
 * @property textStream - 仅文本增量的便捷迭代器
 * @property text - 解析为完整生成文本的 Promise
 * @property usage - 解析为带原始 provider 响应的完整用量数据的 Promise。
 *   优先使用它而非 finish 部分的 totalUsage，因为它包含 cost_details
 *   和其他精确计费所需的 provider 专属元数据。
 */
export interface AiSdkStreamResult {
	fullStream?: AsyncIterable<AiSdkStreamPart>;
	textStream?: AsyncIterable<string>;
	text?: Promise<string> | string;
	usage?: Promise<AiSdkStreamUsage | Record<string, unknown>>;
}
