import type {
	GatewayModelCapability,
	ModelCapability,
	ModelInfo,
} from "@cline/shared";

/**
 * 从目录能力到网关能力的唯一映射。
 *
 * 每个 `ModelCapability` 都命名其网关对应项，或在网关不做区分、
 * 该能力除纯文本生成外不暗示任何东西时为 `null`。由于键类型派生自
 * `ModelCapabilitySchema`，扩展该 schema 而不决定映射会是类型错误，
 * 而不是静默遗漏——这正是它所取代的手写 `switch` 语句
 * 逐渐漂移的原因。
 *
 * 网关的 `audio` 能力在这里刻意没有对应项：没有任何
 * 目录能力描述音频输入，音频输入改由
 * `ModelInfo.modalities` 表达。
 */
const GATEWAY_CAPABILITY_BY_MODEL_CAPABILITY: Readonly<
	Record<ModelCapability, GatewayModelCapability | null>
> = {
	images: "images",
	video: null,
	tools: "tools",
	streaming: null,
	"prompt-cache": "prompt-cache",
	reasoning: "reasoning",
	"reasoning-effort": null,
	"computer-use": null,
	"global-endpoint": null,
	structured_output: "structured-output",
	temperature: null,
	files: null,
};

/**
 * 将目录能力转换为网关能力。
 *
 * 每个网关模型都接受文本，因此 `"text"` 始终存在并位于
 * 结果开头。`ModelCapabilitySchema` 之外的能力会以纯字符串类型
 * 从动态 provider 列表到达此函数；它们只贡献隐含的文本能力，
 * 而不是未经验证地透传。
 *
 * 缺失或空列表产生 `undefined`，与 `modelHasCapability`
 * 将两者都视为「无能力信号」一致，使下游门控应用
 * 自己的默认值，而不是读取一个权威的否定。
 */
export function toGatewayModelCapabilities(
	capabilities: ModelInfo["capabilities"] | readonly string[] | undefined,
): GatewayModelCapability[] | undefined {
	if (!capabilities?.length) {
		return undefined;
	}

	const mapped = new Set<GatewayModelCapability>(["text"]);
	for (const capability of capabilities) {
		const gatewayCapability =
			GATEWAY_CAPABILITY_BY_MODEL_CAPABILITY[capability as ModelCapability];
		if (gatewayCapability) {
			mapped.add(gatewayCapability);
		}
	}
	return [...mapped];
}
