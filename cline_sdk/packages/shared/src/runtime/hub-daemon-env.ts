export const CLINE_RUN_AS_HUB_DAEMON_ENV = "CLINE_RUN_AS_HUB_DAEMON";
export const CLINE_CONNECTOR_CLI_LAUNCH_ENV = "CLINE_CONNECTOR_CLI_LAUNCH";
export const CLINE_CONNECTOR_STARTING_INSTANCE_ENV =
	"CLINE_CONNECTOR_STARTING_INSTANCE";
export const CLINE_CONNECTOR_SUPERVISED_ENV = "CLINE_CONNECTOR_SUPERVISED";

export interface ConnectorCliLaunchSpec {
	launcher: string;
	connectArgsPrefix: string[];
	cwd: string;
}

/** 标识一个连接器实例：一个适配器通道加上其实例 id。 */
export interface ConnectorInstanceRef {
	channel: string;
	instanceId: string;
}

/**
 * {@link claimHubDaemonProcess} 的锁定结果，使哨兵可以从
 * 环境中移除而进程仍然知道自己的身份。
 */
let claimedHubDaemonProcess: boolean | undefined;

/**
 * 从环境中取出守护进程哨兵，记住其值。
 *
 * 哨兵选择共享 CLI 二进制启动哪种身份，因此它不能
 * 存活于那个决定之后：hub 守护进程托管会话运行时，会话生成的每个
 * 进程——Agent shell 命令、MCP 服务器、hooks、插件
 * 沙箱——都会继承其环境。继承的哨兵使每个
 * 进程尝试成为 hub 守护进程而非运行命令，并因
 * 真实 hub 的 EADDRINUSE 而死亡。观察到从
 * Slack 连接器 Agent 调用的每次 `cline` 调用都失败，包括 `cline --help`，
 * 因为身份在任何参数解析之前就被选择。
 *
 * 从入口点调用一次，代替 {@link isHubDaemonProcess}。
 * 故意启动守护进程的生成路径在子环境上显式设置变量，
 * 因此不受影响。
 */
export function claimHubDaemonProcess(
	env: Record<string, string | undefined> = process.env,
): boolean {
	claimedHubDaemonProcess = env[CLINE_RUN_AS_HUB_DAEMON_ENV] === "1";
	delete env[CLINE_RUN_AS_HUB_DAEMON_ENV];
	return claimedHubDaemonProcess;
}

/**
 * 此进程是否为 hub 守护进程。
 *
 * 先读取锁存值，使调用方在 {@link claimHubDaemonProcess} 清理环境后
 * 仍得到正确答案——尤其是阻止守护进程生成另一个守护进程的
 * 防护。显式传入的环境总是逐字读取。
 */
export function isHubDaemonProcess(
	env?: Record<string, string | undefined>,
): boolean {
	if (env) {
		return env[CLINE_RUN_AS_HUB_DAEMON_ENV] === "1";
	}
	return (
		claimedHubDaemonProcess ?? process.env[CLINE_RUN_AS_HUB_DAEMON_ENV] === "1"
	);
}

export function setConnectorCliLaunchSpec(
	spec: ConnectorCliLaunchSpec,
	env: Record<string, string | undefined> = process.env,
): void {
	env[CLINE_CONNECTOR_CLI_LAUNCH_ENV] = JSON.stringify(spec);
}

/** {@link claimSupervisedConnectorProcess} 的锁定结果。 */
let claimedSupervisedConnectorProcess: boolean | undefined;

/**
 * 从环境中取出受监督连接器标记，记住其值。
 *
 * 与 {@link claimHubDaemonProcess} 相同的危险：受监督连接器托管
 * Agent 会话，它们生成的一切——shell 命令、MCP 服务器、hooks
 * ——继承其环境。继承的标记使嵌套的 `cline connect`
 * 认为自己是 hub 正在跟踪的进程，因此它在该 shell 命令的前台
 * 运行连接器而不是将其交给 hub。
 *
 * 从入口点调用一次，代替
 * {@link isSupervisedConnectorProcess}。监督器在子环境上显式设置标记，
 * 因此不受影响。
 */
export function claimSupervisedConnectorProcess(
	env: Record<string, string | undefined> = process.env,
): boolean {
	claimedSupervisedConnectorProcess =
		env[CLINE_CONNECTOR_SUPERVISED_ENV] === "1";
	delete env[CLINE_CONNECTOR_SUPERVISED_ENV];
	return claimedSupervisedConnectorProcess;
}

/**
 * hub 监督器启动的连接器进程中为 true。
 *
 * 此类进程必须自行运行连接器，而非像用户调用的后台 `connect`
 * 那样——请求 hub 启动它（会直接循环回这里）或生成自己的独立子进程
 * 然后退出（会让监督器持有已消失进程的句柄）。
 *
 * 先读取锁存值，使调用方在
 * {@link claimSupervisedConnectorProcess} 清理环境后仍得到正确答案。
 * 显式传入的环境总是逐字读取。
 */
export function isSupervisedConnectorProcess(
	env?: Record<string, string | undefined>,
): boolean {
	if (env) {
		return env[CLINE_CONNECTOR_SUPERVISED_ENV] === "1";
	}
	return (
		claimedSupervisedConnectorProcess ??
		process.env[CLINE_CONNECTOR_SUPERVISED_ENV] === "1"
	);
}

/**
 * 宣告此进程正在启动的连接器实例。
 *
 * 连接器启动自己的 hub 守护进程，守护进程随后重新连接每个
 * 持久化的连接器。正在启动的实例在守护进程启动时尚未注册为
 * 活动，因此没有此标记，守护进程会启动它的第二个副本——
 * 两个进程持有相同的 bot token。守护进程从生成它的连接器
 * 继承此变量，因此它可以区分「正在启动我的连接器」和
 * 「上一个 hub 会话遗留的连接器」，后者确实需要重启。
 */
export function setStartingConnectorInstance(
	ref: ConnectorInstanceRef,
	env: Record<string, string | undefined> = process.env,
): void {
	env[CLINE_CONNECTOR_STARTING_INSTANCE_ENV] = JSON.stringify(ref);
}

export function readStartingConnectorInstance(
	env: Record<string, string | undefined> = process.env,
): ConnectorInstanceRef | undefined {
	const raw = env[CLINE_CONNECTOR_STARTING_INSTANCE_ENV];
	if (!raw) {
		return undefined;
	}
	try {
		const parsed = JSON.parse(raw) as Partial<ConnectorInstanceRef>;
		if (
			typeof parsed.channel !== "string" ||
			!parsed.channel.trim() ||
			typeof parsed.instanceId !== "string" ||
			!parsed.instanceId.trim()
		) {
			return undefined;
		}
		return { channel: parsed.channel, instanceId: parsed.instanceId };
	} catch {
		return undefined;
	}
}

export function readConnectorCliLaunchSpec(
	env: Record<string, string | undefined> = process.env,
): ConnectorCliLaunchSpec | undefined {
	const raw = env[CLINE_CONNECTOR_CLI_LAUNCH_ENV];
	if (!raw) {
		return undefined;
	}
	try {
		const parsed = JSON.parse(raw) as Partial<ConnectorCliLaunchSpec>;
		if (
			typeof parsed.launcher !== "string" ||
			!parsed.launcher.trim() ||
			!Array.isArray(parsed.connectArgsPrefix) ||
			!parsed.connectArgsPrefix.every((arg) => typeof arg === "string") ||
			typeof parsed.cwd !== "string" ||
			!parsed.cwd.trim()
		) {
			return undefined;
		}
		return {
			launcher: parsed.launcher,
			connectArgsPrefix: parsed.connectArgsPrefix,
			cwd: parsed.cwd,
		};
	} catch {
		return undefined;
	}
}
