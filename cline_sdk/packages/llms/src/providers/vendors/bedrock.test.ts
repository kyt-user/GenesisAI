import type { GatewayResolvedProviderConfig } from "@cline/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createBedrockProviderModule, resolveBedrockModelId } from "./bedrock";

const createAmazonBedrockMock = vi.hoisted(() => vi.fn());
const fromNodeProviderChainMock = vi.hoisted(() => vi.fn());
const bedrockModelMock = vi.hoisted(() =>
	vi.fn((modelId: string) => ({ modelId })),
);

vi.mock("@ai-sdk/amazon-bedrock", () => ({
	createAmazonBedrock: createAmazonBedrockMock,
}));

vi.mock("@aws-sdk/credential-providers", () => ({
	fromNodeProviderChain: fromNodeProviderChainMock,
}));

const ORIGINAL_ENV = { ...process.env };

describe("createBedrockProviderModule", () => {
	beforeEach(() => {
		process.env = { ...ORIGINAL_ENV };
		createAmazonBedrockMock.mockReset();
		createAmazonBedrockMock.mockReturnValue(bedrockModelMock);
		fromNodeProviderChainMock.mockReset();
		fromNodeProviderChainMock.mockReturnValue(async () => ({
			accessKeyId: "chain-access-key",
			secretAccessKey: "chain-secret-key",
		}));
		bedrockModelMock.mockClear();
	});

	afterEach(() => {
		process.env = { ...ORIGINAL_ENV };
	});

	it("uses explicit Bedrock bearer API keys without configuring SigV4 credentials", async () => {
		await createBedrockProviderModule(
			config({
				apiKey: " bedrock-api-key ",
				options: { region: "us-east-1" },
			}),
		);

		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "bedrock-api-key",
				region: "us-east-1",
				accessKeyId: undefined,
				secretAccessKey: undefined,
				sessionToken: undefined,
				credentialProvider: undefined,
			}),
		);
		expect(fromNodeProviderChainMock).not.toHaveBeenCalled();
	});

	it("suppresses provider credential fallback for explicit API-key auth with no resolved key", async () => {
		await createBedrockProviderModule(
			config({
				options: { authentication: "apikey", region: "us-east-1" },
			}),
		);

		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "",
				accessKeyId: undefined,
				secretAccessKey: undefined,
				sessionToken: undefined,
				credentialProvider: undefined,
			}),
		);
		expect(fromNodeProviderChainMock).not.toHaveBeenCalled();
	});

	it("uses direct IAM credentials and disables bearer-token env fallback", async () => {
		process.env.AWS_BEARER_TOKEN_BEDROCK = "env-bearer-token";

		await createBedrockProviderModule(
			config({
				options: {
					authentication: "iam",
					region: "us-west-2",
					accessKeyId: "access-key",
					secretAccessKey: "secret-key",
					sessionToken: "session-token",
				},
			}),
		);

		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "",
				accessKeyId: "access-key",
				secretAccessKey: "secret-key",
				sessionToken: "session-token",
				credentialProvider: undefined,
			}),
		);
		expect(fromNodeProviderChainMock).not.toHaveBeenCalled();
	});

	it("uses AWS profiles through the SDK credential provider chain", async () => {
		await createBedrockProviderModule(
			config({
				options: {
					authentication: "profile",
					profile: "dev-profile",
					region: "us-east-2",
				},
			}),
		);

		expect(fromNodeProviderChainMock).toHaveBeenCalledWith({
			ignoreCache: true,
			profile: "dev-profile",
			clientConfig: { region: "us-east-2" },
		});
		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "",
				accessKeyId: undefined,
				secretAccessKey: undefined,
				sessionToken: undefined,
				credentialProvider: expect.any(Function),
			}),
		);
	});

	it("treats a configured AWS profile as profile auth when authentication is omitted", async () => {
		await createBedrockProviderModule(
			config({
				options: {
					profile: "default",
					region: "us-east-1",
				},
			}),
		);

		expect(fromNodeProviderChainMock).toHaveBeenCalledWith({
			ignoreCache: true,
			profile: "default",
			clientConfig: { region: "us-east-1" },
		});
		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "",
				credentialProvider: expect.any(Function),
			}),
		);
	});

	it("uses the default AWS SDK credential chain when no static credentials are configured", async () => {
		await createBedrockProviderModule(
			config({
				options: { authentication: "iam", region: "us-east-1" },
			}),
		);

		expect(fromNodeProviderChainMock).toHaveBeenCalledWith({
			clientConfig: { region: "us-east-1" },
		});
		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "",
				credentialProvider: expect.any(Function),
			}),
		);
	});

	it("does not treat AWS_REGION or IAM env vars as bearer API keys", async () => {
		process.env.AWS_REGION = "us-west-2";
		process.env.AWS_ACCESS_KEY_ID = "env-access-key";
		process.env.AWS_SECRET_ACCESS_KEY = "env-secret-key";

		await createBedrockProviderModule(
			config({
				apiKeyEnv: ["AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
				options: { authentication: "iam" },
			}),
		);

		expect(createAmazonBedrockMock).toHaveBeenCalledWith(
			expect.objectContaining({
				apiKey: "",
				credentialProvider: expect.any(Function),
			}),
		);
	});

	it("routes bare modern model ids through the region's geo inference profile", async () => {
		const module = await createBedrockProviderModule(
			config({
				apiKey: "bedrock-api-key",
				options: { region: "us-east-1" },
			}),
		);

		module.operations.language("anthropic.claude-sonnet-4-6");

		expect(bedrockModelMock).toHaveBeenCalledWith(
			"us.anthropic.claude-sonnet-4-6",
		);
	});

	it("derives the geo prefix from AWS_REGION when no region is configured", async () => {
		process.env.AWS_REGION = "eu-west-1";

		const module = await createBedrockProviderModule(
			config({ apiKey: "bedrock-api-key" }),
		);

		module.operations.language("anthropic.claude-sonnet-4-6");

		expect(bedrockModelMock).toHaveBeenCalledWith(
			"eu.anthropic.claude-sonnet-4-6",
		);
	});

	it("still prefixes catalog models when a stale customModelBaseId is configured", async () => {
		// 遗留迁移复制 awsBedrockCustomModelBaseId 时并不带
		// 自定义选择标志，因此保留的 base id 不得为
		// 普通目录模型禁用 inference-profile 路由。
		const module = await createBedrockProviderModule(
			config({
				apiKey: "bedrock-api-key",
				options: {
					region: "us-east-1",
					customModelBaseId: "anthropic.claude-3-5-sonnet-20241022-v2:0",
				},
			}),
		);

		module.operations.language("anthropic.claude-sonnet-4-6");

		expect(bedrockModelMock).toHaveBeenCalledWith(
			"us.anthropic.claude-sonnet-4-6",
		);
	});

	it("passes already-prefixed inference-profile ids through unmodified", async () => {
		const module = await createBedrockProviderModule(
			config({
				apiKey: "bedrock-api-key",
				options: { region: "us-east-1", useCrossRegionInference: true },
			}),
		);

		module.operations.language("global.anthropic.claude-sonnet-4-6");

		expect(bedrockModelMock).toHaveBeenCalledWith(
			"global.anthropic.claude-sonnet-4-6",
		);
	});
});

describe("resolveBedrockModelId", () => {
	it("prefixes bare modern Claude ids with the region geo profile", () => {
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "us-east-1",
			}),
		).toBe("us.anthropic.claude-sonnet-4-6");
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-5-20250929-v1:0", {
				region: "eu-central-1",
			}),
		).toBe("eu.anthropic.claude-sonnet-4-5-20250929-v1:0");
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-5", {
				region: "us-east-1",
			}),
		).toBe("us.anthropic.claude-sonnet-5");
		expect(
			resolveBedrockModelId("anthropic.claude-fable-5", {
				region: "us-east-1",
			}),
		).toBe("us.anthropic.claude-fable-5");
	});

	it("prefixes other profile-only foundation models with confirmed variants", () => {
		// 保持目录存在性显式：模型会从生成的目录中
		// 出现和消失（deepseek.r1-v1:0 已从 Bedrock 退役），
		// 但不改变解析器"确认变体加前缀"的契约。
		const hasCatalogModel = (modelId: string) =>
			modelId === "us.deepseek.r1-v1:0" ||
			modelId === "us.meta.llama4-maverick-17b-instruct-v1:0";
		expect(
			resolveBedrockModelId("deepseek.r1-v1:0", {
				region: "us-west-2",
				hasCatalogModel,
			}),
		).toBe("us.deepseek.r1-v1:0");
		expect(
			resolveBedrockModelId("meta.llama4-maverick-17b-instruct-v1:0", {
				region: "us-east-1",
				hasCatalogModel,
			}),
		).toBe("us.meta.llama4-maverick-17b-instruct-v1:0");
	});

	it("covers future tier-first Claude ids once the catalog carries their variants", () => {
		// tier-first 命名以通用方式匹配，因此未来的 tier 无需
		// 更新模式列表——只需重新生成的目录条目。
		const hasCatalogModel = (id: string) =>
			id === "us.anthropic.claude-newtier-6";
		expect(
			resolveBedrockModelId("anthropic.claude-newtier-6", {
				region: "us-east-1",
				hasCatalogModel,
			}),
		).toBe("us.anthropic.claude-newtier-6");
	});

	it("preserves the raw id when no catalog variant confirms the geo profile", () => {
		// 保持目录缺失显式：生成的目录可以获得变体，
		// 而不改变解析器的回退契约。
		const hasCatalogModel = () => false;
		const cases: Array<[string, string | undefined]> = [
			["amazon.nova-lite-v1:0", "eu-central-1"],
			["amazon.nova-pro-v1:0", "us-east-1"],
			["amazon.nova-2-lite-v1:0", "us-east-1"],
			["anthropic.claude-3-7-sonnet-20250219-v1:0", "us-east-1"],
			["anthropic.claude-sonnet-5", "us-gov-west-1"],
			["anthropic.claude-newtier-6", "us-east-1"],
		];
		for (const [modelId, region] of cases) {
			expect(resolveBedrockModelId(modelId, { region, hasCatalogModel })).toBe(
				modelId,
			);
			expect(
				resolveBedrockModelId(modelId, {
					region,
					useCrossRegionInference: true,
					hasCatalogModel,
				}),
			).toBe(modelId);
		}
	});

	it("uses a catalog-confirmed us-gov. variant in GovCloud regions", () => {
		const hasCatalogModel = (id: string) =>
			id === "us-gov.anthropic.claude-sonnet-5";
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-5", {
				region: "us-gov-west-1",
				hasCatalogModel,
			}),
		).toBe("us-gov.anthropic.claude-sonnet-5");
	});

	it("never rewrites ids that are already profile-prefixed", () => {
		for (const modelId of [
			"us.anthropic.claude-sonnet-4-6",
			"eu.anthropic.claude-sonnet-5",
			"apac.anthropic.claude-sonnet-4-20250514-v1:0",
			"jp.anthropic.claude-sonnet-4-6",
			"global.anthropic.claude-opus-5",
		]) {
			expect(
				resolveBedrockModelId(modelId, {
					region: "us-east-1",
					useCrossRegionInference: true,
				}),
			).toBe(modelId);
		}
	});

	it("never rewrites ARNs", () => {
		const provisionedArn =
			"arn:aws:bedrock:us-east-1:123456789012:provisioned-model/abc123";
		const profileArn =
			"arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/xyz";
		expect(
			resolveBedrockModelId(provisionedArn, {
				region: "us-east-1",
				useCrossRegionInference: true,
			}),
		).toBe(provisionedArn);
		expect(resolveBedrockModelId(profileArn, { region: "us-east-1" })).toBe(
			profileArn,
		);
	});

	it("keeps custom/provisioned model ids raw on the cross-region path", () => {
		// 不是目录模型也不是已知的仅 profile 系列：无法确认任何
		// profile 变体，因此即使启用跨区域设置，
		// id 也保持不变。
		expect(
			resolveBedrockModelId("my-provisioned-model", {
				region: "us-east-1",
				useCrossRegionInference: true,
			}),
		).toBe("my-provisioned-model");
	});

	it("falls back to the raw id for regions without a geo profile mapping", () => {
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "ca-central-1",
			}),
		).toBe("anthropic.claude-sonnet-4-6");
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				useCrossRegionInference: true,
			}),
		).toBe("anthropic.claude-sonnet-4-6");
	});

	it("prefixes bare OpenAI GPT-5.x/GPT-6 ids, which have no on-demand throughput", () => {
		// cline/cline#14468：目录以裸 id 列出这些模型，但
		// Bedrock 仅通过 inference profile 为它们服务。
		const hasCatalogModel = (id: string) =>
			[
				"us.openai.gpt-6-astra",
				"us.openai.gpt-6-sol",
				"us.openai.gpt-5.6-luna",
				"global.openai.gpt-6-astra",
			].includes(id);
		for (const [bare, expected] of [
			["openai.gpt-6-astra", "us.openai.gpt-6-astra"],
			["openai.gpt-6-sol", "us.openai.gpt-6-sol"],
			["openai.gpt-5.6-luna", "us.openai.gpt-5.6-luna"],
		]) {
			expect(
				resolveBedrockModelId(bare, { region: "us-east-1", hasCatalogModel }),
			).toBe(expected);
		}
		// 仅使用目录确认的 geo 变体：没有 eu. profile 时
		// id 保持原始而不猜测（global. 需要显式
		// useGlobalInference 设置）。
		expect(
			resolveBedrockModelId("openai.gpt-6-astra", {
				region: "eu-west-1",
				hasCatalogModel,
			}),
		).toBe("openai.gpt-6-astra");
		expect(
			resolveBedrockModelId("openai.gpt-6-astra", {
				region: "eu-west-1",
				useCrossRegionInference: true,
				useGlobalInference: true,
				hasCatalogModel,
			}),
		).toBe("global.openai.gpt-6-astra");
	});

	it("routes India regions through the in. profile when that is the confirmed variant", () => {
		// GPT-5.6 Luna/Terra 提供 in. profile，而没有 apac. 的。
		const hasCatalogModel = (id: string) =>
			id === "in.openai.gpt-5.6-luna" || id === "in.openai.gpt-5.6-terra";
		expect(
			resolveBedrockModelId("openai.gpt-5.6-luna", {
				region: "ap-south-1",
				hasCatalogModel,
			}),
		).toBe("in.openai.gpt-5.6-luna");
		expect(
			resolveBedrockModelId("openai.gpt-5.6-terra", {
				region: "ap-south-2",
				useCrossRegionInference: true,
				hasCatalogModel,
			}),
		).toBe("in.openai.gpt-5.6-terra");
		// 已带前缀的印度 id 像其他所有 geo profile 一样原样通过。
		expect(
			resolveBedrockModelId("in.openai.gpt-5.6-luna", {
				region: "us-east-1",
				hasCatalogModel,
			}),
		).toBe("in.openai.gpt-5.6-luna");
	});

	it("keeps bare gpt-oss ids, which have on-demand throughput", () => {
		const hasCatalogModel = (id: string) =>
			id === "us-gov.openai.gpt-oss-120b-1:0" ||
			id === "us.openai.gpt-oss-120b-1:0";
		expect(
			resolveBedrockModelId("openai.gpt-oss-120b-1:0", {
				region: "us-east-1",
				hasCatalogModel,
			}),
		).toBe("openai.gpt-oss-120b-1:0");
	});

	it("leaves on-demand-capable models untouched", () => {
		for (const modelId of [
			"anthropic.claude-3-5-sonnet-20241022-v2:0",
			"anthropic.claude-v2:1",
			"anthropic.claude-instant-v1",
			"amazon.nova-canvas-v1:0",
			"amazon.titan-text-express-v1",
		]) {
			expect(resolveBedrockModelId(modelId, { region: "us-east-1" })).toBe(
				modelId,
			);
		}
	});

	it("applies cross-region inference only to catalog-confirmed profile variants", () => {
		const hasCatalogModel = (id: string) => id === "us.vendor.on-demand-model";
		// 确认的变体：设置路由经过 geo profile。
		expect(
			resolveBedrockModelId("vendor.on-demand-model", {
				region: "us-east-1",
				useCrossRegionInference: true,
				hasCatalogModel,
			}),
		).toBe("us.vendor.on-demand-model");
		// 没有该设置时 id 保持原始。
		expect(
			resolveBedrockModelId("vendor.on-demand-model", {
				region: "us-east-1",
				hasCatalogModel,
			}),
		).toBe("vendor.on-demand-model");
		// 没有确认的变体：绝不为按需可用的模型
		// 捏造 profile id。
		expect(
			resolveBedrockModelId("vendor.on-demand-model", {
				region: "eu-west-1",
				useCrossRegionInference: true,
				hasCatalogModel,
			}),
		).toBe("vendor.on-demand-model");
	});

	it("uses the global profile when both inference settings are enabled and the variant exists", () => {
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "us-east-1",
				useCrossRegionInference: true,
				useGlobalInference: true,
			}),
		).toBe("global.anthropic.claude-sonnet-4-6");
		// 全局推理需要跨区域推理，与遗留行为一致。
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "us-east-1",
				useGlobalInference: true,
			}),
		).toBe("us.anthropic.claude-sonnet-4-6");
		// 没有已知全局变体的模型降级到 geo profile。
		// 显式断言 geo 变体，使该用例不依赖
		// 生成的目录仍然包含此模型。
		expect(
			resolveBedrockModelId("deepseek.r1-v1:0", {
				region: "us-west-2",
				useCrossRegionInference: true,
				useGlobalInference: true,
				hasCatalogModel: (modelId) => modelId === "us.deepseek.r1-v1:0",
			}),
		).toBe("us.deepseek.r1-v1:0");
	});

	it("prefers country profiles over apac. where AWS ships them", () => {
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "ap-northeast-1",
			}),
		).toBe("jp.anthropic.claude-sonnet-4-6");
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "ap-southeast-2",
			}),
		).toBe("au.anthropic.claude-sonnet-4-6");
	});

	it("uses apac. only when the catalog confirms the variant exists", () => {
		const hasCatalogModel = (id: string) =>
			["apac.anthropic.claude-sonnet-4-20250514-v1:0"].includes(id);
		// 亚太区域中确认的 apac 变体。
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-20250514-v1:0", {
				region: "ap-southeast-1",
				hasCatalogModel,
			}),
		).toBe("apac.anthropic.claude-sonnet-4-20250514-v1:0");
		// 当不存在 jp./au. 变体时，国家区域降级到
		// 确认的 apac 变体。
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-20250514-v1:0", {
				region: "ap-northeast-1",
				hasCatalogModel,
			}),
		).toBe("apac.anthropic.claude-sonnet-4-20250514-v1:0");
		// 没有确认的变体：保留原始 id，而不是捏造一个
		// AWS 会以无效模型标识符拒绝的 apac. id。
		expect(
			resolveBedrockModelId("anthropic.claude-sonnet-4-6", {
				region: "ap-southeast-1",
				hasCatalogModel,
			}),
		).toBe("anthropic.claude-sonnet-4-6");
	});

	it("resolves the expected wire id per region for a modern Claude model", () => {
		const cases: Array<[string | undefined, string]> = [
			["us-east-1", "us.anthropic.claude-sonnet-4-6"],
			["us-west-2", "us.anthropic.claude-sonnet-4-6"],
			// 没有目录确认的 us-gov. 变体：保留原始 id。
			["us-gov-west-1", "anthropic.claude-sonnet-4-6"],
			["eu-central-1", "eu.anthropic.claude-sonnet-4-6"],
			["eu-west-3", "eu.anthropic.claude-sonnet-4-6"],
			["ap-northeast-1", "jp.anthropic.claude-sonnet-4-6"],
			["ap-northeast-3", "jp.anthropic.claude-sonnet-4-6"],
			["ap-southeast-2", "au.anthropic.claude-sonnet-4-6"],
			["ap-southeast-4", "au.anthropic.claude-sonnet-4-6"],
			// 没有目录确认的 apac./geo 变体：保留原始 id。
			["ap-southeast-1", "anthropic.claude-sonnet-4-6"],
			["ap-northeast-2", "anthropic.claude-sonnet-4-6"],
			["ca-central-1", "anthropic.claude-sonnet-4-6"],
			["sa-east-1", "anthropic.claude-sonnet-4-6"],
			["me-central-1", "anthropic.claude-sonnet-4-6"],
			[undefined, "anthropic.claude-sonnet-4-6"],
		];
		for (const [region, expected] of cases) {
			expect(
				resolveBedrockModelId("anthropic.claude-sonnet-4-6", { region }),
			).toBe(expected);
		}
	});
});

function config(
	overrides: Partial<GatewayResolvedProviderConfig>,
): GatewayResolvedProviderConfig {
	return {
		providerId: "bedrock",
		...overrides,
	};
}
