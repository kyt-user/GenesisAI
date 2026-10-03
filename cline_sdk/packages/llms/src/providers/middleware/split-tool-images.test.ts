import type {
	LanguageModelV4CallOptions,
	LanguageModelV4Message,
} from "@ai-sdk/provider";
import { describe, expect, it } from "vitest";
import {
	rewritePromptToolImages,
	splitToolImagesMiddleware,
} from "./split-tool-images";

const PLACEHOLDER = "(see following user message for image)";
const OMITTED_PLACEHOLDER = "[media omitted: invalid or exceeds size limit]";
const imageData = (byteLength: number, fill = 1) =>
	Buffer.alloc(byteLength, fill).toString("base64");

describe("rewritePromptToolImages", () => {
	it("leaves prompts without tool messages unchanged", () => {
		const prompt: LanguageModelV4Message[] = [
			{ role: "system", content: "you are a helpful assistant" },
			{
				role: "user",
				content: [{ type: "text", text: "what is 2+2?" }],
			},
			{
				role: "assistant",
				content: [{ type: "text", text: "4" }],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(false);
		// 没有变化时，实现会重新构建数组。
		// 关键是内容等价。
		expect(out.prompt).toEqual(prompt);
	});

	it("leaves text-only tool messages unchanged", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: { type: "text", value: "no images here" },
					},
					{
						type: "tool-result",
						toolCallId: "call_2",
						toolName: "read_files",
						output: {
							type: "content",
							value: [{ type: "text", text: "still no images" }],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(false);
		expect(out.prompt).toHaveLength(1);
	});

	it("splits image-data parts into a synthetic user message", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "Successfully read image" },
								{
									type: "file",
									data: { type: "data", data: "QkFTRTY0SU1BR0VCWVRFUw==" },
									mediaType: "image/jpeg",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(2);

		const [toolMsg, syntheticUser] = out.prompt;
		expect(toolMsg.role).toBe("tool");
		// tool-result 现在应具有占位文本而非 image-data。
		const toolResult = (
			toolMsg as Extract<LanguageModelV4Message, { role: "tool" }>
		).content[0];
		if (toolResult.type !== "tool-result") {
			throw new Error("expected tool-result");
		}
		expect(toolResult.output).toEqual({
			type: "content",
			value: [
				{ type: "text", text: "Successfully read image" },
				{ type: "text", text: PLACEHOLDER },
			],
		});

		// 合成用户消息以 FilePart 形式携带图片。
		expect(syntheticUser).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: { type: "data", data: "QkFTRTY0SU1BR0VCWVRFUw==" },
					mediaType: "image/jpeg",
				},
			],
		});
	});

	it("preserves filename and provider options on file-data parts", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: { type: "data", data: "QkFTRTY0UERGQllURVM=" },
									mediaType: "application/pdf",
									filename: "spec.pdf",
									providerOptions: {
										openai: { detail: "high" },
									},
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt[1]).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: { type: "data", data: "QkFTRTY0UERGQllURVM=" },
					mediaType: "application/pdf",
					filename: "spec.pdf",
					providerOptions: {
						openai: { detail: "high" },
					},
				},
			],
		});
	});

	it("converts image-url parts to file parts", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("https://example.com/cat.png"),
									},
									mediaType: "image/*",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt[1]).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: {
						type: "url",
						url: new URL("https://example.com/cat.png"),
					},
					mediaType: "image/*",
				},
			],
		});
	});

	it("omits image-url parts that exceed the aggregate media budget", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("https://example.com/a.png"),
									},
									mediaType: "image/*",
								},
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("https://example.com/b.png"),
									},
									mediaType: "image/*",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(2);
		expect(out.prompt[1]).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: {
						type: "url",
						url: new URL("https://example.com/a.png"),
					},
					mediaType: "image/*",
				},
			],
		});
		expect(JSON.stringify(out.prompt[0])).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(out.prompt[0])).not.toContain(
			"https://example.com/b.png",
		);
	});

	it("omits invalid data URL image-url parts instead of splitting them", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("data:image/png;base64,not-base64"),
									},
									mediaType: "image/*",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(1);
		expect(JSON.stringify(out.prompt[0])).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(out.prompt[0])).not.toContain("not-base64");
	});

	it("omits unsupported uppercase data URL image-url parts before splitting", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("DATA:image/svg+xml;base64,PHN2Zz4="),
									},
									mediaType: "image/*",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(1);
		expect(JSON.stringify(out.prompt[0])).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(out.prompt[0])).not.toContain("PHN2Zz4=");
	});

	it("omits file-url parts that exceed the aggregate media budget", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("https://example.com/a.pdf"),
									},
									mediaType: "application/octet-stream",
								},
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("https://example.com/b.pdf"),
									},
									mediaType: "application/octet-stream",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(2);
		expect(out.prompt[1]).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: {
						type: "url",
						url: new URL("https://example.com/a.pdf"),
					},
					mediaType: "application/octet-stream",
				},
			],
		});
		expect(JSON.stringify(out.prompt[0])).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(out.prompt[0])).not.toContain(
			"https://example.com/b.pdf",
		);
	});

	it("omits malformed file-url data URLs instead of splitting them", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: {
										type: "url",
										url: new URL("data:application/pdf;base64,not-base64"),
									},
									mediaType: "application/octet-stream",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(1);
		expect(JSON.stringify(out.prompt[0])).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(out.prompt[0])).not.toContain("not-base64");
	});

	it("omits oversized file-data parts instead of splitting them", () => {
		const oversizedFile = "A".repeat(6 * 1024 * 1024);
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: { type: "data", data: oversizedFile },
									mediaType: "application/pdf",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(1);
		expect(JSON.stringify(out.prompt[0])).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(out.prompt[0])).not.toContain(oversizedFile);
	});

	it("replaces invalid image-data with a text placeholder instead of splitting it", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{
									type: "file",
									data: { type: "data", data: "not-base64" },
									mediaType: "image/png",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(1);
		const toolResult = (
			out.prompt[0] as Extract<LanguageModelV4Message, { role: "tool" }>
		).content[0];
		if (toolResult.type !== "tool-result") {
			throw new Error("expected tool-result");
		}
		expect(toolResult.output).toEqual({
			type: "content",
			value: [
				{
					type: "text",
					text: "[media omitted: invalid or exceeds size limit]",
				},
			],
		});
	});

	it("leaves image-file-id parts in place (no FilePart equivalent)", () => {
		// image-file-id 是 OpenAI 特有的 provider 引用。它无法
		// 表示为 `LanguageModelV4FilePart`，因此我们将其留在
		// tool-result 中。该路径已经是多模态感知的，
		// 不需要重写。
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "Successfully read image" },
								{
									type: "file",
									data: {
										type: "reference",
										reference: { openai: "file_abc" },
									},
									mediaType: "image/*",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(false);
		const toolResult = (
			out.prompt[0] as Extract<LanguageModelV4Message, { role: "tool" }>
		).content[0];
		if (toolResult.type !== "tool-result") {
			throw new Error("expected tool-result");
		}
		// 仅存在 image-file-id 时输出不变。
		expect(toolResult.output).toEqual({
			type: "content",
			value: [
				{ type: "text", text: "Successfully read image" },
				{
					type: "file",
					data: { type: "reference", reference: { openai: "file_abc" } },
					mediaType: "image/*",
				},
			],
		});
	});

	it("aggregates images from multiple tool-results in one tool message", () => {
		// 带两个路径的 `read_files` 产生单条 `role:'tool'` 消息，
		// 包含两个 `tool-result` 片段（或一个带两张图片的 tool-result，
		// 取决于 agent 如何发出它们）。无论哪种方式，重写后的
		// 用户消息都应携带两张图片。
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "image 1" },
								{
									type: "file",
									data: { type: "data", data: "QUFB" },
									mediaType: "image/jpeg",
								},
							],
						},
					},
					{
						type: "tool-result",
						toolCallId: "call_2",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "image 2" },
								{
									type: "file",
									data: { type: "data", data: "QkJC" },
									mediaType: "image/png",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(2);
		expect(out.prompt[1]).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: { type: "data", data: "QUFB" },
					mediaType: "image/jpeg",
				},
				{
					type: "file",
					data: { type: "data", data: "QkJC" },
					mediaType: "image/png",
				},
			],
		});
	});

	it("omits images that exceed the aggregate media budget in the split backstop", () => {
		const firstImage = imageData(3_600_000, 1);
		const secondImage = imageData(3_600_000, 2);
		const prompt: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "image 1" },
								{
									type: "file",
									data: { type: "data", data: firstImage },
									mediaType: "image/png",
								},
							],
						},
					},
					{
						type: "tool-result",
						toolCallId: "call_2",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "image 2" },
								{
									type: "file",
									data: { type: "data", data: secondImage },
									mediaType: "image/png",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		expect(out.prompt).toHaveLength(2);
		expect(out.prompt[1]).toEqual({
			role: "user",
			content: [
				{
					type: "file",
					data: { type: "data", data: firstImage },
					mediaType: "image/png",
				},
			],
		});
		const toolMessage = out.prompt[0];
		if (toolMessage.role !== "tool") {
			throw new Error("expected tool message");
		}
		expect(JSON.stringify(toolMessage)).toContain(OMITTED_PLACEHOLDER);
		expect(JSON.stringify(toolMessage)).not.toContain(secondImage);
	});

	it("handles multiple separate tool messages in the same prompt", () => {
		const prompt: LanguageModelV4Message[] = [
			{
				role: "user",
				content: [{ type: "text", text: "show me both" }],
			},
			{
				role: "assistant",
				content: [
					{
						type: "tool-call",
						toolCallId: "call_1",
						toolName: "read_files",
						input: { paths: ["a.jpg"] },
					},
				],
			},
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "first" },
								{
									type: "file",
									data: { type: "data", data: "QUFB" },
									mediaType: "image/jpeg",
								},
							],
						},
					},
				],
			},
			{
				role: "assistant",
				content: [
					{
						type: "tool-call",
						toolCallId: "call_2",
						toolName: "read_files",
						input: { paths: ["b.jpg"] },
					},
				],
			},
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_2",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "second" },
								{
									type: "file",
									data: { type: "data", data: "QkJC" },
									mediaType: "image/png",
								},
							],
						},
					},
				],
			},
		];

		const out = rewritePromptToolImages(prompt);

		expect(out.mutated).toBe(true);
		// 原始 5 条 + 2 条合成用户消息。
		expect(out.prompt).toHaveLength(7);
		expect(out.prompt[3].role).toBe("user");
		expect(out.prompt[6].role).toBe("user");
	});

	it("does not mutate the input prompt array or its messages", () => {
		const original: LanguageModelV4Message[] = [
			{
				role: "tool",
				content: [
					{
						type: "tool-result",
						toolCallId: "call_1",
						toolName: "read_files",
						output: {
							type: "content",
							value: [
								{ type: "text", text: "before" },
								{
									type: "file",
									data: { type: "data", data: "QUFB" },
									mediaType: "image/jpeg",
								},
							],
						},
					},
				],
			},
		];
		const snapshot = JSON.parse(JSON.stringify(original));

		rewritePromptToolImages(original);

		expect(original).toEqual(snapshot);
	});
});

describe("splitToolImagesMiddleware", () => {
	it("has v4 specification version", () => {
		expect(splitToolImagesMiddleware.specificationVersion).toBe("v4");
	});

	it("returns the same params object reference when no rewrite is needed", async () => {
		const params: LanguageModelV4CallOptions = {
			prompt: [
				{
					role: "user",
					content: [{ type: "text", text: "hello" }],
				},
			],
		};

		const out = await splitToolImagesMiddleware.transformParams?.({
			type: "stream",
			params,
			// `model` 不被此中间件使用；为测试进行类型转换。
			model: undefined as never,
		});

		// 保持同一性意味着下游看到的是原始 CallOptions，
		// 无需不必要的克隆。
		expect(out).toBe(params);
	});

	it("returns transformed params with rewritten prompt when images are present", async () => {
		const params: LanguageModelV4CallOptions = {
			prompt: [
				{
					role: "tool",
					content: [
						{
							type: "tool-result",
							toolCallId: "call_1",
							toolName: "read_files",
							output: {
								type: "content",
								value: [
									{ type: "text", text: "ok" },
									{
										type: "file",
										data: { type: "data", data: "QUFB" },
										mediaType: "image/jpeg",
									},
								],
							},
						},
					],
				},
			],
		};

		const out = await splitToolImagesMiddleware.transformParams?.({
			type: "stream",
			params,
			model: undefined as never,
		});

		expect(out).not.toBe(params);
		expect(out?.prompt).toHaveLength(2);
		expect(out?.prompt[1].role).toBe("user");
	});

	it("preserves call-options siblings (temperature, tools, etc.)", async () => {
		const params: LanguageModelV4CallOptions = {
			prompt: [
				{
					role: "tool",
					content: [
						{
							type: "tool-result",
							toolCallId: "call_1",
							toolName: "read_files",
							output: {
								type: "content",
								value: [
									{
										type: "file",
										data: { type: "data", data: "QUFB" },
										mediaType: "image/jpeg",
									},
								],
							},
						},
					],
				},
			],
			temperature: 0.7,
			maxOutputTokens: 1000,
		};

		const out = await splitToolImagesMiddleware.transformParams?.({
			type: "stream",
			params,
			model: undefined as never,
		});

		expect(out?.temperature).toBe(0.7);
		expect(out?.maxOutputTokens).toBe(1000);
	});
});
