/**
 * 处理器接口类型
 *
 * 所有提供商处理器必须实现的核心接口。
 */

import type { Message, ModelInfo, ToolDefinition } from "@cline/shared";
import type { ApiStream, ApiStreamUsageChunk } from "./stream";

/**
 * 处理器返回的模型信息
 */
export interface HandlerModelInfo {
	/** 模型标识符 */
	id: string;
	/** 模型能力和定价信息 */
	info: ModelInfo;
}

/**
 * 核心 API 处理器接口
 *
 * 所有提供商必须实现此接口。
 */
export interface ApiHandler {
	/**
	 * 将 Cline 消息转换为提供商特定的消息格式
	 *
	 * @param systemPrompt - 要使用的系统提示词
	 * @param messages - 对话历史
	 * @returns 提供商特定的消息载荷
	 */
	getMessages(systemPrompt: string, messages: Message[]): unknown;

	/**
	 * 创建流式消息补全
	 *
	 * @param systemPrompt - 要使用的系统提示词
	 * @param messages - 对话历史
	 * @param tools - 可选的工具定义，用于原生工具调用
	 * @returns 异步生成器，产生流块
	 */
	createMessage(
		systemPrompt: string,
		messages: Message[],
		tools?: ToolDefinition[],
	): ApiStream;

	/**
	 * 获取当前模型配置
	 */
	getModel(): HandlerModelInfo;

	/**
	 * 获取上次 API 调用的用量信息（可选）
	 * 某些提供商可以从单独的端点获取
	 */
	getApiStreamUsage?(): Promise<ApiStreamUsageChunk | undefined>;

	/**
	 * 中止当前请求（可选）
	 */
	abort?(): void;

	/**
	 * 更新用于后续请求的中止信号（可选）。
	 */
	setAbortSignal?(signal: AbortSignal | undefined): void;
}

/**
 * 简单单轮补全的处理器
 */
export interface SingleCompletionHandler {
	/**
	 * 完成单条提示词（不流式）
	 */
	completePrompt(prompt: string): Promise<string>;
}

/**
 * 创建处理器的工厂函数类型
 */
export type HandlerFactory<TConfig = unknown> = (config: TConfig) => ApiHandler;

/**
 * 懒加载处理器的异步工厂
 */
export type LazyHandlerFactory<TConfig = unknown> = (
	config: TConfig,
) => Promise<ApiHandler>;
