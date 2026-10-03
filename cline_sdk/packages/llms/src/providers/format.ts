import { safeJsonParse, safeJsonStringify } from "@cline/shared";

export function extractErrorMessage(error: unknown): string {
	// 通用 SDK 包装器本身不携带信号——当它们存在时，我们优先
	// 采用底层的 cause/detail（例如 AI SDK 的 AI_NoOutputGeneratedError）。
	const GENERIC_WRAPPER_MESSAGES = new Set([
		"no output generated. check the stream for errors.",
	]);
	const isGenericWrapperMessage = (message: string): boolean =>
		GENERIC_WRAPPER_MESSAGES.has(message.trim().toLowerCase());

	const hasErrorMessage = (
		value: unknown,
	): value is { error_message: unknown } =>
		typeof value === "object" && value !== null && "error_message" in value;

	// 从结构化的 provider 字段（error/detail/errors/responseBody）
	// 中提取人类可读的消息，不回退到顶层
	// `message`。由 Error 分支和普通对象分支共享。
	const extractStructuredDetail = (value: object): string | undefined => {
		const payload = value as {
			error?: { message?: string } | string;
			errors?: unknown;
			detail?: string;
			responseBody?: unknown;
			value?: unknown;
		};
		if (typeof payload.error === "string" && payload.error.trim()) {
			return payload.error;
		}
		if (
			payload.error &&
			typeof payload.error === "object" &&
			typeof payload.error.message === "string" &&
			payload.error.message.trim()
		) {
			return payload.error.message;
		}
		if (typeof payload.detail === "string" && payload.detail.trim()) {
			return payload.detail;
		}
		if (Array.isArray(payload.errors)) {
			for (const nestedError of payload.errors) {
				const nested = extractStructuredMessage(nestedError);
				if (nested) {
					return nested;
				}
			}
		}
		// 一些网关把上游 provider 的拒绝信息转发在 `value` 下，
		// 原始响应体 JSON 编码在 `value.error_message` 中
		// （例如 Vercel AI Gateway 转发 Alibaba Qwen 的上下文长度错误，
		// 顶层消息只是 "Stream error occurred"，而
		// cause 是无关的内部 ZodError）。
		if (hasErrorMessage(payload.value)) {
			const nested = extractStructuredMessage(payload.value.error_message);
			if (nested) {
				return nested;
			}
		}
		if ("responseBody" in payload && payload.responseBody !== value) {
			const nested = extractStructuredMessage(payload.responseBody);
			if (nested) {
				return nested;
			}
		}
		return undefined;
	};

	const extractStructuredMessage = (value: unknown): string | undefined => {
		if (!value) {
			return undefined;
		}
		if (typeof value === "string") {
			// JSON.parse 绝不产生 undefined，因此这里的 undefined 意味着该
			// 字符串不是 JSON——回退到去除空白后的字符串本身。
			const parsed = safeJsonParse<unknown>(value);
			if (parsed !== undefined) {
				return extractStructuredMessage(parsed);
			}
			return value.trim() || undefined;
		}
		if (typeof value !== "object") {
			return undefined;
		}
		if (value instanceof Error) {
			const message = value.message.trim();
			const detailMessage = extractStructuredDetail(value);
			const cause = (value as { cause?: unknown }).cause;
			const causeMessage = extractStructuredMessage(cause);

			// 通用包装器（例如 "No output generated..."）只作为
			// 回退——改为呈现底层的 detail/cause。
			if (message && isGenericWrapperMessage(message)) {
				return detailMessage ?? causeMessage ?? undefined;
			}

			// 直接附着在错误上的结构化 provider 详情
			// （responseBody/detail/error 字段）比平淡的
			// 顶层 Error 消息更有用。
			if (detailMessage && detailMessage !== message) {
				return detailMessage;
			}

			// 否则保留包装器消息及其 cause，例如
			// "fetch failed: SocketError: other side closed (UND_ERR_SOCKET)"。
			if (causeMessage && message && causeMessage !== message) {
				const causeName =
					cause instanceof Error && cause.name && cause.name !== "Error"
						? `${cause.name}: `
						: "";
				const causeCode =
					cause && typeof cause === "object" && "code" in cause
						? (cause as { code?: unknown }).code
						: undefined;
				const codeSuffix =
					typeof causeCode === "string" && causeCode.trim()
						? ` (${causeCode})`
						: "";
				return `${message}: ${causeName}${causeMessage}${codeSuffix}`;
			}
			return causeMessage ?? (message || undefined);
		}

		const detail = extractStructuredDetail(value);
		if (detail) {
			return detail;
		}
		const payload = value as { cause?: unknown; message?: string };
		if ("cause" in payload && payload.cause !== value) {
			const nested = extractStructuredMessage(payload.cause);
			if (nested) {
				return nested;
			}
		}
		if (typeof payload.message === "string" && payload.message.trim()) {
			return payload.message;
		}
		return undefined;
	};

	const structuredMessage = extractStructuredMessage(error);
	if (structuredMessage) {
		return structuredMessage;
	}

	// 对普通对象调用 String() 会产生 "[object Object]"——JSON 至少
	// 对用户和下游错误分类更有可操作性。
	if (typeof error === "object" && error !== null) {
		const json = safeJsonStringify(error);
		if (json && json !== "{}" && json !== "null") {
			return json;
		}
	}
	return String(error);
}
