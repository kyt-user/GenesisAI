import { getPropagatedAttributesFromContext } from "@langfuse/core";
import type { Context } from "@opentelemetry/api";
import type { Span, SpanProcessor } from "@opentelemetry/sdk-trace-node";

/** 将 Langfuse 上下文复制到中继 span 上而不创建导出器。 */
export class LangfuseAttributesSpanProcessor implements SpanProcessor {
	onStart(span: Span, parentContext: Context): void {
		if (span.instrumentationScope.name !== "cline-provider-langfuse") return;
		const attributes = getPropagatedAttributesFromContext(parentContext);
		for (const [key, value] of Object.entries(attributes)) {
			// 显式 span 属性（例如提示词元数据）优先。
			if (span.attributes[key] === undefined) span.setAttribute(key, value);
		}
	}

	onEnd(): void {}
	async forceFlush(): Promise<void> {}
	async shutdown(): Promise<void> {}
}
