import { beforeEach, describe, expect, it, vi } from "vitest";
import {
	_testing,
	createGatewayApiHandler,
	createGatewayApiHandlerAsync,
	toGatewayRequestMessages,
} from "./compat";
import { ClineNotSubscribedError } from "./errors";
import { DEFAULT_GATEWAY_MAX_OUTPUT_FRACTION } from "./gateway";
import type { Message } from "./types";

const streamTextSpy = vi.fn();
const openaiCompatibleFactorySpy = vi.fn();
const openaiCompatibleSpy = vi.fn((modelId: string) => ({
	modelId,
	family: "openai-compatible",
}));

vi.mock("ai", () => ({
	jsonSchema: (schema: unknown, options: unknown) => ({
		jsonSchema: schema,
		...(options && typeof options === "object" ? options : {}),
	}),
	streamText: (input: unknown) => streamTextSpy(input),
	wrapLanguageModel: ({ model }: { model: unknown }) => model,
}));

vi.mock("@ai-sdk/openai-compatible", () => ({
	createOpenAICompatible: (config: unknown) => {
		openaiCompatibleFactorySpy(config);
		return (modelId: string) => openaiCompatibleSpy(modelId);
	},
}));

vi.mock("@ai-sdk/openai", () => ({
	createOpenAI: () => ({
		responses: (modelId: string) => ({ modelId, family: "openai" }),
	}),
}));

vi.mock("@ai-sdk/anthropic", () => ({
	createAnthropic: () => (modelId: string) => ({
		modelId,
		family: "anthropic",
	}),
}));

vi.mock("@ai-sdk/google", () => ({
	createGoogleGenerativeAI: () => (modelId: string) => ({
		modelId,
		family: "google",
	}),
}));

vi.mock("ai-sdk-provider-codex-cli", () => ({
	createCodexExec: () => (modelId: string) => ({
		modelId,
		family: "openai-codex",
	}),
}));

describe("createGatewayApiHandler.getMessages", () => {
	it("preserves structured tool_result content for gateway requests", () => {
		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "test-model",
			apiKey: "test-key",
		});

		const messages: Message[] = [
			{
				role: "assistant",
				content: [
					{
						type: "tool_use",
						id: "toolu_1",
						name: "run_commands",
						input: { commands: ["echo hello"] },
					},
				],
			},
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "toolu_1",
						content: [
							{
								query: "echo hello",
								result: "hello\n",
								success: true,
							},
						],
					},
				],
			},
		];

		const request = handler.getMessages("", messages) as {
			messages: Array<{
				role: string;
				content: Array<Record<string, unknown>>;
			}>;
		};

		expect(request.messages).toHaveLength(2);
		expect(request.messages[1]).toMatchObject({
			role: "user",
			content: [
				{
					type: "tool-result",
					toolCallId: "toolu_1",
					toolName: "run_commands",
					output: [
						{
							query: "echo hello",
							result: "hello\n",
							success: true,
						},
					],
					isError: false,
				},
			],
		});
	});

	it("normalizes mixed legacy blocks and structured tool results", () => {
		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "test-model",
			apiKey: "test-key",
		});

		const messages: Message[] = [
			{
				role: "assistant",
				content: [
					{
						type: "tool_use",
						id: "toolu_2",
						name: "run_commands",
						input: { commands: ["pwd"] },
					},
				],
			},
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "toolu_2",
						content: [
							{ type: "text", text: "Command output:" },
							{
								query: "pwd",
								result: "/tmp/project\n",
								success: true,
							},
							{ type: "file", path: "/tmp/log.txt", content: "log line" },
						],
					},
				],
			},
		];

		const request = handler.getMessages("", messages) as {
			messages: Array<{
				role: string;
				content: Array<Record<string, unknown>>;
			}>;
		};

		// `toGatewayRequestMessages` 现在原样转发原始 `tool_result.content`，
		// 不做改动。下游 `formatMessagesForAiSdk` /
		// `toAiSdkToolResultOutput` 负责任何展平或
		// 图片提取；本层只把 Cline 的 `Message[]`
		// 形状翻译为 AI-SDK 格式化器片段。
		expect(request.messages[1]).toMatchObject({
			role: "user",
			content: [
				{
					type: "tool-result",
					toolCallId: "toolu_2",
					toolName: "run_commands",
					output: [
						{ type: "text", text: "Command output:" },
						{
							query: "pwd",
							result: "/tmp/project\n",
							success: true,
						},
						{ type: "file", path: "/tmp/log.txt", content: "log line" },
					],
					isError: false,
				},
			],
		});
	});

	it("forwards nested images inside structured tool results to the AI SDK formatter", () => {
		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "test-model",
			apiKey: "test-key",
		});

		const messages: Message[] = [
			{
				role: "assistant",
				content: [
					{
						type: "tool_use",
						id: "toolu_3",
						name: "read_files",
						input: { file_paths: ["/tmp/demo.png"] },
					},
				],
			},
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "toolu_3",
						content: [
							{
								query: "/tmp/demo.png",
								success: true,
								result: [
									{ type: "text", text: "Successfully read image" },
									{
										type: "image",
										data: "YWJj",
										mediaType: "image/png",
									},
								],
							},
						],
					},
				],
			},
		];

		const request = handler.getMessages("", messages) as {
			messages: Array<{
				role: string;
				content: Array<Record<string, unknown>>;
			}>;
		};

		// compat 层不再把图片拆分到相邻的 user
		// 消息——该职责已移入
		// `toAiSdkToolResultOutput`，它把每个嵌套的 `image`
		// 内容块提取为原生 `image-data` 内容片段。
		// 因此 gateway 请求包含原始的
		// `ToolOperationResult[]` 内容不变，图片块
		// 仍嵌在 `result` 内部。
		expect(request.messages[1]).toMatchObject({
			role: "user",
			content: [
				{
					type: "tool-result",
					toolCallId: "toolu_3",
					toolName: "read_files",
					output: [
						{
							query: "/tmp/demo.png",
							success: true,
							result: [
								{ type: "text", text: "Successfully read image" },
								{
									type: "image",
									data: "YWJj",
									mediaType: "image/png",
								},
							],
						},
					],
					isError: false,
				},
			],
		});
		expect(request.messages[1]?.content).toHaveLength(1);
	});

	it("preserves is_error for structured tool results", () => {
		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "test-model",
			apiKey: "test-key",
		});

		const messages: Message[] = [
			{
				role: "assistant",
				content: [
					{
						type: "tool_use",
						id: "toolu_4",
						name: "run_commands",
						input: { commands: ["false"] },
					},
				],
			},
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "toolu_4",
						is_error: true,
						content: [
							{
								query: "false",
								result: "",
								error: "Command failed: exit 1",
								success: false,
							},
						],
					},
				],
			},
		];

		const request = handler.getMessages("", messages) as {
			messages: Array<{
				role: string;
				content: Array<Record<string, unknown>>;
			}>;
		};

		expect(request.messages[1]).toMatchObject({
			role: "user",
			content: [
				{
					type: "tool-result",
					toolCallId: "toolu_4",
					toolName: "run_commands",
					output: [
						{
							query: "false",
							result: "",
							error: "Command failed: exit 1",
							success: false,
						},
					],
					isError: true,
				},
			],
		});
	});
});

describe("createGatewayApiHandler.createMessage", () => {
	beforeEach(() => {
		streamTextSpy.mockReset();
		openaiCompatibleFactorySpy.mockReset();
		openaiCompatibleSpy.mockClear();
	});

	it.each([
		["openai-responses", "openai"],
		["anthropic", "anthropic"],
	])("retains live model protocol %s through the handler", async (apiProtocol, family) => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
		});
		const handler = createGatewayApiHandler({
			providerId: "opencode-go",
			modelId: "new-live-model",
			apiKey: "test-key",
			knownModels: {
				"new-live-model": { id: "new-live-model", metadata: { apiProtocol } },
			},
		});
		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 演练实时目录 -> handler -> gateway 的投影。
		}
		expect(streamTextSpy.mock.calls.at(-1)?.[0].model).toMatchObject({
			modelId: "new-live-model",
			family,
		});
	});

	it("defaults maxOutputTokens to a fraction of the catalog maxTokens", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});

		const handler = createGatewayApiHandler({
			providerId: "openrouter",
			clientType: "openai-compatible",
			modelId: "z-ai/glm-5.1",
			apiKey: "test-key",
			knownModels: {
				"z-ai/glm-5.1": {
					id: "z-ai/glm-5.1",
					name: "GLM 5.1",
					contextWindow: 202_800,
					maxInputTokens: 202_800,
					maxTokens: 202_800,
					capabilities: ["tools"],
				},
			},
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 请求被执行。
		}

		const call = streamTextSpy.mock.calls.at(-1)?.[0] as
			| { maxOutputTokens?: unknown }
			| undefined;
		expect(call).toHaveProperty(
			"maxOutputTokens",
			Math.floor(202_800 * DEFAULT_GATEWAY_MAX_OUTPUT_FRACTION),
		);
	});

	it("conservatively normalizes exotic effort for unlisted OpenRouter models", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});

		const handler = createGatewayApiHandler({
			providerId: "openrouter",
			clientType: "openai-compatible",
			modelId: "reasoning-model",
			apiKey: "test-key",
			thinking: true,
			reasoningEffort: "max",
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 请求被执行。
		}

		expect(streamTextSpy).toHaveBeenCalledWith(
			expect.objectContaining({
				reasoning: "xhigh",
			}),
		);
	});

	it("sends configured OpenAI-compatible maxOutputTokens to the provider request", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});

		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "custom-model",
			apiKey: "test-key",
			baseUrl: "https://example.com/v1",
			maxOutputTokens: 4_096,
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 请求被执行。
		}

		expect(streamTextSpy).toHaveBeenCalledWith(
			expect.objectContaining({
				maxOutputTokens: 4_096,
			}),
		);
	});

	it("caps configured maxOutputTokens with the catalog model output limit", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});

		const handler = createGatewayApiHandler({
			providerId: "openrouter",
			clientType: "openai-compatible",
			modelId: "small-output",
			apiKey: "test-key",
			maxOutputTokens: 16_000,
			knownModels: {
				"small-output": {
					id: "small-output",
					name: "Small Output",
					contextWindow: 202_800,
					maxInputTokens: 202_800,
					maxTokens: 8_192,
					capabilities: ["tools"],
				},
			},
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 请求被执行。
		}

		expect(streamTextSpy).toHaveBeenCalledWith(
			expect.objectContaining({
				maxOutputTokens: 8_192,
			}),
		);
	});

	it("strips legacy thinking history before sending Cerebras requests", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});

		const handler = createGatewayApiHandler({
			providerId: "cerebras",
			modelId: "zai-glm-4.7",
			apiKey: "test-key",
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "hello" },
			{
				role: "assistant",
				content: [
					{ type: "thinking", thinking: "private trace" },
					{ type: "text", text: "Hello from Cline" },
				],
			},
			{
				role: "assistant",
				content: [{ type: "thinking", thinking: "drop me" }],
			},
			{ role: "user", content: "workd" },
		])) {
			// 排空流，使 provider 请求被执行。
		}

		const call = streamTextSpy.mock.calls.at(-1)?.[0] as
			| { messages?: unknown[] }
			| undefined;
		expect(call?.messages).toEqual(
			expect.arrayContaining([
				expect.objectContaining({
					role: "assistant",
					content: [
						expect.objectContaining({
							type: "text",
							text: "Hello from Cline",
						}),
					],
				}),
			]),
		);
		const serializedMessages = JSON.stringify(call?.messages);
		expect(serializedMessages).not.toContain("reasoning");
		expect(serializedMessages).not.toContain("private trace");
		expect(serializedMessages).not.toContain("drop me");
	});

	it("adds Azure API version to deployment-style OpenAI-compatible requests", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});
		const providerFetch = vi.fn(
			async () => new Response("{}"),
		) as unknown as typeof fetch;

		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "gpt-4.1",
			apiKey: "test-key",
			baseUrl: "https://example.openai.azure.com/openai/deployments/gpt-4.1",
			fetch: providerFetch,
			azure: { apiVersion: "2025-01-01-preview" },
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 被构造。
		}

		const factoryConfig = openaiCompatibleFactorySpy.mock.calls.at(-1)?.[0] as
			| { fetch?: typeof fetch }
			| undefined;
		expect(factoryConfig?.fetch).toEqual(expect.any(Function));

		await factoryConfig?.fetch?.(
			"https://example.openai.azure.com/openai/deployments/gpt-4.1/chat/completions",
			{ method: "POST" },
		);

		expect(providerFetch).toHaveBeenCalledWith(
			"https://example.openai.azure.com/openai/deployments/gpt-4.1/chat/completions?api-version=2025-01-01-preview",
			{ method: "POST" },
		);
	});

	it("does not add Azure API version to OpenAI v1-compatible requests", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});
		const providerFetch = vi.fn(
			async () => new Response("{}"),
		) as unknown as typeof fetch;

		const handler = createGatewayApiHandler({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "gpt-4.1",
			apiKey: "test-key",
			baseUrl: "https://example.openai.azure.com/openai/v1",
			fetch: providerFetch,
			azure: { apiVersion: "2025-01-01-preview" },
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 被构造。
		}

		const factoryConfig = openaiCompatibleFactorySpy.mock.calls.at(-1)?.[0] as
			| { fetch?: typeof fetch }
			| undefined;
		await factoryConfig?.fetch?.(
			"https://example.openai.azure.com/openai/v1/chat/completions",
			{ method: "POST" },
		);

		expect(providerFetch).toHaveBeenCalledWith(
			"https://example.openai.azure.com/openai/v1/chat/completions",
			{ method: "POST" },
		);
	});

	it("throws ClineNotSubscribedError for ClinePass required-plan 403 responses", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});
		const providerFetch = vi.fn(
			async () =>
				new Response(
					JSON.stringify({
						error: {
							message: "the user is not subscribed to required model plan",
						},
					}),
					{ status: 403 },
				),
		) as unknown as typeof fetch;

		const handler = createGatewayApiHandler({
			providerId: "cline-pass",
			clientType: "openai-compatible",
			modelId: "premium-model",
			apiKey: "test-key",
			fetch: providerFetch,
		});

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 被构造。
		}

		const factoryConfig = openaiCompatibleFactorySpy.mock.calls.at(-1)?.[0] as
			| { fetch?: typeof fetch }
			| undefined;

		await expect(
			factoryConfig?.fetch?.("https://api.cline.bot/api/v1/chat/completions", {
				method: "POST",
			}),
		).rejects.toBeInstanceOf(ClineNotSubscribedError);
	});
});

describe("createGatewayApiHandlerAsync", () => {
	beforeEach(() => {
		streamTextSpy.mockReset();
		openaiCompatibleFactorySpy.mockReset();
		openaiCompatibleSpy.mockClear();
	});

	it("honors setAbortSignal on requests, not just the construction-time signal", async () => {
		streamTextSpy.mockReturnValue({
			fullStream: (async function* () {
				yield { type: "finish", finishReason: "stop" };
			})(),
			usage: Promise.resolve({ inputTokens: 1, outputTokens: 1 }),
		});

		const handler = await createGatewayApiHandlerAsync({
			providerId: "openai-compatible",
			clientType: "openai-compatible",
			modelId: "custom-model",
			apiKey: "test-key",
		});

		const controller = new AbortController();
		handler.setAbortSignal?.(controller.signal);

		for await (const _chunk of handler.createMessage("", [
			{ role: "user", content: "Hello" },
		])) {
			// 排空流，使 provider 请求被执行。
		}

		const call = streamTextSpy.mock.calls.at(-1)?.[0] as
			| { abortSignal?: AbortSignal }
			| undefined;
		expect(call?.abortSignal).toBe(controller.signal);
	});
});

/**
 * compat.ts 消息转换（LlmsProviders.Message → AgentMessage）的测试。
 *
 * 专门守护 read_file 图片传递路径：编排器的
 * `tool_result` 块携带 {text, image} 内容块数组，我们
 * 必须把该数组作为 AgentMessage 的 `tool-result` `output` 转发，使
 * 下游 `toAiSdkToolResultOutput` 格式化器发出 AI SDK
 * `{type:"content", value:[{type:"media", ...}, {type:"text", ...}]}`。如果在这里
 * 把数组折叠为字符串，图片字节就会被丢弃，
 * 模型会产生幻觉。
 */
describe("toGatewayRequestMessages — tool_result with images", () => {
	it("forwards text+image content arrays as the tool-result output", () => {
		const messages: Message[] = [
			{
				role: "assistant",
				content: [
					{
						type: "tool_use",
						id: "call_1",
						name: "read_file",
						input: { path: "/tmp/image.jpg" },
					},
				],
			},
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "call_1",
						content: [
							{ type: "text", text: "Successfully read image" },
							{
								type: "image",
								data: "BASE64DATA",
								mediaType: "image/jpeg",
							},
						],
						is_error: false,
					},
				],
			},
		];

		const [, userMessage] = toGatewayRequestMessages(messages);

		// user 消息必须只包含一个 tool-result 块（没有孤立的图片兄弟块）。
		expect(userMessage.content).toHaveLength(1);
		const toolResult = userMessage.content[0] as Record<string, unknown>;

		expect(toolResult.type).toBe("tool-result");
		expect(toolResult.toolCallId).toBe("call_1");
		expect(toolResult.toolName).toBe("read_file");
		expect(toolResult.isError).toBe(false);

		// `output` 必须是完整的结构化内容块数组——包括
		// 图片——这样 toAiSdkToolResultOutput 才能发出 `{type:"content"}`。
		const output = toolResult.output as Array<Record<string, unknown>>;
		expect(Array.isArray(output)).toBe(true);
		expect(output).toHaveLength(2);
		expect(output[0]).toEqual({
			type: "text",
			text: "Successfully read image",
		});
		expect(output[1]).toEqual({
			type: "image",
			data: "BASE64DATA",
			mediaType: "image/jpeg",
		});
	});

	it("forwards text-only tool_result content unchanged for downstream normalisation", () => {
		// compat 层不再把 `[{type:'text', text}]` 折叠为
		// 裸字符串——AI SDK 格式化器直接接受内容块
		// 数组，并将其作为 `{type:'content'}` tool-result
		// 输出发出。（`toAiSdkToolResultOutput` 随后原样转发
		// 文本部分。）
		const messages: Message[] = [
			{
				role: "assistant",
				content: [
					{
						type: "tool_use",
						id: "call_2",
						name: "read_file",
						input: { path: "/tmp/notes.txt" },
					},
				],
			},
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "call_2",
						content: [{ type: "text", text: "hello world" }],
					},
				],
			},
		];

		const [, userMessage] = toGatewayRequestMessages(messages);
		expect(userMessage.content).toHaveLength(1);
		const toolResult = userMessage.content[0] as Record<string, unknown>;
		expect(toolResult.output).toEqual([{ type: "text", text: "hello world" }]);
	});

	it("passes plain string content through unchanged", () => {
		const messages: Message[] = [
			{
				role: "user",
				content: [
					{
						type: "tool_result",
						tool_use_id: "call_3",
						content: "raw string output",
					},
				],
			},
		];

		const [userMessage] = toGatewayRequestMessages(messages);
		const toolResult = userMessage.content[0] as Record<string, unknown>;
		expect(toolResult.output).toBe("raw string output");
	});
});

describe("buildGatewayModels", () => {
	const { buildGatewayModels } = _testing;

	it("projects configured maxInputTokens onto the selected gateway model", () => {
		const models = buildGatewayModels("ollama", {
			providerId: "ollama",
			modelId: "llama3.1",
			maxInputTokens: 8192,
			knownModels: {
				"llama3.1": {
					id: "llama3.1",
					name: "llama3.1",
					contextWindow: 131072,
				},
			},
		});

		expect(models).toEqual([
			expect.objectContaining({
				id: "llama3.1",
				contextWindow: 8192,
				maxInputTokens: 8192,
			}),
		]);
	});

	it("preserves catalog reasoning controls on projected gateway models", () => {
		const reasoningOptions = [
			{ type: "effort" as const, values: ["medium", "high", "max"] as const },
		];
		const models = buildGatewayModels("openrouter", {
			providerId: "openrouter",
			modelId: "openai/gpt-5.6",
			knownModels: {
				"openai/gpt-5.6": {
					id: "openai/gpt-5.6",
					name: "GPT-5.6",
					contextWindow: 400_000,
					reasoningOptions,
				},
			},
		});

		expect(models).toEqual([
			expect.objectContaining({
				id: "openai/gpt-5.6",
				reasoningOptions,
			}),
		]);
	});

	it("creates a definition for the selected model when it is not in knownModels", () => {
		const models = buildGatewayModels("ollama", {
			providerId: "ollama",
			modelId: "minimax-m3:cloud",
			maxInputTokens: 500000,
		});

		expect(models).toEqual([
			expect.objectContaining({
				id: "minimax-m3:cloud",
				contextWindow: 500000,
				maxInputTokens: 500000,
			}),
		]);
	});

	it("lets an explicit modelInfo override win over the generic limit", () => {
		const models = buildGatewayModels("ollama", {
			providerId: "ollama",
			modelId: "llama3.1",
			maxInputTokens: 8192,
			modelInfo: { id: "llama3.1", contextWindow: 16384 },
		});

		expect(models).toEqual([
			expect.objectContaining({
				id: "llama3.1",
				contextWindow: 16384,
			}),
		]);
	});

	it("returns undefined when there is nothing to project", () => {
		expect(
			buildGatewayModels("ollama", {
				providerId: "ollama",
				modelId: "llama3.1",
			}),
		).toBeUndefined();
	});
});
