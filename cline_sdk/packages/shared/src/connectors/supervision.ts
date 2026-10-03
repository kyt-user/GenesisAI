/**
 * Hub 拥有的连接器监督类型。
 *
 * 连接器是持有第三方凭据的独立进程（Slack socket、
 * Telegram 轮询循环）。Hub 监督这些进程：它是唯一有权
 * 决定哪些实例可以运行的权威，在它们死亡时回收状态，
 * 并以退避策略重启它们。这些记录是它对此工作的报告。
 */

/** 受监督连接器进程如何进入 hub 的监管。 */
export type SupervisedConnectorOrigin =
	/** 当前 hub 生成了它，因此有活动子进程句柄。 */
	| "spawned"
	/** 它早于当前 hub，当前 hub 通过 pid 从状态文件中收养了它。 */
	| "adopted";

export type SupervisedConnectorState =
	/** 就 hub 所知进程存活。 */
	| "running"
	/** 已死亡，正在等待重启退避。 */
	| "backoff"
	/** 连续死亡次数过多；hub 已放弃重启。 */
	| "failed"
	/** 按请求停止；hub 不会重启它。 */
	| "stopped";

export type SupervisedConnectorRecord = {
	channel: string;
	instanceId: string;
	state: SupervisedConnectorState;
	origin: SupervisedConnectorOrigin;
	pid?: number;
	startedAt?: string;
	/** 尚未被稳定运行清除的连续重启次数。 */
	restarts: number;
	/** 下次重启尝试到期时间（处于 "backoff" 时）。 */
	nextRestartAt?: string;
	lastExitCode?: number;
	lastExitSignal?: string;
	lastError?: string;
};

export type ConnectorStartRequest = {
	channel: string;
	instanceId: string;
	/** 连接器 CLI 参数，不包括通道名称本身。 */
	args: string[];
	/**
	 * 替换运行中的实例而非报告已运行。
	 * 由 `connect --restart` 使用。
	 */
	restart?: boolean;
};

export type ConnectorStartResult = {
	/** 实例已运行且未设置 `restart` 时为 false。 */
	started: boolean;
	record: SupervisedConnectorRecord;
	/** `started` 为 false 时的原因。 */
	reason?: "already_running";
};

export type ConnectorStopRequest = {
	channel: string;
	instanceId: string;
	/** 同时停止自动重启此实例。默认为 true。 */
	disableAutostart?: boolean;
};

export type ConnectorStopResultPayload = {
	stopped: boolean;
	channel: string;
	instanceId: string;
};
