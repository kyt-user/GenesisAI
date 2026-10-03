import { spawn } from "node:child_process";
import {
	closeSync,
	mkdirSync,
	openSync,
	readFileSync,
	unlinkSync,
} from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
	CLINE_RUN_AS_HUB_DAEMON_ENV,
	isBunEmbeddedModulePath,
	isHubDaemonProcess,
	resolveClineBuildEnv,
	withResolvedClineBuildEnv,
} from "@cline/shared";
import {
	queryHubSessionActivity,
	rememberRecoverableLocalHubUrl,
	requestHubDrain,
	requestHubShutdown,
	verifyHubConnection,
} from "../client";
import {
	clearHubDiscovery,
	compareHubBuilds,
	createHubServerUrl,
	getManagedHubCompatibility,
	type HubOwnerContext,
	type HubServerDiscoveryRecord,
	type HubServerProbeRecord,
	isManagedHubReusable,
	probeHubServer,
	readHubDiscovery,
	resolveClineDataDir,
	resolveHubBuildIdentity,
	withHubStartupLock,
	writeHubDiscovery,
} from "../discovery";
import {
	type HubEndpointOverrides,
	resolveHubEndpointOptions,
} from "../discovery/defaults";
import {
	resolveProductionHubOwnerContext,
	resolveSharedHubOwnerContext,
} from "../discovery/workspace";

export interface DetachedHubOptions extends HubEndpointOverrides {
	allowPortFallback?: boolean;
	/** 对仅会话的 Hub 禁用账户级连接器监管。默认为 true。 */
	manageConnectors?: boolean;
}

/**
 * 新生成的 Hub 获得多长时间来发布可用的发现记录。
 * Hub 所运行的编译二进制的冷启动（安装或更新后的
 * 首次启动、同时杀毒软件正在扫描它）在
 * Windows 上通常需要 8-13 秒。旧的 8 秒限制恰好在那些 Hub 起来之前放弃，
 * 导致启动失败，尽管下一次尝试就能顺利附加。15 秒覆盖了大多数
 * 冷启动，并且仍能放进桌面 shell 的 30 秒端点等待
 * 与登录 shell PATH 解析并行的时间窗内。
 */
const HUB_STARTUP_TIMEOUT_MS = 15_000;
const HUB_STARTUP_POLL_MS = 200;
const HUB_RETIRE_TIMEOUT_MS = 3_000;
const HUB_RETIRE_POLL_MS = 100;
const HUB_SPAWN_RETRY_DELAYS_MS = [100, 250, 500, 1_000, 2_000];
const COMPILED_BUN_HUB_DAEMON_ARG = "--cline-hub-daemon";
const HUB_RETIRE_ATTEMPT_LIMIT = 3;
const HUB_RETIRE_ATTEMPT_WINDOW_MS = 60_000;

const retireAttemptsByUrl = new Map<
	string,
	{ count: number; windowStartedAt: number }
>();

export const __test__ = {
	/** retire 尝试是按 URL 索引的模块级状态；用例之间清空。 */
	resetRetireAttempts(): void {
		retireAttemptsByUrl.clear();
	},
	resolveDaemonEntryArgs,
};

/**
 * 对同一 Hub URL 重复 retire 的熔断器。
 *
 * 构建排序已保证一对安装中只有一侧可以决定
 * retire，因此健康的安装对给定 URL 只 retire 一次。反复
 * retire 同一 URL 意味着上游出了问题，而其失败模式
 * 很严重：长期存活的客户端（sidecar、交互式 CLI 会话）在紧凑循环中
 * 相互拆除对方的 daemon，每个会话都以异常的
 * socket 关闭告终。几次尝试后退避，使未来的排序缺陷
 * 只会导致过时构建提示，而不是不可用的 Hub。
 */
function shouldAttemptRetire(url: string, now = Date.now()): boolean {
	const entry = retireAttemptsByUrl.get(url);
	if (!entry || now - entry.windowStartedAt > HUB_RETIRE_ATTEMPT_WINDOW_MS) {
		retireAttemptsByUrl.set(url, { count: 1, windowStartedAt: now });
		return true;
	}
	entry.count += 1;
	return entry.count <= HUB_RETIRE_ATTEMPT_LIMIT;
}

function endpointArgs(endpoint: HubEndpointOverrides): string[] {
	return [
		...(endpoint.host ? ["--host", endpoint.host] : []),
		...(typeof endpoint.port === "number"
			? ["--port", String(endpoint.port)]
			: []),
		...(endpoint.pathname ? ["--pathname", endpoint.pathname] : []),
	];
}

function openDetachedHubLogFile(): { fd: number; logPath: string } | undefined {
	try {
		const logPath = join(resolveClineDataDir(), "logs", "hub-daemon.log");
		mkdirSync(dirname(logPath), { recursive: true });
		return { fd: openSync(logPath, "a"), logPath };
	} catch {
		return undefined;
	}
}

function resolveDefaultHubOwnerContext() {
	return resolveClineBuildEnv() === "production"
		? resolveProductionHubOwnerContext()
		: resolveSharedHubOwnerContext();
}

function isReusableHubRecord(record: HubServerProbeRecord): boolean {
	return isManagedHubReusable(record);
}

/**
 * 读取 npm postinstall 搁置一旁的发现记录（参见
 * apps/cli/script/postinstall.mjs）。有意绕过 readHubDiscovery——
 * 该文件是尽力而为的恢复元数据，不是活动记录——并且保持
 * 同步，因此不会给 ensure 流程增加异步边界。
 *
 * 为 `cline doctor` 导出，它绝不能仅因记录被搁置一旁，
 * 就把受保护的活 hub 误认为过期的 daemon。
 */
export function readSupersededHubDiscovery(
	discoveryPath: string,
): { url?: string; authToken?: string; pid?: number } | undefined {
	try {
		const raw = JSON.parse(
			readFileSync(`${discoveryPath}.superseded`, "utf8"),
		) as { url?: unknown; authToken?: unknown; pid?: unknown };
		return {
			url: typeof raw.url === "string" ? raw.url : undefined,
			authToken: typeof raw.authToken === "string" ? raw.authToken : undefined,
			pid: typeof raw.pid === "number" ? raw.pid : undefined,
		};
	} catch {
		return undefined;
	}
}

/**
 * 搁置的记录是一次性恢复元数据：一旦 ensure 以
 * 活且已验证的 hub 完成，它就完成了使命，继续保留
 * 它反而危险——其 pid 可能被操作系统回收，远期的
 * 某次启动若找不到活动记录，就会用它 SIGTERM 一个无关进程。
 */
function discardSupersededHubDiscovery(discoveryPath: string): void {
	try {
		unlinkSync(`${discoveryPath}.superseded`);
	} catch {
		// 已不存在或不可读——无需丢弃。
	}
}

function withMatchingDiscoveryRetirementMetadata(
	probe: HubServerProbeRecord,
	discovered: { url?: string; authToken?: string; pid?: number } | undefined,
	expectedUrl: string,
): HubServerProbeRecord {
	if (!discovered || discovered.url !== expectedUrl) {
		return probe;
	}
	return {
		...probe,
		authToken: probe.authToken ?? discovered.authToken,
		pid: probe.pid ?? discovered.pid,
	};
}

async function safeProbeHubServer(
	url: string,
	authToken?: string,
): Promise<HubServerProbeRecord | undefined> {
	try {
		return await probeHubServer(url, { authToken });
	} catch {
		return undefined;
	}
}

async function waitForHubToRetire(
	url: string,
	timeoutMs: number,
): Promise<boolean> {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		const healthy = await safeProbeHubServer(url);
		if (!healthy?.url) {
			return true;
		}
		await new Promise((resolve) => setTimeout(resolve, HUB_RETIRE_POLL_MS));
	}
	return false;
}

/**
 * 优雅地 retire 一个已发现的 hub。被每条替换路径
 * （detached ensure、in-process ensure）共享，使 retire 的语义始终一致：
 * 先 drain，再执行已认证的 shutdown，SIGTERM 仅作为
 * 最后手段，且仅当 hub 确实消失后才清除发现记录。
 */
export async function retireDiscoveredHub(
	record: { url: string; authToken?: string; pid?: number },
	discoveryPath: string,
): Promise<boolean> {
	if (!shouldAttemptRetire(record.url)) {
		return false;
	}
	// 优雅交接，按强制程度递增：drain（拒绝新
	// 工作），然后已认证的 shutdown，最后 SIGTERM 仅作为回退
	// 且只针对此刻能确证存活的 pid——记录中的
	// pid 可能已被操作系统回收给无关进程。
	await requestHubDrain(
		record.url,
		record.authToken,
		"retired by newer install",
	).catch(() => false);
	await requestHubShutdown(record.url, record.authToken).catch(() => false);
	let retired = await waitForHubToRetire(record.url, HUB_RETIRE_TIMEOUT_MS);
	if (!retired && record.pid && isPidAlive(record.pid)) {
		try {
			process.kill(record.pid, "SIGTERM");
		} catch {
			// 仅尽力而为的清理。兼容的 hub 仍可能启动在回退端口上。
		}
		retired = await waitForHubToRetire(record.url, HUB_RETIRE_TIMEOUT_MS);
	}
	// 只有成功的 retire 才可以清除发现记录：清除幸存 hub
	// 的记录会让活 daemon 无法被发现，只能通过预期 URL 的
	// 探测/修复路径恢复。
	if (retired) {
		await clearHubDiscovery(discoveryPath).catch(() => undefined);
	}
	return retired;
}

function isPidAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as { code?: string })?.code === "EPERM";
	}
}

export type HubRetirementOutcome =
	| "reusable"
	| "retired"
	| "deferred_busy"
	| "failed";

/**
 * Hub 当前是否正在服务会话，因此不得在它们之下关闭。
 *
 * 故障开放（将无法应答的 Hub 视为空闲）为卡死或太旧而无法应答
 * 查询的 Hub 保留了既有的替换路径；只有明确报告存在活会话的
 * Hub 才会被免于替换。
 */
export async function hubHasLiveSessions(
	record: Pick<HubServerProbeRecord, "url" | "authToken">,
): Promise<boolean> {
	try {
		const activity = await queryHubSessionActivity(
			record.url,
			record.authToken,
		);
		return activity.activeSessionCount > 0;
	} catch {
		return false;
	}
}

/**
 * retire 一个 Hub 会杀死其已建立的 WebSocket，因此运行在其上的会话
 * 会在轮次中途以异常关闭告终。当它繁忙时改为延迟：调用方
 * 附加到较老的 Hub，构建不匹配观察器告知用户新构建
 * 正在等待，交换发生在他们选择的边界处。
 *
 * drain 在繁忙检查之前，而不是之后：空闲读数只是一个快照，
 * 在它和 shutdown 之间被接纳的会话会在本应被“空闲”
 * 排除的 retire 中死亡。先接受 drain 后，hub 不再接纳新工作，
 * 因此读数在 retire 全程保持为真。不接受 drain 的 hub
 *（早于 /drain 的构建应答 404；卡死的可能完全不应答）
 * 保留历史的尽力而为快照——永不替换这样的 hub 会让其上每个
 * 客户端永久搁浅，而这正是此路径存在所要修复的故障。
 */
async function retireIncompatibleHub(
	record: HubServerProbeRecord,
	discoveryPath: string,
): Promise<HubRetirementOutcome> {
	if (isReusableHubRecord(record)) {
		return "reusable";
	}
	const drained = await requestHubDrain(
		record.url,
		record.authToken,
		"retired by newer install",
	).catch(() => false);
	if (await hubHasLiveSessions(record)) {
		// 延迟意味着 hub 继续服务其会话，因此把它交回：
		// 被延迟却留在 draining 状态的 hub 会拒绝所有新工作直到重启。
		if (drained) {
			await requestHubDrain(
				record.url,
				record.authToken,
				"hub retirement deferred",
				{ off: true },
			).catch(() => false);
		}
		return "deferred_busy";
	}
	const retired = await retireDiscoveredHub(record, discoveryPath);
	// 熬过整套梯级（或被 retire 熔断器跳过）的 hub 仍在运行，
	// 也把它交回——已 drain 但存活是一种 limbo，
	// 会拒绝所有新工作直到有东西重启它。
	if (!retired && drained) {
		await requestHubDrain(
			record.url,
			record.authToken,
			"hub retirement failed",
			{ off: true },
		).catch(() => false);
	}
	return retired ? "retired" : "failed";
}

/**
 * 预 singleton 的生产构建在共享 owner 发现路径下跟踪本地 hub，
 * 并在随机回退端口上生成 daemon。那些 daemon 对生产 owner 上下文
 * 不可见，因此不会有任何东西复用或停止它们。retire 记录中的
 * 遗留 hub（其记录携带优雅停止所需的 auth token 和 pid）
 * 并清除遗留记录，使升级不会留下运行过时代码的孤儿 daemon。
 */
async function retireLegacySharedHub(owner: HubOwnerContext): Promise<void> {
	if (resolveClineBuildEnv() !== "production") {
		return;
	}
	const legacy = resolveSharedHubOwnerContext();
	if (legacy.discoveryPath === owner.discoveryPath) {
		return;
	}
	const record = await readHubDiscovery(legacy.discoveryPath);
	if (record?.url) {
		await retireDiscoveredHub(record, legacy.discoveryPath);
	} else {
		await clearHubDiscovery(legacy.discoveryPath).catch(() => undefined);
	}
}

function resolveDaemonEntryPath(): string {
	const extension = import.meta.url.endsWith(".ts") ? "ts" : "js";
	return fileURLToPath(new URL(`./entry.${extension}`, import.meta.url));
}

/**
 * 编译后的 Bun 二进制将捆绑模块挂载在虚拟文件系统上，
 * 无法作为脚本参数交给子进程，因此子进程启动其内嵌的
 * 入口点，并根据标记旗标切换身份。
 */
function resolveDaemonEntryArgs(
	daemonEntryPath: string,
	isBunRuntime: boolean,
): string[] {
	if (isBunEmbeddedModulePath(daemonEntryPath)) {
		return [COMPILED_BUN_HUB_DAEMON_ARG];
	}
	const useDevelopmentConditions =
		isBunRuntime && daemonEntryPath.toLowerCase().endsWith(".ts");
	return [
		...(useDevelopmentConditions ? ["--conditions=development"] : []),
		daemonEntryPath,
	];
}

function resolveLaunchCommand(
	workspaceRoot: string,
	endpoint: DetachedHubOptions,
): {
	launcher: string;
	args: string[];
	cwd: string;
	env: NodeJS.ProcessEnv;
} {
	const daemonEntryPath = resolveDaemonEntryPath();
	const execPath = process.execPath?.trim();
	if (!execPath) {
		throw new Error("unable to resolve runtime executable for hub daemon");
	}
	const isBunRuntime = basename(execPath).toLowerCase().includes("bun");
	const entryArgs = resolveDaemonEntryArgs(daemonEntryPath, isBunRuntime);
	return {
		launcher: execPath,
		args: [
			...entryArgs,
			"--cwd",
			workspaceRoot,
			...endpointArgs(endpoint),
			...(endpoint.manageConnectors === false ? ["--no-connectors"] : []),
		],
		cwd: workspaceRoot,
		env: {
			...withResolvedClineBuildEnv(process.env),
			CLINE_NO_INTERACTIVE: "1",
			[CLINE_RUN_AS_HUB_DAEMON_ENV]: "1",
		},
	};
}

function isTextFileBusyError(error: unknown): boolean {
	if (!error || typeof error !== "object") {
		return false;
	}
	const code = "code" in error ? error.code : undefined;
	if (code === "ETXTBSY") {
		return true;
	}
	const message = "message" in error ? error.message : undefined;
	return typeof message === "string" && message.includes("ETXTBSY");
}

export function spawnDetachedHubServer(
	workspaceRoot: string,
	endpoint: DetachedHubOptions = {},
): void {
	if (isHubDaemonProcess()) {
		return;
	}
	const command = resolveLaunchCommand(workspaceRoot, endpoint);
	const logFile = openDetachedHubLogFile();
	try {
		const child = spawn(command.launcher, command.args, {
			detached: true,
			stdio: logFile ? ["ignore", logFile.fd, logFile.fd] : "ignore",
			env: command.env,
			cwd: command.cwd,
			// 防止 Windows 上出现控制台窗口；detached
			// 进程否则会分配新的可见控制台。
			windowsHide: true,
		});
		child.unref();
	} finally {
		if (logFile) {
			closeSync(logFile.fd);
		}
	}
}

export async function spawnDetachedHubServerWithRetry(
	workspaceRoot: string,
	endpoint: DetachedHubOptions = {},
): Promise<void> {
	for (let attempt = 0; ; attempt++) {
		try {
			spawnDetachedHubServer(workspaceRoot, endpoint);
			return;
		} catch (error) {
			const delay = HUB_SPAWN_RETRY_DELAYS_MS[attempt];
			if (!isTextFileBusyError(error) || delay === undefined) {
				throw error;
			}
			await new Promise((resolve) => setTimeout(resolve, delay));
		}
	}
}

export function prewarmDetachedHubServer(
	workspaceRoot: string,
	endpoint: DetachedHubOptions = {},
): void {
	if (isHubDaemonProcess()) {
		return;
	}
	void ensureDetachedHubServer(workspaceRoot, endpoint).catch(() => {
		// 仅尽力而为的预热
	});
}

export interface DetachedHubResolution {
	url: string;
	authToken: string;
}

async function ensureDetachedHubServerLocked(
	owner: HubOwnerContext,
	workspaceRoot: string,
	endpointOverrides: DetachedHubOptions = {},
): Promise<DetachedHubResolution> {
	const hasExplicitEndpoint =
		endpointOverrides.host !== undefined ||
		endpointOverrides.port !== undefined ||
		endpointOverrides.pathname !== undefined ||
		!!process.env.CLINE_HUB_PORT?.trim();
	const endpoint = resolveHubEndpointOptions(endpointOverrides);
	const expectedUrl = createHubServerUrl(
		endpoint.host,
		endpoint.port,
		endpoint.pathname,
	);
	const rememberIfManaged = (
		result: DetachedHubResolution,
	): DetachedHubResolution => {
		if (!hasExplicitEndpoint) {
			rememberRecoverableLocalHubUrl(result.url, result.authToken);
		}
		return result;
	};
	await retireLegacySharedHub(owner).catch(() => undefined);
	const discovered = await readHubDiscovery(owner.discoveryPath);
	// npm 包的 postinstall 将发现记录搁置一旁（相同的
	// ".superseded" 后缀），使 3.0.55 之前的更新器无法重启繁忙的 hub。
	// 没有它，被那样顶替掉的 hub 将永远无法在这里 retire：
	// 仅靠端口探测不携带 auth token 或 pid。
	const superseded = discovered?.url
		? undefined
		: readSupersededHubDiscovery(owner.discoveryPath);
	let retiredUnusableDiscovery = false;
	if (discovered?.url) {
		const discoveredAuthToken = discovered.authToken;
		if (!discoveredAuthToken) {
			retiredUnusableDiscovery = true;
			await retireDiscoveredHub(discovered, owner.discoveryPath);
		} else {
			const healthy = await safeProbeHubServer(
				discovered.url,
				discoveredAuthToken,
			);
			if (
				healthy?.url &&
				isReusableHubRecord(healthy) &&
				(await verifyHubConnection(healthy.url, {
					authToken: discoveredAuthToken,
				}))
			) {
				discardSupersededHubDiscovery(owner.discoveryPath);
				return rememberIfManaged({
					url: healthy.url,
					authToken: discoveredAuthToken,
				});
			}
			if (healthy?.url) {
				const outcome = await retireIncompatibleHub(
					{ ...healthy, authToken: discoveredAuthToken },
					owner.discoveryPath,
				);
				// 繁忙的老 Hub 保持运行，因此附加到它，而不是
				// 生成第二个与它争夺端口的 daemon。
				if (
					outcome === "deferred_busy" &&
					(await verifyHubConnection(healthy.url, {
						authToken: discoveredAuthToken,
					}))
				) {
					return rememberIfManaged({
						url: healthy.url,
						authToken: discoveredAuthToken,
					});
				}
			} else {
				await clearHubDiscovery(owner.discoveryPath).catch(() => undefined);
			}
		}
	}
	const expected = await safeProbeHubServer(expectedUrl);
	if (expected?.url) {
		const expectedForRetirement = withMatchingDiscoveryRetirementMetadata(
			expected,
			discovered ?? superseded,
			expectedUrl,
		);
		if (isReusableHubRecord(expected)) {
			// 活 hub 健康但发现记录缺失/不可读（或 auth token
			// 为空）。优先通过任何已知的 auth token 附加，
			// 而不是生成第二个以 EADDRINUSE 死掉的 daemon。
			const candidateTokens = [
				expected.authToken,
				discovered?.authToken,
				superseded?.authToken,
			].filter(
				(token): token is string =>
					typeof token === "string" && token.trim().length > 0,
			);
			for (const token of candidateTokens) {
				if (
					!(await verifyHubConnection(expected.url, {
						authToken: token,
					}))
				) {
					continue;
				}
				const repaired: HubServerDiscoveryRecord = {
					hubId: expected.hubId ?? `repaired-${expected.port}`,
					protocolVersion: expected.protocolVersion,
					minClientProtocolVersion: expected.minClientProtocolVersion,
					maxClientProtocolVersion: expected.maxClientProtocolVersion,
					capabilities: expected.capabilities,
					coreVersion: expected.coreVersion,
					buildId: expected.buildId,
					authToken: token,
					host: expected.host,
					port: expected.port,
					url: expected.url,
					pid: expected.pid ?? discovered?.pid ?? superseded?.pid,
					startedAt: expected.startedAt ?? new Date().toISOString(),
					updatedAt: new Date().toISOString(),
				};
				try {
					await writeHubDiscovery(owner.discoveryPath, repaired);
				} catch {
					// 尽力而为的修复；即使发现文件不可写，
					// 刚验证过的 token 仍可用于附加。
				}
				discardSupersededHubDiscovery(owner.discoveryPath);
				return rememberIfManaged({
					url: expected.url,
					authToken: token,
				});
			}
			const upgradeHint = retiredUnusableDiscovery
				? " This can happen immediately after upgrading from a build that wrote an empty hub auth token; run 'cline doctor fix' to stop the old daemon and repair local hub discovery."
				: "";
			throw new Error(
				`A compatible Cline Hub is already running at ${expectedUrl}, but its discovery record is missing or unreadable and no usable auth token is available. Run 'cline doctor fix' to repair local hub discovery.${upgradeHint}`,
			);
		}
		const expectedOutcome = await retireIncompatibleHub(
			expectedForRetirement,
			owner.discoveryPath,
		);
		if (expectedOutcome === "deferred_busy") {
			// 同上：老 Hub 仍在服务会话，用任何能验证的
			// token 附加，而不是替换它。
			for (const token of [
				expectedForRetirement.authToken,
				discovered?.authToken,
			].filter(
				(candidate): candidate is string =>
					typeof candidate === "string" && candidate.trim().length > 0,
			)) {
				if (await verifyHubConnection(expected.url, { authToken: token })) {
					return rememberIfManaged({ url: expected.url, authToken: token });
				}
			}
			if (endpointOverrides.allowPortFallback !== true && endpoint.port !== 0) {
				throw new Error(
					`An older Cline Hub is running at ${expectedUrl} and is still serving active sessions, so it was not replaced, but no usable auth token is available to attach to it. Finish those sessions, or run 'cline doctor fix' to stop the hub.`,
				);
			}
		}
		if (
			expectedOutcome === "failed" &&
			endpointOverrides.allowPortFallback !== true &&
			endpoint.port !== 0
		) {
			throw new Error(
				`An incompatible Cline Hub is already running at ${expectedUrl} and could not be retired automatically. Run 'cline doctor fix' to stop stale hub daemons before starting a new hub.`,
			);
		}
	}
	const shouldUseFallbackPort =
		endpointOverrides.allowPortFallback === true && endpoint.port !== 0;
	const spawnEndpoint = shouldUseFallbackPort
		? { ...endpoint, port: 0 }
		: endpoint;
	await spawnDetachedHubServerWithRetry(workspaceRoot, {
		...spawnEndpoint,
		manageConnectors: endpointOverrides.manageConnectors,
	});
	const deadline = Date.now() + HUB_STARTUP_TIMEOUT_MS;
	while (Date.now() < deadline) {
		const nextDiscovery = await readHubDiscovery(owner.discoveryPath);
		if (nextDiscovery?.url && nextDiscovery.authToken) {
			const healthy = await safeProbeHubServer(
				nextDiscovery.url,
				nextDiscovery.authToken,
			);
			if (
				healthy?.url &&
				isReusableHubRecord(healthy) &&
				(await verifyHubConnection(healthy.url, {
					authToken: nextDiscovery.authToken,
				}))
			) {
				discardSupersededHubDiscovery(owner.discoveryPath);
				return rememberIfManaged({
					url: healthy.url,
					authToken: nextDiscovery.authToken,
				});
			}
		}
		const nextExpected = await safeProbeHubServer(expectedUrl);
		if (nextExpected?.url && !isReusableHubRecord(nextExpected)) {
			const expectedForRetirement = withMatchingDiscoveryRetirementMetadata(
				nextExpected,
				nextDiscovery ?? superseded,
				expectedUrl,
			);
			const nextOutcome = await retireIncompatibleHub(
				expectedForRetirement,
				owner.discoveryPath,
			);
			if (
				nextOutcome === "deferred_busy" &&
				nextDiscovery?.authToken &&
				(await verifyHubConnection(nextExpected.url, {
					authToken: nextDiscovery.authToken,
				}))
			) {
				return rememberIfManaged({
					url: nextExpected.url,
					authToken: nextDiscovery.authToken,
				});
			}
			if (
				nextOutcome === "failed" &&
				endpointOverrides.allowPortFallback !== true &&
				endpoint.port !== 0
			) {
				throw new Error(
					`An incompatible Cline Hub is still running at ${expectedUrl} and could not be retired automatically. Run 'cline doctor fix' to stop stale hub daemons before starting a new hub.`,
				);
			}
		}
		await new Promise((resolve) => setTimeout(resolve, HUB_STARTUP_POLL_MS));
	}
	throw new Error(
		`Timed out after ${HUB_STARTUP_TIMEOUT_MS}ms waiting for detached hub startup.`,
	);
}

export async function ensureDetachedHubServer(
	workspaceRoot: string,
	endpointOverrides: DetachedHubOptions = {},
): Promise<DetachedHubResolution> {
	const owner = resolveDefaultHubOwnerContext();
	return await withHubStartupLock(owner.discoveryPath, async () =>
		ensureDetachedHubServerLocked(owner, workspaceRoot, endpointOverrides),
	);
}

const HUB_UPGRADE_DEFAULT_WAIT_MS = 5_000;
const HUB_UPGRADE_IDLE_POLL_MS = 500;

export interface UpgradeManagedHubOptions {
	workspaceRoot?: string;
	/**
	 * 排空后，等待 hub 的活会话完成多久，然后替换它
	 *（`force`）或放弃（`still_busy`）。
	 */
	waitForIdleMs?: number;
	/**
	 * 等待期结束后，即使会话仍在进行也替换 hub。
	 * 仅在用户同意的路径上设置：那些会话会在轮次中途死亡。
	 * 仅在 hub 已接受 drain 后才生效——拒绝 drain 的繁忙 hub
	 * 永不替换，因为 drain 正是保护等待窗口期间
	 * 开始的工作的机制。
	 */
	force?: boolean;
	/** 记录为 hub 的 drain 原因，在 `hub.status` 中可见。 */
	reason?: string;
}

export type UpgradeManagedHubOutcome =
	/** 运行中的老 hub 已 retire，当前构建的 hub 已运行。 */
	| "replaced"
	/** 未找到活 hub；已启动当前构建的 hub。 */
	| "started"
	/** 运行中的 hub 已匹配本构建；无事可做。 */
	| "already_current"
	/**
	 * 运行中的 hub 比本构建更新（或无法排序）。
	 * 替换它会把另一个安装的 hub 降级并重新打开
	 * 互相 retire 循环（#13145），因此拒绝；
	 * 修复方式是更新此客户端。
	 */
	| "hub_not_older"
	/** 未设置 `force` 且会话从未完成——或 hub 的
	 * 活动从未得到确认，此处视为繁忙；hub
	 * 的 drain 已被解除并保持运行。 */
	| "still_busy";

export interface UpgradeManagedHubResult {
	outcome: UpgradeManagedHubOutcome;
	url?: string;
	authToken?: string;
	/**
	 * 决策时在老 hub 上观察到的活会话：被中断（`replaced`）
	 * 或使其保持运行（`still_busy`）的会话。
	 * hub 从未应答活动查询时省略。
	 */
	activeSessionCount?: number;
}

/**
 * 在用户明确同意下，用运行本构建的 hub 替换托管的本地 hub。
 * 这是 `ensureDetachedHubServer` 中自动替换的有意对应物，
 * 后者在老 hub 正在服务会话时延迟：先 drain（hub 拒绝新工作，
 * 进行中的轮次获得 `waitForIdleMs` 来完成），然后是共享的
 * 优雅 retire 梯级，最后是新的 daemon。Drain-first 是保证而非礼节：
 * 此函数仅在接受的 drain 之下 retire hub，因为 drain 是准入屏障，
 * 使任何繁忙或空闲读数在 retire 全程保持为真。拒绝 drain 的 hub
 * 直接升级失败——`force` 不能覆盖这一点，空闲读数也不能，
 * 因为没有 drain 时它只是一个快照，新接纳的会话可能在 retire
 * 落地前使其失效。（这样的 hub 仍会在下一个客户端启动时一旦
 * 空闲就被自动 ensure 路径替换，这是太旧而无法服务 /drain
 * 的 hub 的既有恢复路线。）失败的读数视为“未知”而非“空闲”：
 * 它们永不缩短等待窗口，且没有 `force` 时未确认的 hub
 * 会被解除 drain 交回，而不是 retire。
 *
 * 绝不替换本构建并非严格更新的 hub——构建全序保证
 * 任何安装对至多一侧能到达 retire 步骤，
 * 这正是防止两个混合安装轮流将 hub “升级”为各自构建的机制。
 */
export async function upgradeManagedHub(
	options: UpgradeManagedHubOptions = {},
): Promise<UpgradeManagedHubResult> {
	const owner = resolveDefaultHubOwnerContext();
	const workspaceRoot = options.workspaceRoot ?? process.cwd();
	const discovered = await readHubDiscovery(owner.discoveryPath);
	const live = discovered?.url
		? await safeProbeHubServer(discovered.url, discovered.authToken)
		: undefined;
	if (!live?.url) {
		const ensured = await ensureDetachedHubServer(workspaceRoot);
		return { outcome: "started", ...ensured };
	}
	const record = {
		...live,
		authToken: live.authToken ?? discovered?.authToken,
		pid: live.pid ?? discovered?.pid,
	};
	if (getManagedHubCompatibility(live).compatible) {
		return {
			outcome: "already_current",
			url: live.url,
			authToken: record.authToken,
		};
	}
	if (compareHubBuilds(resolveHubBuildIdentity(), live) <= 0) {
		return {
			outcome: "hub_not_older",
			url: live.url,
			authToken: record.authToken,
		};
	}
	const drained = await requestHubDrain(
		record.url,
		record.authToken,
		options.reason ?? "hub upgrade requested",
	).catch(() => false);
	// 没有接受的 drain，就没有升级——无条件。drain 是准入屏障，
	// 使下面每个读数在 retire 全程保持为真；没有它，
	// 即使明确空闲的读数也只是一个快照，稍后被接纳的
	// 会话会使其失效，而那个会话会死于用户同意提示
	// 从未涵盖的 retire。太旧或卡死而无法接受 drain 的 hub
	// 仍会在下一个客户端启动时一旦空闲被自动 ensure 路径替换。
	if (!drained) {
		throw new Error(
			`The running Cline Hub at ${record.url} did not accept a drain request, so it was not replaced. It is replaced automatically once idle when a Cline client next starts, or run 'cline doctor fix' to stop it now.`,
		);
	}
	// 中止的升级必须把 hub 交回：让它保持 draining 会拒绝
	// 所有新的变更性工作直到重启。
	const undrain = async (): Promise<void> => {
		await requestHubDrain(record.url, record.authToken, "hub upgrade aborted", {
			off: true,
		}).catch(() => false);
	};
	const deadline =
		Date.now() + (options.waitForIdleMs ?? HUB_UPGRADE_DEFAULT_WAIT_MS);
	// 失败的读数视为“未知”，绝不是“空闲”：它不得提前结束等待
	// 窗口、覆盖最后一次真实观察、或自行授权
	// retire——轮次仍在完成时的瞬时查询抖动不得
	// 缩短 drain 旨在提供的宽限。
	// 只有观察到零的读数才会在截止时间前结束窗口。
	//（至少检查一次，使 waitForIdleMs: 0 仍能观察到空闲 hub。）
	let observedSessionCount: number | undefined;
	for (;;) {
		try {
			observedSessionCount = (
				await queryHubSessionActivity(record.url, record.authToken)
			).activeSessionCount;
			if (observedSessionCount === 0) {
				break;
			}
		} catch {
			// 未知；持续轮询直到截止时间。
		}
		if (Date.now() >= deadline) {
			break;
		}
		await new Promise((resolve) =>
			setTimeout(resolve, HUB_UPGRADE_IDLE_POLL_MS),
		);
	}
	const confirmedIdle = observedSessionCount === 0;
	// 没有 force 时，只有明确空闲的 hub 才可被替换：繁忙或
	// 无法应答的 hub 会被解除 drain 并交回。有 force 时，用户已
	// 同意中断提示展示给他们的会话，且接受的 drain
	// 从此到 retire 期间阻止新工作进入。
	if (!confirmedIdle && options.force !== true) {
		await undrain();
		return {
			outcome: "still_busy",
			url: record.url,
			authToken: record.authToken,
			...(observedSessionCount !== undefined
				? { activeSessionCount: observedSessionCount }
				: {}),
		};
	}
	if (!(await retireDiscoveredHub(record, owner.discoveryPath))) {
		await undrain();
		throw new Error(
			`The running Cline Hub at ${record.url} could not be stopped. Run 'cline doctor fix' to stop stale hub daemons, then try again.`,
		);
	}
	const ensured = await ensureDetachedHubServer(workspaceRoot);
	return {
		outcome: "replaced",
		...ensured,
		activeSessionCount: observedSessionCount ?? 0,
	};
}
