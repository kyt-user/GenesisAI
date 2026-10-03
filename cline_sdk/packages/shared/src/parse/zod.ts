/**
 * Zod 工具
 *
 * 用于处理 Zod schema 的辅助函数。
 */

import { z } from "zod";

/**
 * 使用 Zod schema 验证输入
 * 验证失败时抛出格式化错误
 */
export function validateWithZod<T>(schema: z.ZodType<T>, input: unknown): T {
	const result = schema.safeParse(input);
	if (!result.success) {
		throw new Error(z.prettifyError(result.error));
	}
	return result.data;
}

export function zodToJsonSchema(schema: z.ZodTypeAny): Record<string, unknown> {
	return z.toJSONSchema(schema);
}
