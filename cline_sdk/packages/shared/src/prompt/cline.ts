import type { WorkspaceContext } from "../extensions/context";
import { isClineProvider } from "../providers/utils";
import type { WorkspaceInfo } from "../session/workspace";
import { DEFAULT_CLINE_SYSTEM_PROMPTS } from "./system";

const WORKSPACE_CONFIGURATION_MARKER = "# Workspace Configuration";

/**
 * 解释运行时盖在用户消息上的 <user_input mode="..."> 包装器和
 * <mode_notice> 元素（prepareTurnInput / formatUserInputBlock）。
 * 包含在 plan 和 act 提示词中，使模型能解释模式切换
 * 和标记为另一模式的早期消息。YOLO 提示词省略这些
 * 说明，因为它们不使用 plan/act 工作流。
 */
export const MODE_TAG_INSTRUCTIONS = `# Plan / Act Modes

User messages arrive wrapped in a <user_input mode="..."> tag. The mode attribute is the interaction mode the user was in when they sent that message: "plan" means plan-mode constraints applied (explore, analyze, and align on a plan -- no edits or state-changing commands), while "act" (or "yolo") means implementation was allowed. If the mode attribute changes between messages, the user switched modes -- the newest message's mode is what governs right now, regardless of what earlier messages allowed. A <mode_notice> block inside a message marks exactly when such a switch happened.`;

/**
 * Plan 模式行为契约，当会话模式为 "plan" 时追加。
 * run_commands 有意在 plan 模式保持可用——它对于只读调查
 * 至关重要——因此契约必须明确说明那里仅限检查用途。
 * 提示词是第一道防线；plan 模式命令守卫 hook（由 core 运行时构建器
 * 为 plan 模式会话注册）是硬性后盾，在审批或执行前
 * 以工具错误拒绝文件编辑类的 run_commands 调用。
 */
const PLAN_MODE_INSTRUCTIONS_BASE = `# Plan Mode

You are in Plan mode. Your role is to explore, analyze, and plan -- not to execute.

- Read files, search the codebase, and gather context to understand the problem
- Ask clarifying questions when requirements are ambiguous
- Present your plan as a structured outline with clear steps
- Explain tradeoffs between different approaches when they exist
- Do NOT edit files, write code, run destructive commands, or make any changes
- Do NOT implement anything -- focus on understanding and alignment first

The run_commands tool remains available in plan mode strictly for read-only inspection -- listing files, searching (grep), reading configs, inspecting git history and diffs, checking tool versions, and the like. Never use it to change anything: no creating, modifying, or deleting files, no writing scripts that make changes, and no state-changing commands (installs, migrations, database or schema changes, container commands that mutate state, etc.). File-editing commands (rm/mv/cp, in-place edits like sed -i, output redirection to files outside /tmp, git commands that change the working tree, package installs) are hard-blocked in plan mode: they are not executed and return a tool error instead, so do not attempt them. If the task requires a mutation, put it in the plan; it happens only after the user switches to act mode.`;

export const PLAN_MODE_INSTRUCTIONS = `${PLAN_MODE_INSTRUCTIONS_BASE}

Once the user has reviewed your plan and explicitly approved it in a follow-up message, use the switch_to_act_mode tool to switch to act mode and begin implementation. Calling switch_to_act_mode immediately starts execution, so never call it in the same turn you present a plan and never treat the original task request as approval -- end your turn after presenting the plan and wait for the user's response.`;

/**
 * 不暴露 switch_to_act_mode 工具的宿主的 plan 模式契约
 *（VS Code 扩展，与旧版扩展行为一致）。模型
 * 必须引导用户切换 Plan/Act 开关，而不是调用
 * 其工具集中不存在的工具。
 */
export const PLAN_MODE_INSTRUCTIONS_MANUAL_SWITCH = `${PLAN_MODE_INSTRUCTIONS_BASE}

Once you have presented your plan, end your turn and wait for the user's response. You do NOT have the ability to switch to act mode yourself -- the user must do it manually with the Plan/Act toggle once they are satisfied with the plan. If the task requires tools that are only available in act mode, ask the user to "toggle to Act mode" (use those words).`;

function redactRemoteUrlCredentials(remote: string): string {
	const schemeEnd = remote.indexOf("://");
	if (schemeEnd < 1) return remote;

	const authorityStart = schemeEnd + 3;
	let authorityEnd = authorityStart;
	while (authorityEnd < remote.length) {
		const char = remote[authorityEnd];
		if (
			char === "/" ||
			char === "?" ||
			char === "#" ||
			char.charCodeAt(0) <= 32
		) {
			break;
		}
		authorityEnd++;
	}

	const userInfoEnd = remote.lastIndexOf("@", authorityEnd - 1);
	if (userInfoEnd < authorityStart) return remote;
	return remote.slice(0, authorityStart) + remote.slice(userInfoEnd + 1);
}

export function processWorkspaceInfo(info: WorkspaceInfo): string {
	return JSON.stringify(
		{
			workspaces: {
				[info.rootPath]: {
					hint: info.hint,
					associatedRemoteUrls: info.associatedRemoteUrls?.map(
						redactRemoteUrlCredentials,
					),
					latestGitCommitHash: info.latestGitCommitHash,
					latestGitBranchName: info.latestGitBranchName,
				},
			},
		},
		null,
		2,
	);
}

function buildWorkspaceMetadata(
	rootPath: string,
	workspaceName?: string,
	metadata?: string,
): string {
	if (metadata?.trim()?.includes(WORKSPACE_CONFIGURATION_MARKER)) {
		return metadata.trim();
	}
	const body =
		metadata ||
		JSON.stringify(
			{
				workspaces: {
					[rootPath]: {
						hint: workspaceName || rootPath.split("/").at(-1) || rootPath,
					},
				},
			},
			null,
			2,
		);
	return `\n${WORKSPACE_CONFIGURATION_MARKER}\n${body}`;
}

/**
 * 构建 Cline 系统提示词的选项。
 *
 * 扩展 WorkspaceContext，使调用方可以直接展开 ExtensionContext.workspace。
 * `workspaceRoot` 作为 `rootPath` 的别名被接受，以支持
 * 显式设置它的现有调用点。
 */
export interface ClineSystemPromptOptions
	extends Omit<WorkspaceContext, "rootPath"> {
	/**
	 * 工作区根路径。接受 `rootPath`（来自 WorkspaceContext/WorkspaceInfo）
	 * 或 `workspaceRoot`（旧版别名）——提供哪个就用哪个。
	 */
	rootPath?: string;
	/** rootPath 的别名 — 为与现有调用点的向后兼容而保留 */
	workspaceRoot?: string;
	/** 每请求的系统提示词覆盖 */
	overridePrompt?: string;
	/** Provider ID — 用于门控 Cline 专属元数据注入 */
	providerId?: string;
	/**
	 * 宿主是否在 plan 模式暴露 switch_to_act_mode 工具。
	 * 默认为 true（CLI 行为）。要求用户自行切换
	 * Plan/Act 开关的宿主（VS Code 扩展）将其设为 false，使
	 * plan 模式契约引导模型请求用户，而不是调用
	 * 不在其工具集中的工具。
	 */
	planModeSwitchTool?: boolean;
}

export function buildClineSystemPrompt(
	options: ClineSystemPromptOptions,
): string {
	const {
		ide = "Terminal Shell",
		mode,
		platform = "unknown",
		workspaceName,
		metadata,
		rules,
		overridePrompt,
		providerId,
		planModeSwitchTool = true,
	} = options;
	const workspaceRoot = options.workspaceRoot ?? options.rootPath ?? "";
	const isCline = isClineProvider(providerId || "");

	if (overridePrompt?.trim()) {
		const trimmed = overridePrompt.trim();
		if (
			isCline &&
			metadata?.trim() &&
			!trimmed.includes(WORKSPACE_CONFIGURATION_MARKER)
		) {
			return `${trimmed}\n\n${buildWorkspaceMetadata(workspaceRoot, workspaceName, metadata)}`.trim();
		}
		return trimmed;
	}

	const basePrompt =
		mode === "yolo"
			? DEFAULT_CLINE_SYSTEM_PROMPTS.YOLO
			: DEFAULT_CLINE_SYSTEM_PROMPTS.ACT;

	// 跨宿主保持模式语义共享，但在 YOLO 模式省略 plan/act 工作流
	// 说明。调用方规则在所有模式下适用。
	const effectiveRules = [
		rules,
		mode === "yolo" ? undefined : MODE_TAG_INSTRUCTIONS,
		mode === "plan"
			? planModeSwitchTool
				? PLAN_MODE_INSTRUCTIONS
				: PLAN_MODE_INSTRUCTIONS_MANUAL_SWITCH
			: undefined,
	]
		.filter(Boolean)
		.join("\n\n");

	return basePrompt
		.replace("{{PLATFORM_NAME}}", platform)
		.replace("{{CWD}}", workspaceRoot)
		.replace("{{CURRENT_DATE}}", new Date().toLocaleDateString())
		.replace("{{IDE_NAME}}", ide)
		.replace(
			"{{CLINE_METADATA}}",
			isCline
				? buildWorkspaceMetadata(workspaceRoot, workspaceName, metadata)
				: "",
		)
		.replace("{{CLINE_RULES}}", effectiveRules)
		.trim();
}
