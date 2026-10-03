export function formatFileContentBlock(path: string, content: string): string {
	return `<file_content path="${path}">\n${content}\n</file_content>`;
}

export function formatUserInputBlock(
	input: string,
	mode: "act" | "plan" | "yolo" = "act",
): string {
	return `<user_input mode="${mode}">${input}</user_input>`;
}

export function formatUserCommandBlock(input: string, slash: string): string {
	return `<user_command slash="${slash}">${input}</user_command>`;
}

// 与 formatUserInputBlock 写入的内容完全一致（小写标签、小写
// mode 值），但使用搜索而非锚定：持久化的用户内容可能
// 在包装器周围携带前置的 <mode_notice> 元素或尾部附件块。
const USER_INPUT_MODE_RE = /<user_input\b[^>]*\bmode="(act|plan|yolo)"/;

/**
 * 从持久化用户消息的 <user_input mode="..."> 包装器中恢复
 * 其发送时的智能体模式。当输入未包装时返回 undefined
 *（纯文本、user_command 信封、旧版转录）。
 */
export function parseUserInputMode(
	input?: string,
): "act" | "plan" | "yolo" | undefined {
	const match = USER_INPUT_MODE_RE.exec(input ?? "");
	return match ? (match[1] as "act" | "plan" | "yolo") : undefined;
}

/**
 * 标记对话中用户在 plan 与 act 模式之间切换的确切位置。
 * 前置到切换后发送的首条用户消息。它能在 normalizeUserInput 后存活
 *（因此 prepareTurnInput 中的出站清理会将其交付给模型），
 * 并在显示边界被 stripModeNotices 从转录显示中隐藏。
 */
export function formatModeSwitchNotice(
	from: "act" | "plan",
	to: "act" | "plan",
): string {
	return `<mode_notice>The user switched from ${from} mode to ${to} mode before sending this message.</mode_notice>`;
}

export type ModeSwitchNotice = {
	from: "act" | "plan";
	to: "act" | "plan";
};

/**
 * 跟踪用户发起的模式切换，使下一条用户消息可以携带
 * 标记它的 <mode_notice>。只应记录 UI 切换：
 * 模型发起的 switch_to_act_mode 路径已通过
 * 继续提示词自我声明。往返（发送任何内容前 plan -> act -> plan）
 * 会相互抵消，因为模型最后看到的模式实际上
 * 从未改变。
 */
export function createModeSwitchNoticeTracker() {
	let pending: ModeSwitchNotice | null = null;
	return {
		record(from: "act" | "plan", to: "act" | "plan"): void {
			if (from === to) {
				return;
			}
			if (pending) {
				pending = pending.from === to ? null : { from: pending.from, to };
				return;
			}
			pending = { from, to };
		},
		consume(): ModeSwitchNotice | null {
			const notice = pending;
			pending = null;
			return notice;
		},
	};
}

export type ModeSwitchNoticeTracker = ReturnType<
	typeof createModeSwitchNoticeTracker
>;

export type UserCommandEnvelope = {
	slash: string;
	content: string;
};

function extractFullTagContent(
	input: string,
	tag: string,
): { attrs: string; content: string } | undefined {
	const trimmed = input.trim();
	const match = new RegExp(
		`^<${tag}\\b([^>]*)>([\\s\\S]*?)<\\/${tag}>$`,
		"i",
	).exec(trimmed);
	if (!match) {
		return undefined;
	}
	return {
		attrs: match[1] ?? "",
		content: match[2] ?? "",
	};
}

function readAttribute(attrs: string, key: string): string | undefined {
	const match = new RegExp(`${key}="([^"]+)"`, "i").exec(attrs);
	return match?.[1]?.trim() || undefined;
}

export function parseUserCommandEnvelope(
	input?: string,
): UserCommandEnvelope | undefined {
	if (!input?.trim()) {
		return undefined;
	}
	const extracted = extractFullTagContent(input, "user_command");
	if (!extracted) {
		return undefined;
	}
	const slash = readAttribute(extracted.attrs, "slash");
	if (!slash) {
		return undefined;
	}
	return {
		slash,
		content: extracted.content.trim(),
	};
}

export function normalizeUserInput(input?: string): string {
	if (!input?.trim()) return "";
	let next = input.trim();
	for (const tag of ["user_input", "user_command"] as const) {
		const extracted = xmlTagsRemoval(next, tag);
		next = (
			extracted !== next
				? extracted
				: next.replace(new RegExp(`<${tag}[^>]*>`, "g"), "")
		).trim();
	}
	return next;
}

/**
 * 移除运行时生成的 <mode_notice> 元素（含内容）：它们
 * 不是用户输入的文本，不能以此渲染。有意不放入
 * normalizeUserInput——那个函数也会在宿主包装提示词之前
 * 清理出站提示（prepareTurnInput），在那里剥离会在
 * 模型看到通知之前将其删除。
 */
export function stripModeNotices(input?: string): string {
	if (!input?.trim()) return "";
	return removeTagElements(input, "mode_notice").trim();
}

// 基于 indexOf 而非正则：惰性 dot-all 模式会从每个未匹配的开标签
// 重新扫描到字符串末尾，这对对抗性转录内容
// 是多项式复杂度（CodeQL js/polynomial-redos）。
function removeTagElements(input: string, tag: string): string {
	const open = `<${tag}>`;
	const close = `</${tag}>`;
	let result = input;
	let start = result.indexOf(open);
	while (start !== -1) {
		const end = result.indexOf(close, start + open.length);
		if (end === -1) {
			break;
		}
		result = result.slice(0, start) + result.slice(end + close.length);
		start = result.indexOf(open, start);
	}
	return result;
}

export function formatDisplayUserInput(input?: string): string {
	const normalized = stripModeNotices(normalizeUserInput(input));
	const envelope = parseUserCommandEnvelope(input);
	if (!envelope) {
		return normalized;
	}
	if (envelope.slash.toLowerCase() === "team") {
		const prefix = "spawn a team of agents for the following task:";
		const stripped = normalized.toLowerCase().startsWith(prefix)
			? normalized.slice(prefix.length).trim()
			: normalized;
		return stripped ? `/team ${stripped}` : "/team";
	}
	return normalized ? `/${envelope.slash} ${normalized}` : `/${envelope.slash}`;
}

export const SESSION_SEARCH_TITLE_MAX_LENGTH = 240;
export const SESSION_SEARCH_PREVIEW_MAX_LENGTH = 480;

function compactSessionSearchText(input: string, maxLength: number): string {
	const compact = input.replace(/\s+/gu, " ").trim();
	if (compact.length <= maxLength) return compact;
	return `${compact.slice(0, maxLength - 1).trimEnd()}…`;
}

export function formatSessionSearchTitle(input?: string): string {
	return compactSessionSearchText(
		formatDisplayUserInput(input),
		SESSION_SEARCH_TITLE_MAX_LENGTH,
	);
}

export function formatSessionSearchPreview(
	role: string,
	input?: string,
): string {
	const trimmed = input?.trim() ?? "";
	const normalizedRole = role.toLowerCase();
	const display =
		normalizedRole === "user" || normalizedRole === "session"
			? formatDisplayUserInput(trimmed)
			: trimmed;
	return compactSessionSearchText(display, SESSION_SEARCH_PREVIEW_MAX_LENGTH);
}

export function xmlTagsRemoval(input?: string, tag?: string): string {
	if (!input?.trim()) return "";
	if (!tag) return input;
	const regex = new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)</${tag}>`, "g");
	return input.replace(regex, "$1");
}
