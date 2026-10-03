// Bedrock 推理的 wire 契约测试，通过*真实的*
// `@ai-sdk/amazon-bedrock` adapter 执行（bedrock.test.ts 会 mock 构造函数）。
// 每个用例通过 gateway 加 stub fetch 流式执行一轮，
// 并对 adapter 放在 Converse 请求上的 `additionalModelRequestFields`
// 进行断言。它们固定了 adapter 下限为 5.0.65 的原因：
// 通过 inference profile 到达的 OpenAI 模型（`us.openai.*`、
// `global.openai.*`）必须得到 `reasoning.effort`，即 Bedrock 接受的字段，
// 而不是旧版 adapter 对任何无法识别的 id 所发出的通用 `reasoningConfig`
// （cline/cline#14451, vercel/ai#19403）。
import type { GatewayStreamRequest, ModelReasoningOption } from "@cline/shared";
import { describe, expect, it } from "vitest";
import { createGateway } from "../gateway";

const EFFORT_OPTIONS: readonly ModelReasoningOption[] = [
	{ type: "effort", values: ["low", "medium", "high", "xhigh", "max"] },
];

interface WireRequest {
	/** Converse 请求的路径，携带 vendor 解析出的 id。 */
	path: string | undefined;
	/** adapter 构建的 `additionalModelRequestFields`；没有时为 `null`。 */
	sent: Record<string, unknown> | null | undefined;
}

/**
 * 为 `modelId` 流式执行一轮，并返回到达 stub fetch 的内容。
 *
 * 在 provider config 上注册模型可以让模型*选择*不依赖
 * 生成的目录。模型 id 的*解析*则不然：vendor 仍会让
 * 每个 id 经过 `resolveBedrockModelId`，后者查询目录来
 * 路由仅含裸 profile 的 id（us-east-1 中的裸 `openai.gpt-…` 会以
 * `us.openai.gpt-…` 发出）。已经携带 geo/global 前缀的 id
 * 原样通过，所以那些用例也固定了路径。stub 返回
 * 400：只有请求在测试范围内。
 */
async function wireRequest(
	modelId: string,
	reasoning: GatewayStreamRequest["reasoning"],
): Promise<WireRequest> {
	let path: string | undefined;
	let sent: Record<string, unknown> | null | undefined;
	const fetchStub = (async (input, init) => {
		path = decodeURIComponent(new URL(String(input)).pathname);
		const body = JSON.parse((init?.body as string) ?? "{}");
		sent = body.additionalModelRequestFields ?? null;
		return new Response(JSON.stringify({ message: "stub" }), {
			status: 400,
			headers: { "content-type": "application/json" },
		});
	}) as typeof fetch;
	const gateway = createGateway({
		providerConfigs: [
			{
				providerId: "bedrock",
				apiKey: "test-bearer-key",
				fetch: fetchStub,
				options: { region: "us-east-1", authentication: "apikey" },
				models: [
					{
						id: modelId,
						name: modelId,
						capabilities: ["text", "reasoning"],
						reasoningOptions: EFFORT_OPTIONS,
					},
				],
			},
		],
	});
	for await (const _event of await gateway.stream({
		providerId: "bedrock",
		modelId,
		reasoning,
		maxTokens: 16,
		messages: [
			{
				id: "m1",
				role: "user",
				content: [{ type: "text", text: "hi" }],
				createdAt: 0,
			},
		],
	})) {
		// 排空
	}
	return { path, sent };
}

describe("Bedrock reasoning wire contract", () => {
	// 回归点：这些 id 携带前缀到达 adapter，在 5.0.65 之前得到的是
	// 通用 `reasoningConfig`。它们原样通过
	// 解析器，因此路径断言不需要目录。
	it.each([
		"us.openai.gpt-6-astra",
		"global.openai.gpt-5.6-luna",
	])("sends reasoning.effort for inference-profile OpenAI model %s", async (modelId) => {
		const { path, sent } = await wireRequest(modelId, { effort: "high" });
		expect(path).toBe(`/model/${modelId}/converse-stream`);
		expect(sent).toEqual({ reasoning: { effort: "high" } });
	});

	it("sends reasoning.effort for a bare OpenAI id, whichever profile the resolver picks", async () => {
		// 在 us-east-1 中，当目录确认存在 geo profile 时，解析器会
		// 将该裸 id 路由到它，因此 adapter 可能看到 `us.openai.…`。
		// 无论哪种方式，都必须保持 OpenAI 的形状。
		const { path, sent } = await wireRequest("openai.gpt-6-astra", {
			effort: "high",
		});
		expect(path).toMatch(
			/\/model\/(?:[a-z-]+\.)?openai\.gpt-6-astra\/converse-stream$/,
		);
		expect(sent).toEqual({ reasoning: { effort: "high" } });
	});

	it("keeps reasoning_effort for gpt-oss", async () => {
		const { path, sent } = await wireRequest("openai.gpt-oss-120b-1:0", {
			effort: "high",
		});
		expect(path).toBe("/model/openai.gpt-oss-120b-1:0/converse-stream");
		expect(sent).toEqual({ reasoning_effort: "high" });
	});

	it("keeps Anthropic models on the adapter's native shape", async () => {
		const { path, sent } = await wireRequest("us.anthropic.claude-sonnet-4-6", {
			effort: "high",
		});
		expect(path).toBe("/model/us.anthropic.claude-sonnet-4-6/converse-stream");
		expect(sent).toMatchObject({ output_config: { effort: "high" } });
	});
});
