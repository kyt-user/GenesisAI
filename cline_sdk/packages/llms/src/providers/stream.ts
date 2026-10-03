/**
 * API 流类型
 *
 * 所有 provider 都会产出的统一流式响应类型。
 * 无论底层 provider 是谁，都提供一致的接口。
 */
import type { GeneratedMedia } from "@cline/shared";

/**
 * 主流类型——产出分片的异步生成器
 */
export type ApiStream = AsyncGenerator<ApiStreamChunk> & { id?: string };

/**
 * 所有可能分片类型的联合
 */
export type ApiStreamChunk =
	| ApiStreamTextChunk
	| ApiStreamMediaChunk
	| ApiStreamReasoningChunk
	| ApiStreamUsageChunk
	| ApiStreamToolCallsChunk
	| ApiStreamDoneChunk;

/**
 * 文本内容分片
 */
export interface ApiStreamTextChunk {
	type: "text";
	/** 模型生成的文本内容 */
	text: string;
	/** 与此分片关联的响应 ID */
	id: string;
	/** 思考签名（Gemini 使用） */
	signature?: string;
}

/**
 * 模型生成的媒体内容分片。
 */
export interface ApiStreamMediaChunk {
	type: "media";
	media: GeneratedMedia;
	/** 与此分片关联的响应 ID */
	id: string;
}

/**
 * 推理/思考内容分片
 */
export interface ApiStreamReasoningChunk {
	type: "reasoning";
	/** 模型生成的推理文本 */
	reasoning: string;
	/** 附加推理详情（provider 特有） */
	details?: unknown;
	/** 思考块的签名（Anthropic、Gemini） */
	signature?: string;
	/** 被脱敏的推理数据 */
	redacted_data?: string;
	/** 与此分片关联的响应 ID */
	id: string;
}

/**
 * 用量/token 计数分片
 */
export interface ApiStreamUsageChunk {
	type: "usage";
	/** provider 报告的总输入 token 数 */
	inputTokens: number;
	/** 输出 token 数 */
	outputTokens: number;
	/** 写入缓存的 token 数 */
	cacheWriteTokens?: number;
	/** 从缓存读取的 token 数 */
	cacheReadTokens?: number;
	/** 思考/推理 token 数 */
	thoughtsTokenCount?: number;
	/** 总成本（美元，如可计算） */
	totalCost?: number;
	/** 响应 ID */
	id: string;
}

/**
 * 工具调用分片
 */
export interface ApiStreamToolCallsChunk {
	type: "tool_calls";
	/** 工具调用信息 */
	tool_call: ApiStreamToolCall;
	/** 响应 ID */
	id: string;
	/** 思考签名（Gemini） */
	signature?: string;
}

/**
 * 工具调用详情
 */
export interface ApiStreamToolCall {
	/** 本次工具调用的调用 ID */
	call_id?: string;
	/** 函数/工具信息 */
	function: {
		/** 工具调用 ID */
		id?: string;
		/** 工具名称 */
		name?: string;
		/** 传给工具的参数（可以是字符串或已解析的对象） */
		arguments?: string | Record<string, unknown>;
	};
}

/**
 * 流完成分片——表示流已结束
 */
export interface ApiStreamDoneChunk {
	type: "done";
	/** 流是否成功完成 */
	success: boolean;
	/** 流失败时的错误消息 */
	error?: string;
	/** 响应不完整的原因（例如 "max_output_tokens"） */
	incompleteReason?: string;
	/** 响应 ID */
	id: string;
}
