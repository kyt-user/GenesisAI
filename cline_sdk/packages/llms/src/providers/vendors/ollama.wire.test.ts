// Ollama vendor 的 wire 契约测试，通过*真实的*
// `ollama-ai-provider-v2` 包（经由
// `patches/ollama-ai-provider-v2@4.0.1.patch` 打补丁）执行，
// 而不是 mock 构造函数。每个测试用 stub fetch 通过 vendor 模块
// 驱动 `doStream`，并对实际的 `/api/chat` 请求体或解析后的流
// 进行断言，因此该依赖内部请求转换器和流解析器的回归
// 会在这里被捕获：
//
//   1. 未设置的思考设置必须从请求中*省略*（而不是以
//      `think: false` 发送），以便应用 Ollama 服务器默认值；
//   2. 流中途的 `{"error": ...}` 对象必须作为带 `error` finish reason
//      的错误流片段浮出——而不是被丢弃并报告为
//      干净完成；
//   3. 仅含附件的用户轮次必须将 `content` 序列化为字符串
//      （Ollama 声明 `Message.content` 为字符串），绝不能是 `[]`；
//   4. 工具结果必须携带文档所述的 `tool_name` 字段。
import type {
	LanguageModelV4CallOptions,
	LanguageModelV4Prompt,
	LanguageModelV4StreamPart,
} from "@ai-sdk/provider";
import type {
	GatewayProviderContext,
	GatewayResolvedProviderConfig,
} from "@cline/shared";
import { describe, expect, it } from "vitest";
import { createOllamaProviderModule } from "./ollama";

interface OllamaChatRequest {
	model: string;
	messages: Array<Record<string, unknown>>;
	think?: boolean;
	options?: Record<string, unknown>;
	[key: string]: unknown;
}

interface WireResult {
	/** vendor 发出的每个 `/api/chat` 请求的请求体。 */
	requests: OllamaChatRequest[];
	/** 向消费者浮出的所有流片段。 */
	parts: LanguageModelV4StreamPart[];
}

const DONE_CHUNK = {
	model: "test-model",
	created_at: "2024-01-01T00:00:00Z",
	done: true,
	done_reason: "stop",
	message: { role: "assistant", content: "" },
	prompt_eval_count: 1,
	eval_count: 1,
};

function textChunk(content: string): Record<string, unknown> {
	return {
		model: "test-model",
		created_at: "2024-01-01T00:00:00Z",
		done: false,
		message: { role: "assistant", content },
	};
}

/**
 * 针对预置的 NDJSON 响应，通过 vendor 模块运行一轮 `doStream`，
 * 捕获发出的请求体和结果流。
 */
async function streamThroughVendor({
	responseLines,
	prompt,
	providerOptions,
}: {
	responseLines: Array<Record<string, unknown>>;
	prompt: LanguageModelV4Prompt;
	providerOptions?: LanguageModelV4CallOptions["providerOptions"];
}): Promise<WireResult> {
	const requests: OllamaChatRequest[] = [];
	const fetchStub = (async (_input, init) => {
		requests.push(JSON.parse(init?.body as string));
		const body = `${responseLines.map((line) => JSON.stringify(line)).join("\n")}\n`;
		return new Response(body, {
			status: 200,
			headers: { "content-type": "application/x-ndjson" },
		});
	}) as typeof fetch;

	const module = await createOllamaProviderModule(
		{ providerId: "ollama", fetch: fetchStub } as GatewayResolvedProviderConfig,
		{
			provider: {
				id: "ollama",
				name: "Ollama",
				defaultModelId: "",
				models: [],
			},
			model: { id: "test-model", name: "test-model", providerId: "ollama" },
		} as unknown as GatewayProviderContext,
	);
	const model = module.operations.language("test-model");
	const result = await model.doStream({
		prompt,
		...(providerOptions ? { providerOptions } : {}),
	} as LanguageModelV4CallOptions);

	const parts: LanguageModelV4StreamPart[] = [];
	const reader = result.stream.getReader();
	while (true) {
		const { done, value } = await reader.read();
		if (done) {
			break;
		}
		parts.push(value);
	}
	return { requests, parts };
}

function userText(text: string): LanguageModelV4Prompt {
	return [{ role: "user", content: [{ type: "text", text }] }];
}

describe("ollama wire contract (real provider package)", () => {
	it("omits think from the request when no reasoning setting is given", async () => {
		const { requests } = await streamThroughVendor({
			responseLines: [textChunk("hi"), DONE_CHUNK],
			prompt: userText("hello"),
		});

		expect(requests).toHaveLength(1);
		// 省略 `think` 可以应用 Ollama 服务器默认值（对支持的模型
		// 自动思考）；`think: false` 会强制禁用它。
		expect(requests[0]).not.toHaveProperty("think");
	});

	it("passes explicit think settings through to the request body", async () => {
		const enabled = await streamThroughVendor({
			responseLines: [textChunk("hi"), DONE_CHUNK],
			prompt: userText("hello"),
			providerOptions: { ollama: { think: true } },
		});
		expect(enabled.requests[0].think).toBe(true);

		const disabled = await streamThroughVendor({
			responseLines: [textChunk("hi"), DONE_CHUNK],
			prompt: userText("hello"),
			providerOptions: { ollama: { think: false } },
		});
		expect(disabled.requests[0].think).toBe(false);
	});

	it("routes options.num_ctx through the provider bucket", async () => {
		const { requests } = await streamThroughVendor({
			responseLines: [textChunk("hi"), DONE_CHUNK],
			prompt: userText("hello"),
			providerOptions: { ollama: { options: { num_ctx: 65536 } } },
		});
		expect(requests[0].options).toEqual({ num_ctx: 65536 });
	});

	it("serializes an attachment-only user turn with string content", async () => {
		const { requests } = await streamThroughVendor({
			responseLines: [textChunk("hi"), DONE_CHUNK],
			prompt: [
				{
					role: "user",
					content: [
						{
							type: "file",
							mediaType: "image/png",
							data: { type: "data", data: "iVBORw0KGgoAAAANSUhEUg==" },
						},
					],
				},
			],
		});

		const [message] = requests[0].messages;
		expect(message.role).toBe("user");
		// Ollama 声明 `Message.content` 为字符串；`[]` 不符合契约。
		expect(message.content).toBe("");
		expect(message.images).toHaveLength(1);
	});

	it("includes tool_name on tool result messages", async () => {
		const { requests } = await streamThroughVendor({
			responseLines: [textChunk("done"), DONE_CHUNK],
			prompt: [
				...userText("read a file"),
				{
					role: "assistant",
					content: [
						{
							type: "tool-call",
							toolCallId: "call-1",
							toolName: "read_file",
							input: { path: "a.ts" },
						},
					],
				},
				{
					role: "tool",
					content: [
						{
							type: "tool-result",
							toolCallId: "call-1",
							toolName: "read_file",
							output: { type: "text", value: "file contents" },
						},
					],
				},
			],
		});

		const toolMessage = requests[0].messages.find(
			(message) => message.role === "tool",
		);
		expect(toolMessage).toMatchObject({
			tool_call_id: "call-1",
			tool_name: "read_file",
			content: "file contents",
		});
	});

	it("surfaces a mid-stream error object as an error part, not a clean finish", async () => {
		const { requests, parts } = await streamThroughVendor({
			responseLines: [
				textChunk("partial "),
				{ error: "model crashed while generating" },
			],
			prompt: userText("hello"),
		});

		const errorPart = parts.find((part) => part.type === "error");
		expect(errorPart).toBeDefined();
		expect((errorPart as { error: unknown }).error).toBe(
			"model crashed while generating",
		);

		const finishPart = parts.find((part) => part.type === "finish");
		expect(finishPart).toBeDefined();
		expect(
			(finishPart as { finishReason: { unified: string } }).finishReason
				.unified,
		).toBe("error");

		// 空响应重试中间件（集中应用于 `ai-sdk.ts`，
		// 位于此 vendor 模块之外）不得通过重新发出请求
		// 来掩盖失败；错误完成从不重试。
		expect(requests).toHaveLength(1);
	});
});
