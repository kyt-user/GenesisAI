/**
 * 单一“目录到 gateway”能力翻译器的符合性测试。
 *
 * 这些测试刻意以状态驱动而非示例驱动。三个
 * gateway 模型定义生产者（内置 provider、
 * OpenAI 兼容路径，以及 `@cline/core` 中的已配置模型）各自
 * 携带手写的 `switch`，并逐渐漂移：直通
 * 能力的处理方式有三种，一个生产者映射了目录 schema 未定义的
 * `audio` 能力，另一个在其他人发出 `undefined` 的地方发出了
 * `["text"]`——而 gateway 门控会把后者读作
 * 权威拒绝，而不是“未知”。
 *
 * 因此套件不是断言少量有趣的输入，而是遍历
 * 取自 `ModelCapabilitySchema`（是 schema 本身，
 * 而非其副本）的整个能力状态空间，并断言每个生产者一致。
 * 往 schema 添加能力会在编译期让
 * `GATEWAY_CAPABILITY_BY_MODEL_CAPABILITY` 的穷尽性检查失败；
 * 添加第四个自行映射能力
 * 的生产者会让这里的等价性测试失败。
 */

import {
	type GatewayModelCapability,
	type ModelCapability,
	ModelCapabilitySchema,
	modelSupportsToolCalling,
} from "@cline/shared";
import { describe, expect, it } from "vitest";
import {
	BUILTIN_PROVIDER_COLLECTIONS_BY_ID,
	BUILTIN_PROVIDER_MANIFESTS_BY_ID,
} from "./builtins";
import { _testing } from "./compat";
import { toGatewayModelCapabilities } from "./model-capabilities";

const ALL_MODEL_CAPABILITIES = ModelCapabilitySchema.options;

const GATEWAY_CAPABILITIES: readonly GatewayModelCapability[] = [
	"text",
	"tools",
	"reasoning",
	"prompt-cache",
	"images",
	"audio",
	"structured-output",
];

describe("toGatewayModelCapabilities", () => {
	it("treats an absent or empty list as unspecified", () => {
		// 两者都必须保持 `undefined`：诸如
		// `modelSupportsImageInput` 之类的 gateway 门控只对缺失列表放行，
		// 此处返回 `["text"]` 会静默地拒绝图片。
		expect(toGatewayModelCapabilities(undefined)).toBeUndefined();
		expect(toGatewayModelCapabilities([])).toBeUndefined();
	});

	it.each(
		ALL_MODEL_CAPABILITIES,
	)("maps %s to a valid gateway capability set led by text", (capability) => {
		const result = toGatewayModelCapabilities([capability]);
		expect(result).toBeDefined();
		expect(result?.[0]).toBe("text");
		for (const mapped of result ?? []) {
			expect(GATEWAY_CAPABILITIES).toContain(mapped);
		}
		expect(new Set(result).size).toBe(result?.length);
	});

	it("covers every capability the schema declares", () => {
		// 防止能力被添加进 ModelCapabilitySchema 后
		// 仅为消除穷尽性错误而映射为 `null`：每一个
		// 都必须至少能翻译而不抛错，并产出含 text 能力的集合。
		for (const capability of ALL_MODEL_CAPABILITIES) {
			expect(toGatewayModelCapabilities([capability])).toContain("text");
		}
	});

	it("maps the capabilities the gateway distinguishes", () => {
		expect(toGatewayModelCapabilities(["images"])).toEqual(["text", "images"]);
		expect(toGatewayModelCapabilities(["tools"])).toEqual(["text", "tools"]);
		expect(toGatewayModelCapabilities(["reasoning"])).toEqual([
			"text",
			"reasoning",
		]);
		expect(toGatewayModelCapabilities(["prompt-cache"])).toEqual([
			"text",
			"prompt-cache",
		]);
		expect(toGatewayModelCapabilities(["structured_output"])).toEqual([
			"text",
			"structured-output",
		]);
	});

	it("reduces capabilities with no gateway counterpart to text", () => {
		expect(
			toGatewayModelCapabilities([
				"streaming",
				"temperature",
				"reasoning-effort",
				"computer-use",
				"global-endpoint",
				"files",
				"video",
			]),
		).toEqual(["text"]);
	});

	it("ignores values outside the schema instead of passing them through", () => {
		// 动态 provider 列表被类型化为普通字符串，因此未知
		// 值会在运行时到达翻译器。
		expect(toGatewayModelCapabilities(["not-a-capability"])).toEqual(["text"]);
		expect(toGatewayModelCapabilities(["not-a-capability", "images"])).toEqual([
			"text",
			"images",
		]);
	});

	it("deduplicates capabilities that collapse onto the same gateway value", () => {
		expect(toGatewayModelCapabilities(["streaming", "temperature"])).toEqual([
			"text",
		]);
		expect(toGatewayModelCapabilities(["images", "images"])).toEqual([
			"text",
			"images",
		]);
	});
});

/**
 * 每个 gateway 模型定义生产者都必须与翻译器一致。
 *
 * 这些测试驱动真实生产者，而不是重新实现它们的映射，
 * 因此重新长出自己 `switch` 的生产者会在这里失败，
 * 即使翻译器本身仍通过自己的单元测试。
 */
describe("gateway capability producers", () => {
	const CAPABILITY_STATES: readonly (readonly string[] | undefined)[] = [
		undefined,
		[],
		...ALL_MODEL_CAPABILITIES.map((capability) => [capability]),
		["tools", "images", "reasoning", "prompt-cache", "structured_output"],
		["streaming", "files", "video"],
		["not-a-capability"],
	];

	it.each(
		CAPABILITY_STATES.map(
			(capabilities) =>
				[JSON.stringify(capabilities) ?? "undefined", capabilities] as const,
		),
	)("the OpenAI-compatible path agrees with the translator for %s", (_label, capabilities) => {
		const models = _testing.buildGatewayModels("openai-compatible", {
			providerId: "openai-compatible",
			modelId: "m1",
			knownModels: {
				m1: {
					id: "m1",
					...(capabilities === undefined
						? {}
						: { capabilities: capabilities as ModelCapability[] }),
				},
			},
		} as Parameters<typeof _testing.buildGatewayModels>[1]);

		expect(models?.[0]?.capabilities).toEqual(
			toGatewayModelCapabilities(capabilities),
		);
	});

	it("leaves tool calling enabled for builtin models that declare no capabilities", () => {
		// 少数内置语言模型（dify、sapaicore、opencode 和
		// Codex CLI）在生成的目录中不带能力列表。
		// 为它们发出 `["text"]` 会让 `modelSupportsToolCalling` 读到
		// 权威拒绝，并从请求中剥除所有工具定义。
		const languageModelsWithoutCapabilities = Object.values(
			BUILTIN_PROVIDER_COLLECTIONS_BY_ID,
		).flatMap((collection) =>
			Object.entries(collection.models ?? {})
				.filter(
					([, info]) =>
						!info.capabilities?.length &&
						(info.operation ?? "language") === "language",
				)
				.map(([modelId]) => ({ collection, modelId })),
		);
		expect(languageModelsWithoutCapabilities.length).toBeGreaterThan(0);

		for (const { collection, modelId } of languageModelsWithoutCapabilities) {
			const manifest = BUILTIN_PROVIDER_MANIFESTS_BY_ID[collection.provider.id];
			const model = manifest?.models.find((entry) => entry.id === modelId);
			expect(model?.capabilities).toBeUndefined();
			expect(modelSupportsToolCalling(model ?? {})).toBe(true);
		}
	});

	it("builtin provider manifests agree with the translator", () => {
		// 遍历真实生成的目录：无论每个内置模型声明什么，
		// 其清单条目必须与翻译器的输出完全一致。
		const manifests = Object.values(BUILTIN_PROVIDER_MANIFESTS_BY_ID);
		expect(manifests.length).toBeGreaterThan(0);

		let comparedModels = 0;
		for (const manifest of manifests) {
			const collection = BUILTIN_PROVIDER_COLLECTIONS_BY_ID[manifest.id];
			for (const model of manifest.models) {
				const info = collection?.models?.[model.id];
				if (!info) {
					continue;
				}
				expect(model.capabilities).toEqual(
					toGatewayModelCapabilities(info.capabilities),
				);
				comparedModels += 1;
			}
		}
		expect(comparedModels).toBeGreaterThan(0);
	});
});
