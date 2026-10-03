/**
 * 用于 HTTP 请求的 VCR（录像机）。
 *
 * 补丁 `globalThis.fetch` 以录制和回放 HTTP 交互，
 * 实现无真实 API 调用的确定性测试。
 *
 * 与 nock（补丁 Node 的 `http` 模块）不同，此实现直接包装
 * `globalThis.fetch`，捕获此代码库中的所有 HTTP 流量，
 * 包括通过 OpenAI、Anthropic、Gemini 和 Vercel AI
 * SDK 发出的调用（它们都委托给全局 fetch）。
 *
 * 环境变量：
 *   CLINE_VCR           - "record" 录制 HTTP 请求，"playback" 回放它们
 *   CLINE_VCR_CASSETTE  - cassette 文件路径（默认：./vcr-cassette.json）
 *   CLINE_VCR_FILTER    - 过滤录制/回放请求路径的子串。
 *                         设为非空字符串时，只有路径包含此子串的请求
 *                         被录制/回放；所有其他请求透传到真实网络。
 *                         为空或未设置时，所有请求被拦截（无过滤）。
 *   CLINE_VCR_INCLUDE_REQUEST_BODY - "1" 保存脱敏后的请求体并在回放时
 *                         断言它们。
 *   CLINE_VCR_SSE_DELAY - 回放时 SSE 块之间的毫秒数（默认：100）。
 *                         设为 0 即时交付。
 *
 * 用法：
 *   # 只录制推理请求
 *   CLINE_VCR=record CLINE_VCR_CASSETTE=./fixtures/my-test.json cline task "hello"
 *
 *   # 回放：auth/S3/等请求正常透传，只有推理被 mock
 *   CLINE_VCR=playback CLINE_VCR_CASSETTE=./fixtures/my-test.json cline task "hello"
 *
 *   # 录制一切（无过滤）
 *   CLINE_VCR=record CLINE_VCR_FILTER="" CLINE_VCR_CASSETTE=./fixtures/all.json cline task "hello"
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { registerDisposable } from "./dispose";
import type { VcrRecording } from "./types/vcr";

// ── 类型 ───────────────────────────────────────────────────────────────

type VcrMode = "record" | "playback";

interface VcrConfig {
	mode: VcrMode;
	cassettePath: string;
	includeRequestBody: boolean;
	/**
	 * 仅录制/回放路径包含此子串的请求。
	 * 空字符串（""）表示不过滤，所有请求被拦截。
	 * 非空字符串启用选择性模式，只有匹配的请求
	 * 被拦截，不匹配的请求透传到真实网络。
	 */
	filter: string;
}

interface InternalVcrRecording extends VcrRecording {
	requestContentType?: string;
}

// ── 敏感数据脱敏 ─────────────────────────────────────────────────────────

/**
 * 脱敏基于键：名称匹配规则的 JSON 键的值会被编辑。
 * 这比正则匹配值更稳健，因为它不依赖于值格式。
 *
 * 三类键被编辑：
 *
 * 1. 精确键名（不区分大小写）：secrets、tokens、credentials。
 * 2. 键名模式（子串/后缀）：捕获 ID 字段、PII 等。
 * 3. 值级正则模式：处理嵌入在纯字符串中的值
 *    （如文件系统路径、URL 中的 AWS 密钥 ID）。
 *
 * 要添加新的脱敏规则，只需向下面的集合/数组中添加条目。
 */

/** 值始终被完全脱敏的键（不区分大小写的精确匹配）。 */
const REDACT_KEYS_EXACT = new Set([
	// 密钥和令牌
	// 精确键在小写化后比较，因此 accessToken 匹配 accesstoken。
	"accesskeyid",
	"secretaccesskey",
	"idtoken",
	"refreshtoken",
	"accesstoken",
	"access_token",
	"refresh_token",
	"apikey",
	"api_key",
	"authorization",
	"password",
	"secret",
	"token",
	// PII
	"email",
	"displayname",
	"display_name",
	"userinfo",
]);

/**
 * 当键名以这些子串之一结尾或包含它们时（不区分大小写），
 * 其值会被脱敏。捕获如 "userId"、"organizationId"、"memberId"、
 * "sessionId" 等字段。
 */
const REDACT_KEY_SUFFIXES = [
	"id", // 匹配 *Id 和 *_id，覆盖大多数实体标识符
	"balance",
	"cost",
	"secret",
];

/** 检查键名是否应脱敏其值。 */
function shouldRedactKey(key: string): boolean {
	const lower = key.toLowerCase();
	if (REDACT_KEYS_EXACT.has(lower)) {
		return true;
	}
	for (const suffix of REDACT_KEY_SUFFIXES) {
		// 匹配 "userId"、"user_id"、"id" 但不匹配 "video" 或 "valid"
		if (lower === suffix) {
			return true;
		}
		// camelCase：以 "Id"、"Balance" 等结尾
		if (lower.endsWith(suffix) && lower.length > suffix.length) {
			const charBefore = lower[lower.length - suffix.length - 1];
			// 必须跟在单词边界字符之后（_、- 或大写转换）
			if (charBefore === "_" || charBefore === "-") {
				return true;
			}
			// camelCase：后缀以小写开头但原始键有大写
			const originalChar = key[key.length - suffix.length];
			if (
				originalChar &&
				originalChar === originalChar.toUpperCase() &&
				originalChar !== originalChar.toLowerCase()
			) {
				return true;
			}
		}
		// snake_case：以 "_id"、"_balance" 等结尾
		if (lower.endsWith(`_${suffix}`)) {
			return true;
		}
	}
	return false;
}

/** 应用于纯字符串值的正则模式（非基于键）。 */
const SENSITIVE_VALUE_PATTERNS: { pattern: RegExp; replacement: string }[] = [
	// AWS 访问密钥 ID
	{ pattern: /AKIA[A-Z0-9]{16}/g, replacement: "AKIA_REDACTED" },
	// 带用户名的文件系统路径
	{ pattern: /\/Users\/[A-Za-z0-9._-]+/g, replacement: "/Users/REDACTED_USER" },
	{ pattern: /\/home\/[A-Za-z0-9._-]+/g, replacement: "/home/REDACTED_USER" },
];

/** 对纯字符串应用值级正则脱敏。 */
function sanitizeStringValue(input: string): string {
	let result = input;
	for (const { pattern, replacement } of SENSITIVE_VALUE_PATTERNS) {
		result = result.replace(pattern, replacement);
	}
	return result;
}

function sortJsonValue(value: unknown): unknown {
	if (Array.isArray(value)) {
		return value.map(sortJsonValue);
	}
	if (!value || typeof value !== "object") {
		return value;
	}
	return Object.fromEntries(
		Object.entries(value as Record<string, unknown>)
			.sort(([a], [b]) => compareCodeUnits(a, b))
			.map(([key, nestedValue]) => [key, sortJsonValue(nestedValue)]),
	);
}

function compareCodeUnits(a: string, b: string): number {
	if (a < b) {
		return -1;
	}
	if (a > b) {
		return 1;
	}
	return 0;
}

function canonicalStringify(value: unknown): string {
	return JSON.stringify(sortJsonValue(value));
}

/**
 * 用于规范化录制中请求路径的路径级模式。
 * 这些模式将动态路径段替换为稳定的测试值，使
 * 回放匹配在不同环境/用户之间都能工作。
 *
 * 模式按顺序应用。更具体的模式应排在前面。
 */
const PATH_NORMALIZATION_PATTERNS: { pattern: RegExp; replacement: string }[] =
	[
		// S3 风格的任务产物路径：/tasks/<userId>/<taskId>/api_conversation_history.json
		{
			pattern:
				/tasks\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+\/api_conversation_history/g,
			replacement: "tasks/usr-test/taskid/api_conversation_history",
		},
		// 路径段中带前缀的实体 ID（org-XXX、usr-XXX、mbr-XXX、ses-XXX 等）
		// 匹配合常见 Cline ID 格式：前缀 + ULID/UUID 类后缀
		{
			pattern:
				/\/(org|usr|mbr|ses|gen|req|msg|tsk|sch|exe|srv|cli|wkr|evt|sub|tkn)-[A-Za-z0-9]{10,}(?=[/?#]|$)/g,
			replacement: "/$1-REDACTED",
		},
	];

/** 规范化请求路径以进行稳定匹配。 */
function normalizePath(input: string): string {
	let result = input;
	for (const { pattern, replacement } of PATH_NORMALIZATION_PATTERNS) {
		result = result.replace(pattern, replacement);
	}
	return result;
}

/**
 * 深度脱敏值，编辑敏感键和模式。
 * 处理对象、数组、纯字符串和 JSON 编码字符串。
 */
function sanitizeValue(obj: unknown): unknown {
	if (obj === null || obj === undefined) {
		return obj;
	}

	if (typeof obj === "string") {
		// 尝试解析为 JSON 并递归脱敏
		try {
			const parsed = JSON.parse(obj);
			if (typeof parsed === "object" && parsed !== null) {
				return JSON.stringify(sanitizeValue(parsed));
			}
		} catch {
			// 非 JSON，应用字符串级模式
		}
		return sanitizeStringValue(obj);
	}

	if (Array.isArray(obj)) {
		return obj.map(sanitizeValue);
	}

	if (typeof obj === "object") {
		const result: Record<string, unknown> = {};
		for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
			if (
				shouldRedactKey(key) &&
				(typeof value === "string" || typeof value === "number")
			) {
				result[key] = "REDACTED";
			} else {
				result[key] = sanitizeValue(value);
			}
		}
		return result;
	}

	return obj;
}

function parseUrlEncodedBody(
	input: string,
): Record<string, unknown> | undefined {
	const params = new URLSearchParams(input);
	const entries = Array.from(params.entries());
	if (entries.length === 0 || entries.some(([key]) => key.length === 0)) {
		return undefined;
	}
	const output: Record<string, unknown> = {};
	for (const [key, value] of entries) {
		const existing = output[key];
		if (existing === undefined) {
			output[key] = value;
		} else if (Array.isArray(existing)) {
			existing.push(value);
		} else {
			output[key] = [existing, value];
		}
	}
	return output;
}

function isUrlEncodedContentType(contentType: string | undefined): boolean {
	return (
		contentType?.toLowerCase().split(";")[0]?.trim() ===
		"application/x-www-form-urlencoded"
	);
}

function sanitizeSerializedRequestBody(
	input: string,
	contentType?: string,
): string {
	try {
		return canonicalStringify(sanitizeValue(JSON.parse(input)));
	} catch {
		if (isUrlEncodedContentType(contentType)) {
			const formBody = parseUrlEncodedBody(input);
			if (formBody) {
				return canonicalStringify(sanitizeValue(formBody));
			}
		}
		return sanitizeStringValue(input);
	}
}

/** 脱敏单个录制的交互，剥离敏感数据。 */
function sanitizeRecording(
	rec: InternalVcrRecording,
	includeRequestBody: boolean,
): VcrRecording {
	const cleaned = { ...rec };
	const requestBody =
		includeRequestBody && rec.body !== undefined
			? sanitizeSerializedRequestBody(rec.body, rec.requestContentType)
			: undefined;

	// 移除请求体（可能包含提示词、API 密钥等）
	delete cleaned.body;
	delete cleaned.requestContentType;
	if (requestBody !== undefined) {
		cleaned.requestBody = requestBody;
	}

	// 规范化请求路径以实现稳定匹配
	if (typeof cleaned.path === "string") {
		cleaned.path = normalizePath(cleaned.path);
	}

	// 深度脱敏响应体
	if (cleaned.response !== undefined) {
		cleaned.response = sanitizeValue(cleaned.response);
	}

	return cleaned;
}

// ── URL helpers ─────────────────────────────────────────────────────────

function parseScope(url: string): { scope: string; path: string } {
	try {
		const parsed = new URL(url);
		const scope = `${parsed.protocol}//${parsed.host}`;
		const path = parsed.pathname + parsed.search;
		return { scope, path };
	} catch {
		return { scope: "", path: url };
	}
}

function resolveRequestUrl(input: string | URL | Request): string {
	if (typeof input === "string") {
		return input;
	}
	if (input instanceof URL) {
		return input.toString();
	}
	if (input && typeof (input as Request).url === "string") {
		return (input as Request).url;
	}
	return String(input);
}

function resolveRequestMethod(
	input: string | URL | Request,
	init?: RequestInit,
): string {
	if (init?.method) {
		return init.method.toUpperCase();
	}
	if (input && typeof (input as Request).method === "string") {
		return (input as Request).method.toUpperCase();
	}
	return "GET";
}

function readHeadersContentType(
	headers: RequestInit["headers"] | undefined,
): string | undefined {
	if (!headers) {
		return undefined;
	}
	return new Headers(headers).get("content-type") ?? undefined;
}

function readRequestContentType(
	input: string | URL | Request,
	init?: RequestInit,
): string | undefined {
	const initContentType = readHeadersContentType(init?.headers);
	if (initContentType) {
		return initContentType;
	}
	if (init?.body instanceof URLSearchParams) {
		return "application/x-www-form-urlencoded;charset=UTF-8";
	}
	if (input instanceof Request) {
		return input.headers.get("content-type") ?? undefined;
	}
	return undefined;
}

async function readRequestBody(
	input: string | URL | Request,
	init?: RequestInit,
): Promise<string | undefined> {
	if (init?.body) {
		if (typeof init.body === "string") {
			return init.body;
		}
		if (init.body instanceof URLSearchParams) {
			return init.body.toString();
		}
		if (init.body instanceof ArrayBuffer) {
			return new TextDecoder().decode(init.body);
		}
		if (ArrayBuffer.isView(init.body)) {
			return new TextDecoder().decode(
				new Uint8Array(
					init.body.buffer,
					init.body.byteOffset,
					init.body.byteLength,
				),
			);
		}
		return undefined;
	}
	if (input instanceof Request) {
		try {
			return await input.clone().text();
		} catch {
			return undefined;
		}
	}
	return undefined;
}

// ── Config resolution ───────────────────────────────────────────────────

function getVcrConfig(vcrMode: string | undefined): VcrConfig | null {
	if (!vcrMode) {
		return null;
	}

	if (!process.env.CLINE_VCR_CASSETTE) {
		process.stderr.write(
			"[VCR] No CLINE_VCR_CASSETTE: requests will not be recorded or played back.\n",
		);
		return null;
	}

	if (vcrMode !== "record" && vcrMode !== "playback") {
		process.stderr.write(
			`[VCR] Invalid CLINE_VCR value: "${vcrMode}". Expected "record" or "playback".\n`,
		);
		process.exit(1);
	}

	const cassettePath = resolve(process.env.CLINE_VCR_CASSETTE);
	const filter = process.env.CLINE_VCR_FILTER ?? "";
	const includeRequestBody =
		process.env.CLINE_VCR_INCLUDE_REQUEST_BODY === "1" ||
		process.env.CLINE_VCR_INCLUDE_REQUEST_BODY === "true";

	return { mode: vcrMode, cassettePath, filter, includeRequestBody };
}

// ── Record mode ─────────────────────────────────────────────────────────

/** 可进行中的流捕获，可同步终结。 */
interface InFlightCapture {
	scope: string;
	method: string;
	path: string;
	body: string;
	requestContentType?: string;
	status: number;
	contentType: string | undefined;
	chunks: Uint8Array[];
	finalized: boolean;
}

function startRecordingRequests(
	cassettePath: string,
	filter: string,
	includeRequestBody: boolean,
): void {
	const recordings: InternalVcrRecording[] = [];
	/** 仍在被消费的流，在 flush 或进程退出时最终确定。 */
	const inFlight: InFlightCapture[] = [];
	const originalFetch = globalThis.fetch;

	/** 将累积的块转换为录制条目。 */
	function finalizeCapture(capture: InFlightCapture): void {
		if (capture.finalized) {
			return;
		}
		capture.finalized = true;

		const decoder = new TextDecoder();
		const bodyText =
			capture.chunks.map((c) => decoder.decode(c, { stream: true })).join("") +
			decoder.decode();

		let responseBody: unknown;
		try {
			responseBody = JSON.parse(bodyText);
		} catch {
			responseBody = bodyText;
		}

		recordings.push({
			scope: capture.scope,
			method: capture.method,
			path: capture.path,
			body: capture.body,
			requestContentType: capture.requestContentType,
			status: capture.status,
			response: responseBody,
			responseIsBinary: false,
			contentType: capture.contentType,
		});
	}

	globalThis.fetch = Object.assign(
		async (
			input: string | URL | Request,
			init?: RequestInit,
		): Promise<Response> => {
			const url = resolveRequestUrl(input);
			const method = resolveRequestMethod(input, init);
			const { scope, path } = parseScope(url);

			const requestBody = await readRequestBody(input, init);
			const requestContentType = readRequestContentType(input, init);

			// 调用真实的 fetch
			const response = await originalFetch(input, init);

			// 检查过滤器
			if (filter && !path.includes(filter)) {
				return response;
			}

			// 从真实响应捕获 content-type
			const contentType = response.headers.get("content-type") ?? undefined;

			// 无 body，因此立即录制
			if (!response.body) {
				recordings.push({
					scope,
					method,
					path,
					body: requestBody ?? "",
					requestContentType,
					status: response.status,
					response: "",
					responseIsBinary: false,
					contentType,
				});
				return response;
			}

			// 用 TransformStream 包装响应体，在调用方消费时捕获块。
			// 捕获记录被跟踪在 `inFlight` 中，这样即使流尚未完成
			//（例如 SSE 期间调用 process.exit()），退出处理器也能最终确定它。
			const capture: InFlightCapture = {
				scope,
				method,
				path,
				body: requestBody ?? "",
				requestContentType,
				status: response.status,
				contentType,
				chunks: [],
				finalized: false,
			};
			inFlight.push(capture);

			const originalBody = response.body;
			const transform = new TransformStream<Uint8Array, Uint8Array>({
				transform(chunk, controller) {
					capture.chunks.push(chunk);
					controller.enqueue(chunk);
				},
				flush() {
					finalizeCapture(capture);
				},
			});

			const wrappedBody = originalBody.pipeThrough(transform);

			return new Response(wrappedBody, {
				status: response.status,
				statusText: response.statusText,
				headers: response.headers,
			});
		},
		{ preconnect: (_url: string | URL) => {} },
	);

	const filterDesc = filter ? `matching path "*${filter}*"` : "all paths";
	process.stderr.write(
		`[VCR] Recording HTTP requests (${filterDesc}). Cassette will be saved to: ${cassettePath}\n`,
	);

	// 保存录制，先最终确定所有进行中的流捕获
	let saved = false;
	const saveRecordings = () => {
		if (saved) {
			return;
		}
		saved = true;

		// 恢复原始 fetch
		globalThis.fetch = originalFetch;

		// 用目前已接收的数据最终确定所有进行中的流捕获
		//（对于调用 process.exit() 时可能仍处于打开状态的 SSE 流至关重要）。
		for (const capture of inFlight) {
			finalizeCapture(capture);
		}

		if (recordings.length === 0) {
			process.stderr.write(
				`[VCR] No HTTP requests${filter ? ` matching "${filter}"` : ""} were recorded.\n`,
			);
			return;
		}

		const dir = dirname(cassettePath);
		mkdirSync(dir, { recursive: true });

		const sanitized = recordings.map((recording) =>
			sanitizeRecording(recording, includeRequestBody),
		);
		writeFileSync(cassettePath, JSON.stringify(sanitized, null, 2));
		process.stderr.write(
			`[VCR] Saved ${sanitized.length} recorded HTTP interaction(s) to ${cassettePath}\n`,
		);
	};

	registerDisposable(saveRecordings);
}

// ── Playback mode ───────────────────────────────────────────────────────

/**
 * 将 SSE 响应体拆分为独立的事件块。
 * 每个块是一个完整的 "data: ...\n\n" 片段。
 */
function splitSseChunks(body: string): string[] {
	// 按分隔 SSE 事件的双换行边界拆分
	const chunks: string[] = [];
	const parts = body.split(/\n\n/);
	for (const part of parts) {
		const trimmed = part.trim();
		if (trimmed) {
			chunks.push(`${trimmed}\n\n`);
		}
	}
	return chunks;
}

/**
 * 创建一个 ReadableStream，以块之间的延迟交付 SSE 块。
 */
function createDelayedSseStream(
	chunks: string[],
	delayMs: number,
): ReadableStream<Uint8Array> {
	const encoder = new TextEncoder();
	let index = 0;

	return new ReadableStream<Uint8Array>({
		async pull(controller) {
			if (index >= chunks.length) {
				controller.close();
				return;
			}
			if (index > 0 && delayMs > 0) {
				await new Promise((resolve) => setTimeout(resolve, delayMs));
			}
			const chunk = chunks[index];
			if (chunk === undefined) {
				controller.close();
				return;
			}
			controller.enqueue(encoder.encode(chunk));
			index += 1;
		},
	});
}

async function assertRequestBodyMatches(input: {
	recording: VcrRecording;
	requestInput: string | URL | Request;
	requestInit?: RequestInit;
	method: string;
	path: string;
}): Promise<void> {
	if (input.recording.requestBody === undefined) {
		return;
	}
	const requestBody = await readRequestBody(
		input.requestInput,
		input.requestInit,
	);
	const requestContentType = readRequestContentType(
		input.requestInput,
		input.requestInit,
	);
	const actualBody = sanitizeSerializedRequestBody(
		requestBody ?? "",
		requestContentType,
	);
	if (actualBody !== input.recording.requestBody) {
		throw new Error(
			`[VCR] Request body mismatch for ${input.method} ${input.path}.\n` +
				`  expected: ${input.recording.requestBody}\n` +
				`  actual:   ${actualBody}\n` +
				"Re-record the cassette if the request change is intentional.",
		);
	}
}

function startPlayingBackRequests(cassettePath: string, filter: string): void {
	if (!existsSync(cassettePath)) {
		process.stderr.write(`[VCR] Cassette file not found: ${cassettePath}\n`);
		process.exit(1);
	}

	const recordings: VcrRecording[] = JSON.parse(
		readFileSync(cassettePath, "utf-8"),
	);

	const sseDelayMs = Number.parseInt(
		process.env.CLINE_VCR_SSE_DELAY ?? "100",
		10,
	);

	// 跟踪哪些录制已被消费（每个只能用一次）
	const consumed = new Array<boolean>(recordings.length).fill(false);
	const originalFetch = globalThis.fetch;

	globalThis.fetch = Object.assign(
		async (
			input: string | URL | Request,
			init?: RequestInit,
		): Promise<Response> => {
			const url = resolveRequestUrl(input);
			const method = resolveRequestMethod(input, init);
			const { path } = parseScope(url);
			const normalizedPath = normalizePath(path);

			// 检查过滤器：若设置了过滤器且路径不匹配，则透传
			if (filter && !path.includes(filter)) {
				return originalFetch(input, init);
			}

			// 查找匹配的未消费录制
			const matchIndex = recordings.findIndex((rec, index) => {
				if (consumed[index]) {
					return false;
				}
				// 按方法 + 规范化路径匹配。Scope 检查较为宽松
				//（录制和回放环境的主机名可能不同）。
				const recNormalizedPath = normalizePath(rec.path);
				return (
					rec.method.toUpperCase() === method &&
					recNormalizedPath === normalizedPath
				);
			});

			if (matchIndex >= 0) {
				const rec = recordings[matchIndex];
				if (!rec) {
					return originalFetch(input, init);
				}
				await assertRequestBodyMatches({
					recording: rec,
					requestInput: input,
					requestInit: init,
					method,
					path: normalizedPath,
				});
				consumed[matchIndex] = true;

				// 构建响应体
				const body =
					typeof rec.response === "string"
						? rec.response
						: JSON.stringify(rec.response);

				// 可用时使用录制的内容类型，否则从响应结构推断
				const headers = new Headers();
				if (rec.contentType) {
					headers.set("content-type", rec.contentType);
				} else {
					// 对捕获 contentType 之前录制的 cassette 的备用启发式方法
					const isSSE =
						typeof rec.response === "string" &&
						rec.response.trimStart().startsWith("data:");
					if (isSSE) {
						headers.set("content-type", "text/event-stream");
					} else if (typeof rec.response === "object") {
						headers.set("content-type", "application/json");
					}
				}

				const isSSEResponse =
					headers.get("content-type")?.includes("text/event-stream") ?? false;

				// SSE responses need streaming-friendly headers
				if (isSSEResponse) {
					headers.set("cache-control", "no-cache");
					headers.set("connection", "keep-alive");
				}

				// 对于 SSE 响应，以延迟流式传输块以模拟
				// 实时交付（由 CLINE_VCR_SSE_DELAY 控制）。
				if (isSSEResponse && typeof rec.response === "string") {
					const chunks = splitSseChunks(rec.response);
					if (chunks.length > 1) {
						const stream = createDelayedSseStream(chunks, sseDelayMs);
						return new Response(stream, {
							status: rec.status,
							headers,
						});
					}
				}

				return new Response(body, {
					status: rec.status,
					headers,
				});
			}

			// 未找到匹配
			if (!filter) {
				// 完全隔离模式，因此无过滤器意味着不应泄漏任何内容
				throw new Error(
					`[VCR] No matching recording for ${method} ${url} (path: ${normalizedPath}). ` +
						`${recordings.length} recording(s) loaded from ${cassettePath}.`,
				);
			}

			// Filtered mode, passthrough non-matching requests
			return originalFetch(input, init);
		},
		{ preconnect: (_url: string | URL) => {} },
	);

	const filterDesc = filter
		? `(only paths matching "*${filter}*", all other requests go through normally)`
		: "(all requests intercepted)";
	process.stderr.write(
		`[VCR] Playing back ${recordings.length} recorded HTTP interaction(s) from ${cassettePath} ${filterDesc}\n`,
	);
}

// ── Public API ──────────────────────────────────────────────────────────

/**
 * Initialize VCR mode based on environment variables.
 * Must be called early in startup, before HTTP requests are made.
 *
 * Does nothing if `CLINE_VCR` is not set.
 */
export function initVcr(vcrMode: string | undefined): void {
	const config = getVcrConfig(vcrMode);
	if (!config) {
		return;
	}

	if (config.mode === "record") {
		startRecordingRequests(
			config.cassettePath,
			config.filter,
			config.includeRequestBody,
		);
	} else {
		startPlayingBackRequests(config.cassettePath, config.filter);
	}
}
