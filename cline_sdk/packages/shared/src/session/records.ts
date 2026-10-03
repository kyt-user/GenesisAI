export const SESSION_STATUS_VALUES = [
	// 会话可接受输入且没有回合在执行。既用于等待首个提示词的
	// 新创建会话，也用于回合完成后；无提示词/消息的空闲会话
	// 实际上等同于新会话。
	"idle",
	// 一个回合正在积极执行。从首个提示词派发起直到
	// 运行时发射终止结果或取消。
	"running",
	// 工作正在等待交付或恢复，但当前未在执行。
	// 用于排队的提示词或运行时交接；工作开始时转为 running，
	// 被丢弃时转为终止状态。
	"pending",
	// 会话达到正常的终止结果。当运行时不再接受另一个回合时，
	// 与 endedAt/exitCode 元数据一起使用。
	"completed",
	// 会话因执行或设置失败而终止。当失败不是用户取消时
	// 使用，并保留诊断元数据。
	"failed",
	// 会话被用户/系统有意停止或中止。对于预期的中断使用此状态
	// 而非 failed，并记录退出原因。
	"cancelled",
] as const;

export type SharedSessionStatus = (typeof SESSION_STATUS_VALUES)[number];

export interface SessionLineage {
	parentSessionId?: string;
	agentId?: string;
	parentAgentId?: string;
	conversationId?: string;
	isSubagent: boolean;
}

export interface SessionRuntimeRecordShape extends SessionLineage {
	source: string;
	pid?: number;
	startedAt: string;
	endedAt?: string | null;
	exitCode?: number | null;
	status: SharedSessionStatus;
	interactive: boolean;
	provider: string;
	model: string;
	cwd: string;
	workspaceRoot: string;
	teamName?: string;
	enableTools: boolean;
	enableSpawn: boolean;
	enableTeams: boolean;
	prompt?: string;
	metadata?: Record<string, unknown>;
	hookPath?: string;
	messagesPath?: string;
	updatedAt: string;
}
