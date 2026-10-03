import { normalizeProviderId } from "./ids";
import { getProviderCollectionSync } from "./model-registry";

/**
 * `local-auth` provider 借用凭据的本地 CLI。Provider 在
 * 目录中声明它（`metadata.localCliCommand` 加 `docsUrl`），使
 * 宿主无需知道任何 provider id 就能检查就绪状态并
 * 指向安装页面。
 */
export interface ProviderLocalCli {
	/** 要探测的可执行文件，例如 `codex` 或 `claude`。 */
	command: string;
	/** 机器上还没有它的用户应被引导到的位置。 */
	docsUrl?: string;
}

/**
 * 解析 provider 认证所借助的本地 CLI；当它未声明任何 CLI 时
 * 返回 `undefined`——包括凭据来自宿主无法探测
 * 之处的 `local-auth` provider。
 */
export function resolveProviderLocalCli(
	providerId: string,
): ProviderLocalCli | undefined {
	const provider = getProviderCollectionSync(
		normalizeProviderId(providerId.trim()),
	)?.provider;
	const command = provider?.metadata?.localCliCommand;
	if (typeof command !== "string" || command.trim().length === 0) {
		return undefined;
	}
	return { command: command.trim(), docsUrl: provider?.docsUrl };
}
