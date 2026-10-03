import { execFile as execFileCallback, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readdir, rm, stat, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import type { AgentHooks, BasicLogger, ITelemetryService } from "@cline/shared";
import { resolveClineDataDir } from "@cline/shared/storage";
import { countUserRunMessages } from "../session/user-run-messages";

const execFile = promisify(execFileCallback);
const CHECKPOINT_STASH_MESSAGE_PREFIX = "cline checkpoint session=";
const LS_FILES_MAX_BUFFER = 1024 * 1024 * 64;
/**
 * mtime 早于此值的 scratch 目录会被机会性地清理。
 * 每次快照都会重写索引（因而触及该目录），所以任何活
 * 会话每轮都会刷新其目录；只有空闲这么久 ——
 * 通常是从未被显式删除的会话 —— 才会受影响。
 */
const SCRATCH_DIR_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;

export function isCheckpointStashMessage(message: string): boolean {
	return message.includes(CHECKPOINT_STASH_MESSAGE_PREFIX);
}

function checkpointScratchBaseDir(): string {
	return join(resolveClineDataDir(), "checkpoint-scratch");
}

/**
 * 每会话的 scratch 目录，存放用于快照未跟踪文件的持久化
 * `GIT_INDEX_FILE`。跨轮次持久化索引让 git 的 stat 缓存
 * 跳过对自上一个检查点以来未变更的未跟踪文件的重新读取
 *（和重新哈希）——没有它，每轮都会重新哈希工作区中
 * 每个未跟踪字节。
 *
 * 位于用户的 Cline 数据目录下（绝不用全局共享的 OS 临时目录：
 * 索引和 pathspec 文件会枚举工作区路径），并以 cwd + 会话 id 的
 * 哈希为键：哈希避免不同 id 之间的清洗碰撞，
 * 折叠进 cwd 则防止针对不同工作区恢复的会话
 * 继承外来索引。由 `deleteCheckpointRefs` 和
 * 基于年龄的清理器移除。
 */
export function checkpointScratchDir(cwd: string, sessionId: string): string {
	const key = createHash("sha256")
		.update(`${cwd}\0${sessionId}`)
		.digest("hex")
		.slice(0, 32);
	return join(checkpointScratchBaseDir(), key);
}

/**
 * 对从未被显式删除的会话的 scratch 目录进行尽力而为的清理。
 * 基目录下的一切都属于我们，因此年龄是唯一
 * 标准。错误（基目录缺失、与并发会话的竞争）会被
 * 忽略 —— 下一个 hook 实例会再试。
 */
async function pruneStaleScratchDirs(): Promise<void> {
	try {
		const base = checkpointScratchBaseDir();
		const cutoff = Date.now() - SCRATCH_DIR_MAX_AGE_MS;
		const entries = await readdir(base, { withFileTypes: true });
		await Promise.all(
			entries.map(async (entry) => {
				if (!entry.isDirectory()) return;
				const dir = join(base, entry.name);
				try {
					if ((await stat(dir)).mtimeMs < cutoff) {
						await rm(dir, { recursive: true, force: true });
					}
				} catch {
					// 与另一个进程竞争 —— 忽略。
				}
			}),
		);
	} catch {
		// 基目录缺失或不可读 —— 无需清理。
	}
}

export interface CheckpointEntry {
	ref: string;
	createdAt: number;
	runCount: number;
	kind?: "stash" | "commit";
}

export interface CheckpointMetadata {
	latest: CheckpointEntry;
	history: CheckpointEntry[];
}

type CreateCheckpointHooksOptions = {
	cwd: string;
	sessionId: string;
	logger?: BasicLogger;
	readSessionMetadata: () => Promise<Record<string, unknown> | undefined>;
	writeSessionMetadata: (
		metadata: Record<string, unknown>,
	) => Promise<void> | void;
	/**
	 * 可选的自定义检查点实现。提供时，内置的
	 * git stash/ref 逻辑被完全跳过，改为调用
	 * 此函数。返回 `undefined` 可跳过该轮的检查点写入。
	 */
	createCheckpoint?: (context: {
		cwd: string;
		sessionId: string;
		runCount: number;
	}) => Promise<CheckpointEntry | undefined> | CheckpointEntry | undefined;
	/**
	 * 每次内置快照尝试发出一个 `checkpoint.snapshot` 事件，
	 * 使快照成本与降级（HEAD 回退、被跳过的轮次）
	 * 在现场可观察，而不仅在本地日志中。属性携带
	 * outcome 与 duration —— 绝不携带文件路径或内容。
	 */
	telemetry?: Pick<ITelemetryService, "capture">;
};

function warn(logger: BasicLogger | undefined, message: string): void {
	logger?.log(message, { severity: "warn" });
}

function readCheckpointMetadata(
	metadata: Record<string, unknown> | undefined,
): CheckpointMetadata | undefined {
	const candidate = metadata?.checkpoint;
	if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
		return undefined;
	}
	const record = candidate as Partial<CheckpointMetadata>;
	if (!record.latest || !Array.isArray(record.history)) {
		return undefined;
	}
	const latest = record.latest as Partial<CheckpointEntry>;
	const history = record.history.filter(
		(entry): entry is CheckpointEntry =>
			!!entry &&
			typeof entry === "object" &&
			typeof (entry as Partial<CheckpointEntry>).ref === "string" &&
			typeof (entry as Partial<CheckpointEntry>).createdAt === "number" &&
			typeof (entry as Partial<CheckpointEntry>).runCount === "number",
	);
	if (
		typeof latest.ref !== "string" ||
		typeof latest.createdAt !== "number" ||
		typeof latest.runCount !== "number"
	) {
		return undefined;
	}
	return {
		latest: latest as CheckpointEntry,
		history,
	};
}

async function runGit(
	cwd: string,
	args: string[],
): Promise<{ stdout: string; stderr: string }> {
	const result = await execFile("git", ["-C", cwd, ...args], {
		windowsHide: true,
	});
	return {
		stdout: result.stdout.trim(),
		stderr: result.stderr.trim(),
	};
}

/**
 * 为每个触及 scratch 索引的命令固定配置。持久化索引
 * 会继承仓库配置让 git 写入其中的任何内容，某些
 * 设置会破坏跨轮次变更检测：`core.ignorestat=true` 将
 * 新增条目标记为 assume-unchanged，后续轮次便不再 stat 它们，
 * 被修改的文件在每个后续快照中都保留其第一轮内容。原始实现的
 * 一次性每轮索引在构造上即不受此影响。
 * `core.splitIndex` 还会把我们私有索引的 shared-index
 * 文件散布进用户的 `.git`。
 */
const SCRATCH_INDEX_GIT_CONFIG = [
	"-c",
	"core.ignorestat=false",
	"-c",
	"core.splitIndex=false",
];

async function runGitWithIndex(
	cwd: string,
	indexFile: string,
	args: string[],
): Promise<string> {
	const result = await execFile(
		"git",
		["-C", cwd, ...SCRATCH_INDEX_GIT_CONFIG, ...args],
		{
			windowsHide: true,
			maxBuffer: LS_FILES_MAX_BUFFER,
			env: { ...process.env, GIT_INDEX_FILE: indexFile },
		},
	);
	return result.stdout.trim();
}

/** 与 `runGitWithIndex` 类似，但将 `input` 送入子进程的 stdin。 */
function runGitWithIndexStdin(
	cwd: string,
	indexFile: string,
	args: string[],
	input: string,
): Promise<void> {
	return new Promise((resolve, reject) => {
		const child = spawn(
			"git",
			["-C", cwd, ...SCRATCH_INDEX_GIT_CONFIG, ...args],
			{
				windowsHide: true,
				env: { ...process.env, GIT_INDEX_FILE: indexFile },
				stdio: ["pipe", "ignore", "pipe"],
			},
		);
		let stderr = "";
		child.stderr.on("data", (chunk) => {
			stderr += chunk;
		});
		child.on("error", reject);
		child.on("close", (code) => {
			if (code === 0) {
				resolve();
			} else {
				reject(
					new Error(`git ${args[0]} exited with ${code}: ${stderr.trim()}`),
				);
			}
		});
		child.stdin.on("error", () => {
			// close 处理器报告真正的失败；没有这个监听器，
			// 在消费 stdin 之前退出的子进程会令进程崩溃（EPIPE）。
		});
		child.stdin.end(input);
	});
}

/**
 * 构建一个提交，其树仅包含当前未跟踪（但未被
 * git 忽略）的文件，不触碰工作树或真实索引。
 * 这镜像了 `git stash create --include-untracked` 记录的
 * 第三个父提交，而其他地方使用的普通 `git stash create` 会省略它。
 * 没有未跟踪文件可捕获时返回 `undefined`。
 */
async function createUntrackedParentCommit(
	cwd: string,
	scratchDir: string,
): Promise<string | undefined> {
	const listing = await execFile(
		"git",
		["-C", cwd, "ls-files", "--others", "--exclude-standard", "-z"],
		{ windowsHide: true, maxBuffer: LS_FILES_MAX_BUFFER },
	);
	const untrackedFiles = listing.stdout.split("\0").filter(Boolean);
	if (untrackedFiles.length === 0) {
		return undefined;
	}
	// 0700：索引和 pathspec 会枚举工作区路径——属于用户私有
	//（mode 在 Windows 上是空操作，那里的数据目录本就是每用户独立的）。
	await mkdir(scratchDir, { recursive: true, mode: 0o700 });
	const indexFile = join(scratchDir, "index");
	const pathspecFile = join(scratchDir, "pathspec");
	// 通过 NUL 分隔的 pathspec 文件传入路径，使庞大的
	// 未跟踪集合不会溢出命令行参数限制。
	await writeFile(pathspecFile, `${untrackedFiles.join("\0")}\0`);
	const addArgs = [
		"add",
		"--force",
		"--pathspec-from-file",
		pathspecFile,
		"--pathspec-file-nul",
	];
	try {
		await runGitWithIndex(cwd, indexFile, addArgs);
	} catch (error) {
		// 已列出的文件在 add 之前消失是每轮的竞争，而非索引
		// 损坏——保留缓存，让调用方仅降级本轮。
		const stderr = String((error as { stderr?: unknown }).stderr ?? "");
		if (stderr.includes("did not match any files")) {
			throw error;
		}
		// 损坏的索引或过期的 index.lock（写入中途崩溃、git 被 SIGKILL）
		// 否则会让从这里开始的每一轮都失败；清除两者并
		// 重建一次。重试需要付出完整重新哈希的代价，因此其他错误会向上传播。
		await rm(indexFile, { force: true });
		await rm(`${indexFile}.lock`, { force: true });
		await runGitWithIndex(cwd, indexFile, addArgs);
	}
	// 丢弃已脱离未跟踪集合的索引条目（文件被删除或
	// 变为已跟踪），使其不会幽灵般出现在快照树中——带 pathspec 的
	// `git add` 从不移除条目。比较时规范化尾部斜杠：
	// `ls-files --others` 将未跟踪的嵌套仓库报告为
	// "sub/"，但索引将其 gitlink 条目记录为 "sub"——没有
	// 规范化，gitlink 会在被添加的同一轮就被清除。
	const indexedListing = await execFile(
		"git",
		["-C", cwd, ...SCRATCH_INDEX_GIT_CONFIG, "ls-files", "-z"],
		{
			windowsHide: true,
			maxBuffer: LS_FILES_MAX_BUFFER,
			env: { ...process.env, GIT_INDEX_FILE: indexFile },
		},
	);
	const untrackedSet = new Set(
		untrackedFiles.map((path) =>
			path.endsWith("/") ? path.slice(0, -1) : path,
		),
	);
	const stale = indexedListing.stdout
		.split("\0")
		.filter(Boolean)
		.filter((path) => !untrackedSet.has(path));
	if (stale.length > 0) {
		// 路径经 stdin 传递（`-z --stdin`）：无论多少条目
		// 过期都只用一个 git 进程，也没有命令行长度限制。
		await runGitWithIndexStdin(
			cwd,
			indexFile,
			["update-index", "-z", "--force-remove", "--stdin"],
			`${stale.join("\0")}\0`,
		);
	}
	const tree = await runGitWithIndex(cwd, indexFile, ["write-tree"]);
	if (!tree) {
		return undefined;
	}
	const commit = await runGitWithIndex(cwd, indexFile, [
		"commit-tree",
		tree,
		"-m",
		"untracked files on cline checkpoint",
	]);
	return commit || undefined;
}

/**
 * 创建工作树的 stash 兼容快照提交，与 `git stash create` 不同，
 * 它还将未跟踪文件捕获为第三个父提交。检查点恢复路径
 * 会为携带此第三父提交的快照回卷未跟踪文件，
 * 因此 agent 创建的任何文件（或它修改的未跟踪文件）
 * 都能恢复到其检查点时刻的状态。无可快照内容时
 *（干净工作树且无未跟踪文件）返回 `undefined`，
 * 让调用方回退到 HEAD 提交检查点。
 */
async function createWorktreeStashCommit(
	cwd: string,
	scratchDir: string,
	message: string,
): Promise<string | undefined> {
	const stashRef = (await runGit(cwd, ["stash", "create", message])).stdout;
	const untrackedParent = await createUntrackedParentCommit(cwd, scratchDir);

	if (stashRef) {
		if (!untrackedParent) {
			// 仅跟踪的变更——普通 stash 已捕获它们。
			return stashRef;
		}
		const tree = (await runGit(cwd, ["rev-parse", `${stashRef}^{tree}`]))
			.stdout;
		const base = (await runGit(cwd, ["rev-parse", `${stashRef}^1`])).stdout;
		const indexParent = (await runGit(cwd, ["rev-parse", `${stashRef}^2`]))
			.stdout;
		if (!tree || !base || !indexParent) {
			return stashRef;
		}
		return (
			(
				await runGit(cwd, [
					"commit-tree",
					tree,
					"-p",
					base,
					"-p",
					indexParent,
					"-p",
					untrackedParent,
					"-m",
					message,
				])
			).stdout || stashRef
		);
	}

	// 已跟踪工作树干净。仅当存在需要保留的未跟踪文件时
	// 才合成 stash；否则调用方使用 HEAD 检查点。
	if (!untrackedParent) {
		return undefined;
	}
	const head = (await runGit(cwd, ["rev-parse", "HEAD"])).stdout;
	const headTree = (await runGit(cwd, ["rev-parse", "HEAD^{tree}"])).stdout;
	if (!head || !headTree) {
		return undefined;
	}
	// 索引父提交镜像（未变更的）HEAD 树，使合成的
	// 提交具有 `git stash apply` 期望的双/三父提交形态。
	const indexParent = (
		await runGit(cwd, [
			"commit-tree",
			headTree,
			"-p",
			head,
			"-m",
			"index on cline checkpoint",
		])
	).stdout;
	if (!indexParent) {
		return undefined;
	}
	return (
		(
			await runGit(cwd, [
				"commit-tree",
				headTree,
				"-p",
				head,
				"-p",
				indexParent,
				"-p",
				untrackedParent,
				"-m",
				message,
			])
		).stdout || undefined
	);
}

/**
 * 删除 refs/cline/checkpoints/{sessionId}/ 下所有由检查点
 * 系统创建以保持 stash 对象可达的私有 git ref。
 * 错误会被吞掉 - 如果 cwd 不是 git 仓库或这些 ref 不存在，
 * 删除是空操作。
 */
export async function deleteCheckpointRefs(
	cwd: string | null | undefined,
	sessionId: string,
): Promise<void> {
	if (!cwd) return;
	// scratch 目录（持久化未跟踪索引）以 cwd + 会话 id 为键。
	// 删除时 cwd 未知的会话改由基于年龄的
	// 清理器覆盖。
	await rm(checkpointScratchDir(cwd, sessionId), {
		recursive: true,
		force: true,
	}).catch(() => undefined);
	const prefix = `refs/cline/checkpoints/${sessionId}/`;
	try {
		const { stdout } = await runGit(cwd, [
			"for-each-ref",
			"--format=%(refname)",
			prefix,
		]);
		const refs = stdout.trim().split("\n").filter(Boolean);
		await Promise.allSettled(
			refs.map((ref) => runGit(cwd, ["update-ref", "-d", ref])),
		);
	} catch {
		// 不是 git 仓库或 git 不可用 - 忽略。
	}
}

export async function retainCheckpointRefs(
	cwd: string | null | undefined,
	sessionId: string,
	checkpoints: readonly CheckpointEntry[],
): Promise<void> {
	if (!cwd || checkpoints.length === 0) return;
	await Promise.allSettled(
		checkpoints.map((entry) =>
			runGit(cwd, [
				"update-ref",
				`refs/cline/checkpoints/${sessionId}/${entry.runCount}`,
				entry.ref,
			]),
		),
	);
}

function upsertCheckpointHistory(
	history: readonly CheckpointEntry[],
	entry: CheckpointEntry,
): CheckpointEntry[] {
	const existingIndex = history.findIndex(
		(candidate) => candidate.runCount === entry.runCount,
	);
	if (existingIndex < 0) {
		return [...history, entry];
	}
	return history.map((candidate, index) =>
		index === existingIndex ? entry : candidate,
	);
}

export function createCheckpointHooks(
	options: CreateCheckpointHooksOptions,
): AgentHooks {
	// 清理从未被显式删除的会话留下的 scratch 目录。
	// 触发即忘：清理失败绝不影响会话。
	void pruneStaleScratchDirs();
	let repoSupported: boolean | undefined;
	// 当前轮次开始时存在的消息数量，在 beforeRun 中捕获。
	// 对于在 beforeRun *之后*追加本轮提示词的宿主（例如
	// `AgentRuntime.run(input)`），自该索引以来的增量包含新用户
	// 消息，是可靠的“新用户轮次”信号。它只是提示，不是
	// 唯一门槛：在轮次*之前*就植入提示词的宿主（SessionRuntime
	// 调用 `run("")` 且提示词已在 initialMessages 中）会让
	// 增量为空，而进程重启后的新 hook 实例会将其重置
	// 为 undefined —— 因此持久化的检查点历史才是
	// 真正防止重复/覆盖检查点的机制。
	let rootRunMessageStart: number | undefined;

	const ensureGitRepository = async (): Promise<boolean> => {
		// 只缓存肯定答案：不是 git 仓库的 cwd 可能在
		// 会话中途变成仓库（用户运行 `git init`），而此探测
		// 每个用户轮次最多触发一次，因此重新检查很便宜。一旦
		// 检测到仓库，检查点就从那一轮开始生效。
		if (repoSupported === true) {
			return true;
		}
		try {
			const result = await runGit(options.cwd, [
				"rev-parse",
				"--is-inside-work-tree",
			]);
			repoSupported = result.stdout === "true";
		} catch {
			repoSupported = false;
		}
		return repoSupported;
	};

	const createCheckpoint = async (
		runCount: number,
	): Promise<CheckpointEntry | undefined> => {
		if (options.createCheckpoint) {
			return await options.createCheckpoint({
				cwd: options.cwd,
				sessionId: options.sessionId,
				runCount,
			});
		}

		if (!(await ensureGitRepository())) {
			return undefined;
		}

		const startedAt = Date.now();
		// 每次内置快照尝试一个事件。Outcomes："stash"（完整
		// 快照）、"head_clean"（干净工作树，HEAD 条目是正常
		// 结果）、"head_fallback"（失败后降级到 HEAD）、以及
		// "skipped"（本轮未写入检查点）。仅有耗时 —— 绝不
		// 携带文件路径或内容。
		const captureSnapshot = (
			outcome: "stash" | "head_clean" | "head_fallback" | "skipped",
		): void => {
			options.telemetry?.capture({
				event: "checkpoint.snapshot",
				properties: {
					sessionId: options.sessionId,
					runCount,
					outcome,
					durationMs: Date.now() - startedAt,
				},
			});
		};

		const createHeadCheckpoint = async (
			warnPrefix: string,
		): Promise<CheckpointEntry | undefined> => {
			try {
				const result = await runGit(options.cwd, ["rev-parse", "HEAD"]);
				const ref = result.stdout.trim();
				if (!ref) {
					return undefined;
				}
				return {
					ref,
					createdAt: Date.now(),
					runCount,
					kind: "commit",
				};
			} catch (error) {
				warn(
					options.logger,
					`${warnPrefix}: ${error instanceof Error ? error.message : String(error)}`,
				);
				return undefined;
			}
		};

		const message = `${CHECKPOINT_STASH_MESSAGE_PREFIX}${options.sessionId} run=${runCount}`;
		let ref = "";
		try {
			ref =
				(await createWorktreeStashCommit(
					options.cwd,
					checkpointScratchDir(options.cwd, options.sessionId),
					message,
				)) ?? "";
		} catch (error) {
			warn(
				options.logger,
				`Checkpoint snapshot failed after ${Date.now() - startedAt}ms: ${error instanceof Error ? error.message : String(error)}`,
			);
			const fallback = await createHeadCheckpoint(
				"Checkpoint HEAD fallback failed",
			);
			captureSnapshot(fallback ? "head_fallback" : "skipped");
			return fallback;
		}
		if (!ref) {
			// 无未跟踪文件的干净工作树 —— HEAD 条目是这里
			// 的预期结果，而非降级。
			const fallback = await createHeadCheckpoint(
				"Checkpoint HEAD fallback failed",
			);
			captureSnapshot(fallback ? "head_clean" : "skipped");
			return fallback;
		}

		// 将 stash 提交存储在私有 ref 命名空间下，使其对
		// 用户正常的 `git stash list` 工作流不可见。
		// `refs/stash` 才是填充该列表的 ref；写入任何其他
		// ref 路径都能保持对象可达（GC 安全）而不暴露给
		// 用户。原始 SHA 在恢复路径上已可直接用于 `git stash apply`，
		// 因此无需恢复侧的改动。
		const privateRef = `refs/cline/checkpoints/${options.sessionId}/${runCount}`;
		try {
			await runGit(options.cwd, ["update-ref", privateRef, ref]);
		} catch (error) {
			warn(
				options.logger,
				`Checkpoint store failed: ${error instanceof Error ? error.message : String(error)}`,
			);
			captureSnapshot("skipped");
			return undefined;
		}

		captureSnapshot("stash");
		return {
			ref,
			createdAt: Date.now(),
			runCount,
			kind: "stash",
		};
	};

	return {
		beforeRun: async ({ snapshot }) => {
			if (snapshot.parentAgentId == null) {
				rootRunMessageStart = snapshot.messages.length;
			}
			return undefined;
		},
		beforeModel: async ({ snapshot }) => {
			if (snapshot.parentAgentId != null || snapshot.iteration !== 1) {
				return undefined;
			}
			// 运行时在 beforeRun 之后追加的消息。当宿主将
			// 提示词作为运行输入传入时，此增量包含新用户轮次；当
			// 宿主提前植入它（或 hook 是重启后的新实例）时
			// 增量为空，因此它只是一个正向提示。
			const currentRunMessages = snapshot.messages.slice(
				rootRunMessageStart ?? Math.max(0, snapshot.messages.length - 1),
			);
			rootRunMessageStart = undefined;
			// 跨度感知计数，使编号在压缩后仍然有效（压缩会把
			// 多个用户轮次折叠为一条携带 userRunSpan 的摘要消息）。
			const runCount = countUserRunMessages(snapshot.messages);
			if (runCount < 1) {
				return undefined;
			}
			const metadata = await options.readSessionMetadata();
			const existing = readCheckpointMetadata(metadata);
			// 当一次运行引入新用户轮次时，它值得一个检查点。
			// `introducedUserRun` 捕获运行输入路径（并在
			// 编辑并重新生成时刷新条目）；`alreadyCheckpointed` 读取自
			// 持久会话历史，捕获植入提示词路径，且不同于
			// 任何内存计数器，在进程重启后仍然成立。仅当两者
			// 均不适用时才跳过：延续/恢复重跑一个
			// 已被检查点的运行（它绝不能以已变动的工作区
			// 覆盖运行前快照）。
			const introducedUserRun = countUserRunMessages(currentRunMessages) >= 1;
			const alreadyCheckpointed =
				existing?.history.some((entry) => entry.runCount === runCount) ?? false;
			if (!introducedUserRun && alreadyCheckpointed) {
				return undefined;
			}
			const entry = await createCheckpoint(runCount);
			if (!entry) {
				return undefined;
			}
			if (existing?.latest.ref === entry.ref) {
				return undefined;
			}
			const history = upsertCheckpointHistory(existing?.history ?? [], entry);
			await options.writeSessionMetadata({
				...(metadata ?? {}),
				checkpoint: {
					latest: entry,
					history,
				} satisfies CheckpointMetadata,
			});
			return undefined;
		},
	};
}
