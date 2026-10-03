/**
 * 跨包日志接口，供注入自有日志器的宿主使用（如 `pino` 或 VS Code API）。
 *
 * {@link BasicLogger.debug} 用于详细诊断（宿主通常将其限制在调试日志级别后）。
 * {@link BasicLogger.log} 是非错误的操作消息的主要通道。
 * {@link BasicLogger.error} 是可选的；省略时，宿主可能通过 {@link BasicLogger.log}
 * 以 {@link BasicLogMetadata.severity} `"error"` 传递失败，或在其他地方处理错误。
 *
 * 需要完整定义的空操作实现时使用 {@link noopBasicLogger}。
 */

/**
 * 可选的结构化字段，跨 SDK 使用并推荐用于宿主日志后端。
 * 调用方可以自由添加其他键；这些名称是跨组件查询的共享约定。
 */
export interface BasicLogMetadata extends Record<string, unknown> {
	sessionId?: string;
	runId?: string;
	providerId?: string;
	toolName?: string;
	durationMs?: number;
	/**
	 * 使用 {@link BasicLogger.log} 时，为将单个 `log` 方法映射到
	 * 多个输出级别的后端（如 Pino 的 `info` vs `warn`）消除严重性歧义。
	 */
	severity?: "info" | "warn" | "error";
}

export interface BasicLogger {
	/** 详细诊断；宿主在非调试模式下应空操作或过滤。 */
	debug: (message: string, metadata?: BasicLogMetadata) => void;
	/** 操作消息（替代旧的 `info` / 非错误 `warn` 拆分）。 */
	log: (message: string, metadata?: BasicLogMetadata) => void;
	error?: (
		message: string,
		metadata?: BasicLogMetadata & { error?: unknown },
	) => void;
}

/** 所有级别均实现为空操作；未注入日志器时的安全默认值。 */
export const noopBasicLogger: BasicLogger = {
	debug: () => {},
	log: () => {},
	error: () => {},
};
