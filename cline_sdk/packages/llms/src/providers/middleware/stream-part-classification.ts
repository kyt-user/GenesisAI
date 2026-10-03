// 对每个 `LanguageModelV4StreamPart` 的单一、穷尽分类。
//
// 此前两个消费者对这个联合类型各自持有不完整的理解：
// 空响应重试中间件判定"模型是否产出了任何东西？"，
// `ai-sdk.ts` 中的 `emitAiSdkEvents()` 判定"我们把什么
// 转换成 agent 事件？"。一方计入而另一方不计入的分片，
// 恰好产生重试机制本要防止的失败——例如只有文件的回合对
// 中间件来说是"内容"（永不重试），但被适配器丢弃，于是
// assistant 消息变空，任务仍然以 "Model returned empty response"
// 死亡。
//
// 本模块是给每个分片分配类别的唯一位置；重试
// 中间件据此推导重试资格，适配器的支持集合必须与
// `converted-content` 保持同步（由测试断言）。switch
// 带 `never` 检查做穷尽性验证，因此新增分片类型的 AI SDK
// 升级会在这里编译失败，而不是悄悄落入
// 错误的桶。

import type { LanguageModelV4StreamPart } from "@ai-sdk/provider";

export type ModelStreamPartClass =
	/**
	 * `emitAiSdkEvents()` 会转换成 `AgentModelEvent` 并因此
	 * 到达 assistant 消息的输出。包含其中任何分片的回合
	 * 都不为空。
	 */
	| "converted-content"
	/**
	 * Cline（尚）不会转换为 agent 事件的真实模型输出。
	 * 模型已经响应，重试只会浪费计费请求并
	 * 大概率重现同样的输出——永不重试。如果回合只包含
	 * 不受支持的输出，assistant 消息仍可能变空；
	 * 这是本桶明确的契约缺口，好过
	 * 悄悄对模型确实回答过的回合重新计费。
	 */
	| "unsupported-output"
	/** 不构成模型输出的标记和元数据。 */
	| "structural"
	/** `stream-start`——携带警告；每次尝试一个。 */
	| "stream-start"
	/** 终态记账分片——携带用量和 finish reason。 */
	| "finish"
	/** 作为流分片呈现的上游错误。 */
	| "error";

export function classifyModelStreamPart(
	part: LanguageModelV4StreamPart,
): ModelStreamPartClass {
	switch (part.type) {
		case "text-delta":
		case "reasoning-delta":
			return part.delta.length > 0 ? "converted-content" : "structural";
		// 工具活动：适配器转换工具调用（其流式输入分片
		// 会组装成工具调用）。模型想要行动时，只有工具调用的
		// 回合是正常形态——绝不为空。
		case "tool-call":
		case "tool-input-start":
		case "tool-input-delta":
		case "tool-input-end":
			return "converted-content";
		// 生成的文件被转换为 `file` agent 事件（见
		// `emitAiSdkEvents`），并作为图片或文件分片进入 assistant
		// 消息。
		case "file":
			return "converted-content";
		// provider 执行的工具结果在转换后的工具调用之后到达；
		// 结果本身不会翻译为 agent 事件。
		case "tool-result":
		// provider 侧对 provider 执行工具的审批请求。
		case "tool-approval-request":
		// 不透明的推理块（例如加密/脱敏的思维文件）。
		case "reasoning-file":
		// `{provider}.{type}` 信封中的 provider 特有内容。
		case "custom":
		// 附加到生成文本上的 URL 引用。
		case "source":
			return "unsupported-output";
		case "text-start":
		case "text-end":
		case "reasoning-start":
		case "reasoning-end":
		case "response-metadata":
		case "raw":
			return "structural";
		case "stream-start":
			return "stream-start";
		case "finish":
			return "finish";
		case "error":
			return "error";
		default: {
			// 穷尽性守卫：新的 AI SDK 分片类型必须有意识地
			// 分类，不能直接落入默认分支。
			const unhandled: never = part;
			throw new Error(
				`Unclassified model stream part: ${JSON.stringify(unhandled)}`,
			);
		}
	}
}

/**
 * 一个已完成的尝试若只包含本函数返回 `false` 的分片
 *（加上 `stream-start`/`finish`），是否算作真正为空的回合。
 *转换内容和不受支持的输出都表示"模型已响应"。
 */
export function isModelOutputPart(part: LanguageModelV4StreamPart): boolean {
	const kind = classifyModelStreamPart(part);
	return kind === "converted-content" || kind === "unsupported-output";
}
