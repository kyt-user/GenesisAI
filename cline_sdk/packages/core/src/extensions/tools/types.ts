/**
 * 默认工具的类型
 *
 * 执行器、配置和结果的类型定义。
 */

import type {
	AgentToolContext,
	ImageContent,
	ITelemetryService,
	TextContent,
} from "@cline/shared";
import type {
	ApplyPatchInput,
	EditFileInput,
	ReadFileRequest,
	StructuredCommandInput,
} from "./schemas";

// =============================================================================
// 工具结果类型
// =============================================================================

/**
 * 单次工具操作的结果
 */
export interface ToolOperationResult {
	/** 被执行的查询/输入 */
	query: string;
	/** 结果内容（如果成功） */
	result: unknown;
	/** 错误消息（如果失败） */
	error?: string;
	/** 操作是否成功 */
	success: boolean;
	/** 耗时（毫秒） */
	duration?: number;
}

export type FileReadResultContent = string | Array<TextContent | ImageContent>;

// =============================================================================
// 执行器接口
// =============================================================================

/**
 * 读取文件的执行器
 *
 * @param request - 文件路径与可选的要读取的闭区间行范围
 * @param context - 工具执行上下文
 * @returns 文件内容字符串
 */
export type FileReadExecutor = (
	request: ReadFileRequest,
	context: AgentToolContext,
) => Promise<FileReadResultContent>;

/**
 * 搜索代码库的执行器
 *
 * @param query - 要搜索的正则模式
 * @param cwd - 搜索的当前工作目录
 * @param context - 工具执行上下文
 * @returns 格式化字符串形式的搜索结果
 */
export type SearchExecutor = (
	query: string,
	cwd: string,
	context: AgentToolContext,
) => Promise<string>;

/**
 * 运行 shell 命令的执行器
 *
 * @param command - 要执行的 shell 命令
 * @param cwd - 执行的当前工作目录
 * @param context - 工具执行上下文
 * @returns 命令输出（stdout）
 */
export type ShellExecutor = (
	command: string | StructuredCommandInput,
	cwd: string,
	context: AgentToolContext,
) => Promise<string>;

/**
 * 获取网页内容的执行器
 *
 * @param url - 要获取的 URL
 * @param prompt - 针对内容的分析提示词
 * @param context - 工具执行上下文
 * @returns 分析/提取出的内容
 */
export type WebFetchExecutor = (
	url: string,
	prompt: string,
	context: AgentToolContext,
) => Promise<string>;

/**
 * 编辑文件的执行器
 *
 * @param input - 编辑器命令输入
 * @param cwd - 文件系统操作的当前工作目录
 * @param context - 工具执行上下文
 * @returns 格式化后的操作结果字符串
 */
export type EditorExecutor = (
	input: EditFileInput,
	cwd: string,
	context: AgentToolContext,
) => Promise<string>;

/**
 * apply_patch 操作的执行器
 *
 * @param input - apply_patch 命令负载
 * @param cwd - 文件系统操作的当前工作目录
 * @param context - 工具执行上下文
 * @returns 格式化后的操作结果字符串
 */
export type ApplyPatchExecutor = (
	input: ApplyPatchInput,
	cwd: string,
	context: AgentToolContext,
) => Promise<string>;

/**
 * 调用已配置技能的执行器
 *
 * @param skill - 要调用的技能名称
 * @param args - 技能的可选参数
 * @param context - 工具执行上下文
 * @returns 技能加载/调用结果
 */
export type SkillsExecutor = (
	skill: string,
	args: string | undefined,
	context: AgentToolContext,
) => Promise<string>;

/**
 * 提出单个带可选项的追问的执行器
 *
 * @param question - 面向用户的单个澄清问题
 * @param options - 2-5 个可选择的回答选项
 * @param context - 工具执行上下文
 * @returns 执行器特定的结果负载
 */
export type AskQuestionExecutor = (
	question: string,
	options: string[],
	context: AgentToolContext,
) => Promise<string>;

/**
 * SkillsExecutor 暴露给客户端/UI 的技能元数据
 */
export interface SkillsExecutorSkillMetadata {
	/** 规范化技能 id（通常是小写名称） */
	id: string;
	/** 技能的显示名称 */
	name: string;
	/** 可选的简短描述 */
	description?: string;
	/** 已配置但被有意禁用时为 true */
	disabled: boolean;
}

/**
 * 可调用的执行器，同时可以暴露已配置的技能元数据。
 */
export interface SkillsExecutorWithMetadata {
	(
		skill: string,
		args: string | undefined,
		context: AgentToolContext,
	): Promise<string>;
	configuredSkills?: SkillsExecutorSkillMetadata[];
}

/**
 * 验证用户对问题回答的执行器
 *
 * @param summary - 解决方案及所采取步骤的摘要
 * @param verified - 布尔值，表示解决方案是否已验证
 * @param context - 工具执行上下文
 * @returns 执行器特定的结果负载
 */
export type VerifySubmitExecutor = (
	summary: string,
	verified: boolean,
	context: AgentToolContext,
) => Promise<string>;

/**
 * 所有工具执行器的集合
 */
export interface ToolExecutors {
	/** 文件读取实现 */
	readFile?: FileReadExecutor;
	/** 代码库搜索实现 */
	search?: SearchExecutor;
	/** shell 命令执行实现 */
	bash?: ShellExecutor;
	/** 网页内容获取实现 */
	webFetch?: WebFetchExecutor;
	/** 文件系统编辑器实现 */
	editor?: EditorExecutor;
	/** apply_patch 实现 */
	applyPatch?: ApplyPatchExecutor;
	/** 技能调用实现 */
	skills?: SkillsExecutorWithMetadata;
	/** 追问实现 */
	askQuestion?: AskQuestionExecutor;
	/** 最终提交实现 */
	submit?: VerifySubmitExecutor;
}

// =============================================================================
// Tool Configuration
// =============================================================================

/**
 * 可用默认工具的名称
 */
export type DefaultToolName =
	| "read_files"
	| "search_codebase"
	| "run_commands"
	| "fetch_web_content"
	| "apply_patch"
	| "editor"
	| "skills"
	| "ask_question"
	| "submit_and_exit";

/**
 * 启用/禁用默认工具的配置
 */
export interface DefaultToolsConfig {
	/**
	 * 宿主遥测服务，在工具构建时注入。会发出运行遥测的工具
	 *（例如 run_commands 超时）通过闭包捕获此服务。
	 * 它是活的宿主对象，绝不能出现在每次调用的
	 * AgentToolContext 上，后者会经 JSON IPC 跨越进程边界。
	 */
	telemetry?: ITelemetryService;

	/**
	 * 启用 read_files 工具
	 * @default true
	 */
	enableReadFiles?: boolean;

	/**
	 * 启用 search_codebase 工具
	 * @default true
	 */
	enableSearch?: boolean;

	/**
	 * 启用 run_commands 工具
	 * @default true
	 */
	enableBash?: boolean;

	/**
	 * 启用 fetch_web_content 工具
	 * @default true
	 */
	enableWebFetch?: boolean;

	/**
	 * 启用 apply_patch 工具
	 * @default true
	 */
	enableApplyPatch?: boolean;

	/**
	 * 启用 editor 工具
	 * @default true
	 */
	enableEditor?: boolean;

	/**
	 * 启用 skills 工具
	 * @default true
	 */
	enableSkills?: boolean;

	/**
	 * 启用 ask_followup_question 工具
	 * @default true
	 */
	enableAskQuestion?: boolean;

	/**
	 * 启用 submit_and_exit 工具
	 * @default false
	 */
	enableSubmitAndExit?: boolean;

	/**
	 * 需要它的工具所使用的当前工作目录
	 */
	cwd?: string;

	/**
	 * run_commands 执行器将使用的 shell 可执行文件（名称或完整路径）。
	 * 工具描述会告诉模型应编写哪种 shell 语法，因此它
	 * 必须与执行器上配置的 shell 一致。
	 * @default getDefaultShell(process.platform) —— Unix 上为 "/bin/bash"，Windows 上为 "powershell"
	 */
	shell?: string;

	/**
	 * 文件读取操作的超时（毫秒）
	 * @default 10000
	 */
	fileReadTimeoutMs?: number;

	/**
	 * bash 命令执行的超时（毫秒）
	 * @default 30000
	 */
	bashTimeoutMs?: number;

	/**
	 * 网页获取操作的超时（毫秒）
	 * @default 30000
	 */
	webFetchTimeoutMs?: number;

	/**
	 * 搜索操作的超时（毫秒）
	 * @default 30000
	 */
	searchTimeoutMs?: number;

	/**
	 * apply_patch 操作的超时（毫秒）
	 * @default 30000
	 */
	applyPatchTimeoutMs?: number;

	/**
	 * 编辑器操作的超时（毫秒）
	 * @default 30000
	 */
	editorTimeoutMs?: number;

	/**
	 * 技能操作的超时（毫秒）
	 * @default 15000
	 */
	skillsTimeoutMs?: number;

	/**
	 * submit_and_exit 操作的超时（毫秒）
	 * @default 15000
	 */
	submitTimeoutMs?: number;
}

/**
 * 创建默认工具的选项
 */
export interface CreateDefaultToolsOptions extends DefaultToolsConfig {
	/**
	 * 各工具的执行器实现
	 * 只有提供了执行器的工具才可用
	 */
	executors: ToolExecutors;
}
