/**
 * 自定义处理器注册表
 *
 * 允许用户注册扩展 BaseHandler 的自定义处理器。
 * 适用于需要本包未包含的依赖的 provider
 * （例如需要 vscode 包的 VSCode LM 处理器）。
 *
 * @example
 * ```typescript
 * import { registerHandler, BaseHandler, type ProviderConfig, type ApiStream, type Message } from "@cline/providers"
 * import * as vscode from "vscode"
 *
 * class VSCodeLmHandler extends BaseHandler {
 *   async *createMessage(systemPrompt: string, messages: Message[]): ApiStream {
 *     // 使用 vscode.lm API 的实现
 *   }
 * }
 *
 * // 注册处理器
 * registerHandler("vscode-lm", (config) => new VSCodeLmHandler(config))
 *
 * // 现在 createHandler 将为 "vscode-lm" 使用你的自定义处理器
 * const handler = createHandler({ providerId: "vscode-lm", modelId: "copilot" })
 * ```
 */

import type {
	ApiHandler,
	HandlerFactory,
	LazyHandlerFactory,
	ProviderConfig,
} from "./types";

/**
 * 注册表条目，可以是同步或异步工厂
 */
type RegistryEntry = {
	factory: HandlerFactory<ProviderConfig> | LazyHandlerFactory<ProviderConfig>;
	isAsync: boolean;
};

/**
 * 自定义处理器的内部注册表
 */
const customHandlerRegistry = new Map<string, RegistryEntry>();

/**
 * 为 provider ID 注册自定义处理器工厂
 *
 * 用于为需要本包未捆绑的外部依赖的 provider 添加处理器，
 * 或覆盖内置处理器。
 *
 * @param providerId - 要注册的 provider ID（可以是已有或新的）
 * @param factory - 创建处理器的工厂函数
 *
 * @example
 * ```typescript
 * // 简单注册
 * registerHandler("my-provider", (config) => new MyHandler(config))
 *
 * // 覆盖内置处理器
 * registerHandler("anthropic", (config) => new MyCustomAnthropicHandler(config))
 * ```
 */
export function registerHandler(
	providerId: string,
	factory: HandlerFactory<ProviderConfig>,
): void {
	customHandlerRegistry.set(providerId, { factory, isAsync: false });
}

/**
 * 注册异步处理器工厂以支持懒加载
 *
 * 当你的处理器有重型依赖、应仅在需要时加载时使用。
 *
 * @param providerId - 要注册的 provider ID
 * @param factory - 创建处理器的异步工厂函数
 *
 * @example
 * ```typescript
 * registerAsyncHandler("heavy-provider", async (config) => {
 *   const { HeavyHandler } = await import("./heavy-handler")
 *   return new HeavyHandler(config)
 * })
 * ```
 */
export function registerAsyncHandler(
	providerId: string,
	factory: LazyHandlerFactory<ProviderConfig>,
): void {
	customHandlerRegistry.set(providerId, { factory, isAsync: true });
}

/**
 * 检查某个 provider ID 是否注册了自定义处理器
 *
 * @param providerId - 要检查的 provider ID
 */
export function hasRegisteredHandler(providerId: string): boolean {
	return customHandlerRegistry.has(providerId);
}

/**
 * 获取已注册的处理器（内部使用）
 *
 * @param providerId - 要获取的 provider ID
 * @param config - 传递给工厂的配置
 * @returns 处理器实例；未注册时为 undefined
 */
export function getRegisteredHandler(
	providerId: string,
	config: ProviderConfig,
): ApiHandler | undefined {
	const entry = customHandlerRegistry.get(providerId);
	if (!entry) {
		return undefined;
	}

	if (entry.isAsync) {
		throw new Error(
			`Handler for "${providerId}" is registered as async. Use getRegisteredHandlerAsync() or createHandlerAsync() instead.`,
		);
	}

	return (entry.factory as HandlerFactory<ProviderConfig>)(config);
}

/**
 * 异步获取已注册的处理器（内部使用）
 *
 * @param providerId - 要获取的 provider ID
 * @param config - 传递给工厂的配置
 * @returns 处理器实例；未注册时为 undefined
 */
export async function getRegisteredHandlerAsync(
	providerId: string,
	config: ProviderConfig,
): Promise<ApiHandler | undefined> {
	const entry = customHandlerRegistry.get(providerId);
	if (!entry) {
		return undefined;
	}

	if (entry.isAsync) {
		return (entry.factory as LazyHandlerFactory<ProviderConfig>)(config);
	}

	return (entry.factory as HandlerFactory<ProviderConfig>)(config);
}

/**
 * 检查已注册的处理器是否为异步
 *
 * @param providerId - 要检查的 provider ID
 */
export function isRegisteredHandlerAsync(providerId: string): boolean {
	const entry = customHandlerRegistry.get(providerId);
	return entry?.isAsync ?? false;
}
