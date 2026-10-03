/**
 * 指示认证/凭据失败的错误消息子串。
 * 与提供商认证处理器一起使用，以决定失败的 API 调用
 * 是否应触发 OAuth 刷新。
 */
export const AUTH_ERROR_PATTERNS = [
	"401",
	"403",
	"unauthorized",
	"forbidden",
	"invalid token",
	"expired token",
	"authentication",
] as const;

/**
 * 当 `error` 看起来像认证失败时返回 `true`。
 */
export function isLikelyAuthError(error: unknown): boolean {
	const message = (
		error instanceof Error ? error.message : String(error)
	).toLowerCase();
	return AUTH_ERROR_PATTERNS.some((pattern) => message.includes(pattern));
}
