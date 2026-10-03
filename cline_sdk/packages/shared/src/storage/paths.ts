import type { Dirent } from "node:fs";
import {
	appendFileSync,
	existsSync,
	mkdirSync,
	readdirSync,
	readFileSync,
	statSync,
} from "node:fs";
import { homedir } from "node:os";
import {
	basename,
	dirname,
	extname,
	isAbsolute,
	join,
	relative,
	resolve,
} from "node:path";
import type { PluginManifest } from "..";
import {
	CLINE_CHAT_WORKSPACE_DIRECTORY_NAME,
	CLINE_WORKSPACES_DIRECTORY_NAME,
} from "./chat-workspace-paths";

// 保持结构部分对浏览器安全，同时通过与数据目录解析器并存的
// 规范 Node 存储路径模块对外暴露它们。
export {
	CLINE_CHAT_WORKSPACE_DIRECTORY_NAME,
	CLINE_WORKSPACES_DIRECTORY_NAME,
	isChatWorkspacePath,
} from "./chat-workspace-paths";

const DEPRECATED_CONFIG_DIR = ".clinerules";
const CLINE_CONFIG_DIR = ".cline";
/**
 * 厂商中立的 `.agents` 目录。最初仅为 agentskills.io 技能约定采用
 *（因此有这个历史名称），现在也是 `.agents/plugins` 下
 * Agent 插件的根目录。
 */
const LEGACY_AGENT_SKILLS_CONFIG_DIR = ".agents";
const AGENTS_CONFIG_DIR = LEGACY_AGENT_SKILLS_CONFIG_DIR;

export const AGENT_CONFIG_DIRECTORY_NAME = "agents";
export const HOOKS_CONFIG_DIRECTORY_NAME = "hooks";
export const SKILLS_CONFIG_DIRECTORY_NAME = "skills";
export const RULES_CONFIG_DIRECTORY_NAME = "rules";
export const WORKFLOWS_CONFIG_DIRECTORY_NAME = "workflows";
export const PLUGINS_DIRECTORY_NAME = "plugins";
export const AGENTS_RULES_FILE_NAME = "AGENTS.md";

/**
 * 标记目录为 Agent 插件（agent-plugins.org）的 Manifest。
 * 根据规范，插件必须在其根目录携带此文件，这使它成为
 * 两类包通道之间的区分器：
 *
 * - Agent 插件  -> `<root>/plugin.json`，在 `.agents/plugins` 下发现
 * - Cline 插件  -> 一个 JS/TS 模块，在 `.cline/plugins` 下发现
 *
 * Cline 插件发现将其视为硬停止：携带此文件的目录
 * 永远不会被扫描 Cline 插件模块，因此为其他客户端编写并放入
 * Cline 插件根目录的插件，其技能脚本或另一厂商的扩展目录
 * 不会被作为 Cline 代码导入。
 */
export const AGENT_PLUGIN_MANIFEST_FILE_NAME = "plugin.json";

/**
 * 所有未指定 `cwd`/`workspaceRoot` 启动的会话的共享工作区。
 * 位于 cline 数据目录下（而非 `os.tmpdir()`），这样操作系统临时文件
 * 清理器绝不会删除用户工作，路径在多用户主机上对用户私有，
 * 且该目录与会话存储共享生命周期和环境变量覆盖。
 */
export function resolveChatWorkspacePath(): string {
	return join(
		resolveClineDataDir(),
		CLINE_WORKSPACES_DIRECTORY_NAME,
		CLINE_CHAT_WORKSPACE_DIRECTORY_NAME,
	);
}

export const CLINE_MCP_SETTINGS_FILE_NAME = "cline_mcp_settings.json";
export const CLINE_CONNECTOR_SETTINGS_FILE_NAME = "settings.json";

function resolveDefaultHomeDir(): string {
	const envHome = process?.env?.HOME?.trim();
	if (envHome && envHome !== "~") {
		return envHome;
	}
	const envUserProfile = process?.env?.USERPROFILE?.trim();
	if (envUserProfile) {
		return envUserProfile;
	}
	const envHomeDrive = process?.env?.HOMEDRIVE?.trim();
	const envHomePath = process?.env?.HOMEPATH?.trim();
	if (envHomeDrive && envHomePath) {
		return `${envHomeDrive}${envHomePath}`;
	}
	const osHomeDir = homedir().trim();
	if (osHomeDir && osHomeDir !== "~") {
		return osHomeDir;
	}
	return "~";
}

let HOME_DIR = resolveDefaultHomeDir();
let HOME_DIR_SET_EXPLICITLY = false;

export function setHomeDir(dir: string) {
	const trimmed = dir.trim();
	if (!trimmed) {
		return;
	}
	HOME_DIR = trimmed;
	HOME_DIR_SET_EXPLICITLY = true;
}

export function setHomeDirIfUnset(dir: string) {
	if (HOME_DIR_SET_EXPLICITLY) {
		return;
	}
	const trimmed = dir.trim();
	if (!trimmed) {
		return;
	}
	HOME_DIR = trimmed;
}

let CLINE_DIR: string | undefined;
let CLINE_DIR_SET_EXPLICITLY = false;

export function setClineDir(dir: string): void {
	const trimmed = dir.trim();
	if (!trimmed) {
		return;
	}
	CLINE_DIR = trimmed;
	CLINE_DIR_SET_EXPLICITLY = true;
}

export function setClineDirIfUnset(dir: string): void {
	if (CLINE_DIR_SET_EXPLICITLY) {
		return;
	}
	const trimmed = dir.trim();
	if (!trimmed) {
		return;
	}
	CLINE_DIR = trimmed;
}

export function resolveClineDir(): string {
	if (CLINE_DIR) {
		return CLINE_DIR;
	}
	const envDir = process.env.CLINE_DIR?.trim();
	if (envDir) {
		return envDir;
	}
	return join(HOME_DIR, ".cline");
}

export function resolveDocumentsClineDirectoryPath(): string {
	return join(HOME_DIR, "Documents", "Cline");
}

type DocumentsExtensionName =
	| "Agents"
	| "Hooks"
	| "Rules"
	| "Workflows"
	| "Plugins";

export function resolveDocumentsExtensionPath(
	name: DocumentsExtensionName,
): string {
	return join(resolveDocumentsClineDirectoryPath(), name);
}

export function resolveClineDataDir(): string {
	const explicitDir = process.env.CLINE_DATA_DIR?.trim();
	if (explicitDir) {
		return explicitDir;
	}
	return join(resolveClineDir(), "data");
}

export function resolveSessionDataDir(): string {
	const explicitDir = process.env.CLINE_SESSION_DATA_DIR?.trim();
	if (explicitDir) {
		return explicitDir;
	}
	return join(resolveClineDataDir(), "sessions");
}

export function resolveTeamDataDir(): string {
	const explicitDir = process.env.CLINE_TEAM_DATA_DIR?.trim();
	if (explicitDir) {
		return explicitDir;
	}
	return join(resolveClineDataDir(), "teams");
}

export function resolveConnectorDataDir(): string {
	const explicitDir = process.env.CLINE_CONNECTOR_DATA_DIR?.trim();
	if (explicitDir) {
		return explicitDir;
	}
	return join(resolveClineDataDir(), "connectors");
}

/**
 * 连接器实例的 stdout/stderr 被捕获的位置。CLI（直接生成
 * 分离的连接器）和 hub 监督器（生成并回收它们）都需要
 * 就这一路径达成一致，因此它放在这里而不是任何一方。
 */
export function resolveConnectorLogPath(
	channel: string,
	instanceKey: string,
): string {
	const safeChannel = channel.replace(/[^a-zA-Z0-9._-]+/g, "_");
	const safeKey = instanceKey.replace(/[^a-zA-Z0-9._-]+/g, "_");
	return join(
		resolveClineDataDir(),
		"logs",
		"connectors",
		safeChannel,
		`${safeKey}.log`,
	);
}

export function resolveConnectorSettingsPath(): string {
	const explicitPath = process.env.CLINE_CONNECTOR_SETTINGS_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveConnectorDataDir(), CLINE_CONNECTOR_SETTINGS_FILE_NAME);
}

export function resolveDbDataDir(): string {
	const explicitDir = process.env.CLINE_DB_DATA_DIR?.trim();
	if (explicitDir) {
		return explicitDir;
	}
	return join(resolveClineDataDir(), "db");
}

/**
 * 专用连接器配置数据库的路径。
 * 与 `sessions.db` 并存但是独立文件，使连接器
 * 凭据/配置与会话存储保持解耦。
 */
export function resolveConnectorsDbPath(): string {
	const explicitPath = process.env.CLINE_CONNECTORS_DB_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveDbDataDir(), "connectors.db");
}

/**
 * 专用 cron/自动化数据库的路径。
 * 与 `sessions.db` 并存但是独立文件，使 cron 生命周期、
 * 保留策略和查询模式与会话存储保持解耦。
 */
export function resolveCronDbPath(): string {
	const explicitPath = process.env.CLINE_CRON_DB_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveDbDataDir(), "cron.db");
}

/** 专用 agenda 任务队列数据库的路径。 */
export function resolveTasksDbPath(): string {
	const explicitPath = process.env.CLINE_TASKS_DB_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveDbDataDir(), "tasks.db");
}

export type TaskSpecsScope = "global" | "workspace";

export interface ResolveTaskSpecsDirOptions {
	/** 显式目录，主要用于测试和嵌入式宿主。 */
	taskSpecsDir?: string;
	scope: TaskSpecsScope;
	/** 工作区作用域任务 spec 所必需。 */
	workspaceRoot?: string;
}

/**
 * Agent 创建的调度任务的主工作区：`~/.cline/schedules/`。
 * Agent 创建的调度是用户级例行任务，因此锚定在此处
 *（其无人值守会话也在此运行），而不是继承碰巧
 * 创建它们的聊天工作区。
 */
export function resolveAgentSchedulesDir(): string {
	return join(resolveClineDir(), "schedules");
}

/** 全局文件支持的 agenda 任务：`~/.cline/tasks/`。 */
export function resolveGlobalTaskSpecsDir(): string {
	return join(resolveClineDir(), "tasks");
}

/** 工作区文件支持的 agenda 任务：`<workspace>/.cline/tasks/`。 */
export function resolveWorkspaceTaskSpecsDir(workspaceRoot: string): string {
	const normalized = workspaceRoot.trim();
	if (!normalized) {
		throw new Error("workspaceRoot is required for workspace task scope");
	}
	return join(normalized, ".cline", "tasks");
}

export function resolveTaskSpecsDir(
	options: ResolveTaskSpecsDirOptions,
): string {
	const explicit = options.taskSpecsDir?.trim();
	if (explicit) {
		return explicit;
	}
	if (options.scope === "workspace") {
		return resolveWorkspaceTaskSpecsDir(options.workspaceRoot ?? "");
	}
	return resolveGlobalTaskSpecsDir();
}

export type CronSpecsScope = "global" | "workspace";

export interface ResolveCronSpecsDirOptions {
	/**
	 * 显式 specs 目录。对测试和希望
	 * 提供自有合并/全局/工作区 cron 源根的未来宿主有用。
	 */
	cronSpecsDir?: string;
	/** 默认为 `global`，即 `~/.cline/cron`。 */
	scope?: CronSpecsScope;
	/** 当 `scope` 为 `workspace` 时必需。 */
	workspaceRoot?: string;
}

/**
 * 全局基于文件的 cron spec 编写目录：
 *   `~/.cline/cron/`
 */
export function resolveGlobalCronSpecsDir(): string {
	return join(resolveClineDir(), "cron");
}

/**
 * 为未来工作区作用域自动化支持保留的工作区
 * 基于文件的 cron spec 编写目录：
 *   `${workspaceRoot}/.cline/cron/`
 */
export function resolveWorkspaceCronSpecsDir(workspaceRoot: string): string {
	return join(workspaceRoot, ".cline", "cron");
}

/**
 * 包含基于文件的 cron spec 编写的目录。
 *
 * 默认：全局 `~/.cline/cron/`。
 * 一次性：`*.md`
 * 循环：`*.cron.md`
 * 事件驱动：`events/*.event.md`
 *
 * 字符串参数作为已弃用的兼容简写保留用于工作区作用域。
 * 新代码应传递 `{ scope: "workspace", workspaceRoot }`
 * 或直接调用 `resolveWorkspaceCronSpecsDir(workspaceRoot)`。
 */
export function resolveCronSpecsDir(workspaceRoot: string): string;
export function resolveCronSpecsDir(
	options?: ResolveCronSpecsDirOptions,
): string;
export function resolveCronSpecsDir(
	input?: string | ResolveCronSpecsDirOptions,
): string {
	if (typeof input === "string") {
		return resolveWorkspaceCronSpecsDir(input);
	}
	if (input?.cronSpecsDir?.trim()) {
		return input.cronSpecsDir.trim();
	}
	if (input?.scope === "workspace") {
		const workspaceRoot = input.workspaceRoot?.trim();
		if (!workspaceRoot) {
			throw new Error("workspaceRoot is required for workspace cron scope");
		}
		return resolveWorkspaceCronSpecsDir(workspaceRoot);
	}
	return resolveGlobalCronSpecsDir();
}

/** 每次运行的 markdown 报告写入目录。 */
export function resolveCronReportsDir(workspaceRoot: string): string;
export function resolveCronReportsDir(
	options?: ResolveCronSpecsDirOptions,
): string;
export function resolveCronReportsDir(
	input?: string | ResolveCronSpecsDirOptions,
): string {
	return join(
		resolveCronSpecsDir(input as ResolveCronSpecsDirOptions),
		"reports",
	);
}

/** cron specs 目录内存放事件 spec 文件的目录。 */
export function resolveCronEventsDir(workspaceRoot: string): string;
export function resolveCronEventsDir(
	options?: ResolveCronSpecsDirOptions,
): string;
export function resolveCronEventsDir(
	input?: string | ResolveCronSpecsDirOptions,
): string {
	return join(
		resolveCronSpecsDir(input as ResolveCronSpecsDirOptions),
		"events",
	);
}

export function resolveProviderSettingsPath(): string {
	const explicitPath = process.env.CLINE_PROVIDER_SETTINGS_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveClineDataDir(), "settings", "providers.json");
}

export function resolveGlobalSettingsPath(): string {
	const explicitPath = process.env.CLINE_GLOBAL_SETTINGS_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveClineDataDir(), "settings", "global-settings.json");
}

export function resolveMcpSettingsPath(): string {
	const explicitPath = process.env.CLINE_MCP_SETTINGS_PATH?.trim();
	if (explicitPath) {
		return explicitPath;
	}
	return join(resolveClineDataDir(), "settings", CLINE_MCP_SETTINGS_FILE_NAME);
}

function dedupePaths(paths: ReadonlyArray<string>): string[] {
	const seen = new Set<string>();
	const deduped: string[] = [];
	for (const candidate of paths) {
		if (!candidate || seen.has(candidate)) {
			continue;
		}
		seen.add(candidate);
		deduped.push(candidate);
	}
	return deduped;
}

function getWorkspaceSkillDirectories(workspacePath?: string): string[] {
	if (!workspacePath) {
		return [];
	}
	return [
		DEPRECATED_CONFIG_DIR,
		CLINE_CONFIG_DIR,
		LEGACY_AGENT_SKILLS_CONFIG_DIR,
	].map((dir) => join(workspacePath, dir, SKILLS_CONFIG_DIRECTORY_NAME));
}

export function resolveAgentsConfigDirPath(): string {
	return join(resolveClineDir(), AGENT_CONFIG_DIRECTORY_NAME);
}

export function resolveAgentConfigSearchPaths(
	workspacePath?: string,
): string[] {
	return dedupePaths([
		workspacePath
			? join(workspacePath, CLINE_CONFIG_DIR, AGENT_CONFIG_DIRECTORY_NAME)
			: "",
		resolveAgentsConfigDirPath(),
	]);
}

export function resolveHooksConfigSearchPaths(
	workspacePath?: string,
): string[] {
	const hooks = [
		resolveDocumentsExtensionPath("Hooks"),
		join(resolveClineDir(), HOOKS_CONFIG_DIRECTORY_NAME),
	];
	if (workspacePath) {
		hooks.push(
			join(workspacePath, DEPRECATED_CONFIG_DIR, HOOKS_CONFIG_DIRECTORY_NAME),
			join(workspacePath, CLINE_CONFIG_DIR, HOOKS_CONFIG_DIRECTORY_NAME),
		);
	}
	return dedupePaths(hooks);
}

export function resolveSkillsConfigSearchPaths(
	workspacePath?: string,
): string[] {
	return dedupePaths([
		...getWorkspaceSkillDirectories(workspacePath),
		join(resolveClineDir(), SKILLS_CONFIG_DIRECTORY_NAME),
		join(
			HOME_DIR,
			LEGACY_AGENT_SKILLS_CONFIG_DIR,
			SKILLS_CONFIG_DIRECTORY_NAME,
		),
	]);
}

export function resolveGlobalAgentsRulesPath(): string {
	return join(HOME_DIR, LEGACY_AGENT_SKILLS_CONFIG_DIR, AGENTS_RULES_FILE_NAME);
}

/**
 * 规则文件可能所在的工作区本地目录：旧版
 * `<workspace>/.clinerules` 布局和当前的
 * `<workspace>/.cline/rules` 布局。每个 Cline 界面（CLI、VS Code
 * 扩展、桌面应用）都必须同时遵循两者——硬编码其中之一的宿主
 * 会静默丢弃另一方的规则（cline/cline#14186）。
 */
export function resolveWorkspaceRulesConfigPaths(
	workspacePath: string,
): string[] {
	return [
		join(workspacePath, DEPRECATED_CONFIG_DIR),
		join(workspacePath, CLINE_CONFIG_DIR, RULES_CONFIG_DIRECTORY_NAME),
	];
}

/**
 * 在 Windows 上，用户的 Documents 文件夹经常被
 * OneDrive "Known Folder Move" 重定向到 `%OneDrive%\Documents`。VS Code
 * 扩展通过操作系统（PowerShell
 * `[Environment]::GetFolderPath(MyDocuments)`）解析真实的 Documents 文件夹并在那里创建全局规则，
 * 因此下面朴素的 `HOME/Documents` 猜测在重定向的机器上
 * 永远看不到它们（cline/cline#14144）。添加 OneDrive 候选项，
 * 使发现覆盖两个位置；缺失的目录会被
 * 扫描器跳过。
 */
function resolveRedirectedDocumentsPaths(): string[] {
	const paths: string[] = [];
	for (const envKey of ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"]) {
		const value = process.env[envKey]?.trim();
		if (value) {
			paths.push(join(value, "Documents"));
		}
	}
	return paths;
}

/**
 * 规则文件可能所在的全局（用户级）目录，按从 SDK 原生位置
 * 到 VS Code Rules 标签页使用的 Documents 位置排序。
 */
export function resolveGlobalRulesConfigPaths(): string[] {
	return dedupePaths([
		join(resolveClineDir(), RULES_CONFIG_DIRECTORY_NAME),
		// VS Code Rules 标签页通过 `xdg-user-dir DOCUMENTS` 解析 Documents，
		// 未配置时（WSL/无头环境）会打印裸 $HOME，导致
		// 全局规则位于 ~/Cline/Rules 而非 ~/Documents/Cline/Rules
		//（cline/cline#13542）。
		join(HOME_DIR, "Cline", "Rules"),
		resolveDocumentsExtensionPath("Rules"),
		...resolveRedirectedDocumentsPaths().map((documentsPath) =>
			join(documentsPath, "Cline", "Rules"),
		),
	]);
}

export function resolveRulesConfigSearchPaths(
	workspacePath?: string,
): string[] {
	const wsPaths = workspacePath
		? resolveWorkspaceRulesConfigPaths(workspacePath)
		: [];
	const workspaceAgentsFile = workspacePath
		? [join(workspacePath, AGENTS_RULES_FILE_NAME)]
		: [];
	return dedupePaths([
		...workspaceAgentsFile,
		...wsPaths,
		resolveGlobalAgentsRulesPath(),
		...resolveGlobalRulesConfigPaths(),
	]);
}

export function resolveWorkflowsConfigSearchPaths(
	workspacePath?: string,
): string[] {
	return dedupePaths([
		workspacePath
			? join(workspacePath, ".clinerules", WORKFLOWS_CONFIG_DIRECTORY_NAME)
			: "",
		resolveDocumentsExtensionPath("Workflows"),
		join(resolveClineDir(), WORKFLOWS_CONFIG_DIRECTORY_NAME),
		workspacePath
			? join(workspacePath, ".cline", WORKFLOWS_CONFIG_DIRECTORY_NAME)
			: "",
	]);
}

export function resolvePluginConfigSearchPaths(
	workspacePath?: string,
): string[] {
	return dedupePaths([
		workspacePath ? join(workspacePath, ".cline", PLUGINS_DIRECTORY_NAME) : "",
		join(resolveClineDir(), PLUGINS_DIRECTORY_NAME),
		resolveDocumentsExtensionPath("Plugins"),
	]);
}

/**
 * 搜索 Agent 插件（agent-plugins.org）的根目录。与
 * {@link resolvePluginConfigSearchPaths} 保持分离，使两个包通道永不共享
 * 目录：`.agents/plugins` 是厂商中立位置，与技能发现已遵循的
 * `.agents/skills` 约定一致。
 *
 * 发现根目录由规范显式定义为客户端职责。Cline
 * 仅从 Hub 宿主的主目录自动发现用户安装的包，
 * 这样打开一个仓库无法激活仓库控制的 MCP 服务器。
 */
export function resolveAgentPluginSearchPaths(): string[] {
	return [join(HOME_DIR, AGENTS_CONFIG_DIR, PLUGINS_DIRECTORY_NAME)];
}

const PLUGIN_MODULE_EXTENSIONS = new Set([".js", ".ts"]);
const PLUGIN_PACKAGE_JSON_FILE_NAME = "package.json";
const PLUGIN_DIRECTORY_INDEX_CANDIDATES = ["index.ts", "index.js"];
/**
 * Cline 插件发现期间绝不下降进入。依赖树永远不是
 * 一组插件条目：逐个导入其文件会绕过每个
 * 包自己的入口点，而对于有真实依赖的插件，这意味着
 * 会话启动时数千次导入。
 */
const PLUGIN_SCAN_EXCLUDED_DIRECTORY_NAMES = new Set(["node_modules"]);

/**
 * 当 `directoryPath` 根目录携带 Agent Plugin manifest 时为 true。
 *
 * 此处仅检查 manifest 的存在性。验证其内容
 *（`$schema`、`name`、封闭字段集）属于 Agent Plugin 加载器的职责；
 * 发现只需要知道此目录属于另一通道，
 * 不得为 Cline 插件模块而扫描。
 */
export function isAgentPluginDirectory(directoryPath: string): boolean {
	try {
		return statSync(
			join(directoryPath, AGENT_PLUGIN_MANIFEST_FILE_NAME),
		).isFile();
	} catch {
		return false;
	}
}

interface PluginPackageManifest {
	plugins?: PluginManifest[];
}

export function isPluginModulePath(path: string): boolean {
	const dot = path.lastIndexOf(".");
	if (dot === -1) {
		return false;
	}
	return PLUGIN_MODULE_EXTENSIONS.has(path.slice(dot));
}

function readPluginPackageManifest(
	packageJsonPath: string,
): PluginPackageManifest | null {
	try {
		const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8")) as {
			cline?: PluginPackageManifest;
		};
		if (!packageJson.cline || typeof packageJson.cline !== "object") {
			return null;
		}
		return packageJson.cline;
	} catch {
		return null;
	}
}

function getManifestPluginEntries(
	manifest: PluginPackageManifest | null,
): string[] {
	const entries = manifest?.plugins;
	if (!Array.isArray(entries)) {
		return [];
	}
	return entries.flatMap((entry) => entry.paths ?? []);
}

export function resolvePluginModuleEntries(
	directoryPath: string,
): string[] | null {
	const root = resolve(directoryPath);
	if (!existsSync(root) || !statSync(root).isDirectory()) {
		return null;
	}

	// Agent Plugin 不是 Cline 插件。此处不从中认领任何内容，
	// 使指向它的显式配置路径解析为零模块，
	// 而不是导入其中碰巧存在的任何 JS/TS。
	if (isAgentPluginDirectory(root)) {
		return null;
	}

	const packageJsonPath = join(root, PLUGIN_PACKAGE_JSON_FILE_NAME);
	if (existsSync(packageJsonPath)) {
		const manifest = readPluginPackageManifest(packageJsonPath);
		const entries = getManifestPluginEntries(manifest)
			.map((entry) => resolve(root, entry))
			.filter(
				(entryPath) =>
					existsSync(entryPath) &&
					statSync(entryPath).isFile() &&
					isPluginModulePath(entryPath),
			);
		if (entries.length > 0) {
			return entries;
		}
	}

	for (const candidate of PLUGIN_DIRECTORY_INDEX_CANDIDATES) {
		const entryPath = join(root, candidate);
		if (existsSync(entryPath) && statSync(entryPath).isFile()) {
			return [entryPath];
		}
	}

	return null;
}

function readPackageName(packageJsonPath: string): string | undefined {
	try {
		const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8")) as {
			name?: unknown;
		};
		return typeof packageJson.name === "string" && packageJson.name.trim()
			? packageJson.name.trim()
			: undefined;
	} catch {
		return undefined;
	}
}

function isPathWithin(parentPath: string, childPath: string): boolean {
	const relativePath = relative(resolve(parentPath), resolve(childPath));
	return (
		relativePath === "" ||
		(!relativePath.startsWith("..") && !isAbsolute(relativePath))
	);
}

/**
 * 插件模块条目的人类可读名称。包支撑的插件
 *（例如 `~/.cline/plugins/_installed/<id>/package/index.ts`）以
 * `searchRoot` 内最近祖先 `package.json` 中的 `name` 命名，这样
 * 每次安装不会都显示为 "index"。裸模块文件回退到
 * 文件基名。
 */
export function getPluginDisplayName(
	filePath: string,
	searchRoot: string,
): string {
	let current = dirname(filePath);
	const root = resolve(searchRoot);
	while (isPathWithin(root, current)) {
		const packageJsonPath = join(current, PLUGIN_PACKAGE_JSON_FILE_NAME);
		if (existsSync(packageJsonPath)) {
			const packageName = readPackageName(packageJsonPath);
			if (packageName) {
				return packageName;
			}
			break;
		}
		const parent = resolve(current, "..");
		if (parent === current) {
			break;
		}
		current = parent;
	}
	return basename(filePath, extname(filePath));
}

export function discoverPluginModulePaths(directoryPath: string): string[] {
	const root = resolve(directoryPath);
	if (!existsSync(root)) {
		return [];
	}
	// 扫描根本身可以是插件根，例如当配置的插件
	// 路径直接指向一个 Agent Plugin 时。
	if (isAgentPluginDirectory(root)) {
		return [];
	}
	const discovered: string[] = [];
	const stack = [root];
	while (stack.length > 0) {
		const current = stack.pop();
		if (!current) {
			continue;
		}
		let entries: Dirent[];
		try {
			entries = readdirSync(current, { withFileTypes: true });
		} catch {
			continue;
		}
		for (const entry of entries) {
			const candidate = join(current, entry.name);
			if (entry.isDirectory()) {
				// Agent 插件拥有其整个子树。下降进入会将其技能脚本、
				// 另一厂商的扩展目录及其 vendored 依赖
				// 当作每个都是 Cline 插件来导入——
				// 而加载器在验证模块之前就导入它，因此这种
				// 执行无法撤回。
				if (isAgentPluginDirectory(candidate)) {
					continue;
				}
				if (
					PLUGIN_SCAN_EXCLUDED_DIRECTORY_NAMES.has(entry.name) ||
					entry.name.startsWith(".")
				) {
					continue;
				}
				const packageJsonPath = join(candidate, PLUGIN_PACKAGE_JSON_FILE_NAME);
				if (existsSync(packageJsonPath)) {
					const manifest = readPluginPackageManifest(packageJsonPath);
					const entries = getManifestPluginEntries(manifest)
						.map((e) => resolve(candidate, e))
						.filter(
							(entryPath) =>
								existsSync(entryPath) &&
								statSync(entryPath).isFile() &&
								isPluginModulePath(entryPath),
						);
					if (entries.length > 0) {
						discovered.push(...entries);
						continue;
					}
				}
				stack.push(candidate);
				continue;
			}
			if (entry.name.startsWith(".")) {
				continue;
			}
			if (entry.isFile() && isPluginModulePath(candidate)) {
				discovered.push(candidate);
			}
		}
	}
	return discovered.sort((a, b) => a.localeCompare(b));
}

export function resolveConfiguredPluginModulePaths(
	pluginPaths: ReadonlyArray<string>,
	cwd: string,
): string[] {
	const resolvedPaths: string[] = [];
	for (const pluginPath of pluginPaths) {
		const trimmed = pluginPath.trim();
		if (!trimmed) {
			continue;
		}
		const absolutePath = resolve(cwd, trimmed);
		if (!existsSync(absolutePath)) {
			throw new Error(`Plugin path does not exist: ${absolutePath}`);
		}
		const stats = statSync(absolutePath);
		if (stats.isDirectory()) {
			const entries = resolvePluginModuleEntries(absolutePath);
			if (entries) {
				resolvedPaths.push(...entries);
				continue;
			}
			resolvedPaths.push(...discoverPluginModulePaths(absolutePath));
			continue;
		}
		if (!isPluginModulePath(absolutePath)) {
			throw new Error(
				`Plugin file must use a supported extension (${[...PLUGIN_MODULE_EXTENSIONS].join(", ")}): ${absolutePath}`,
			);
		}
		resolvedPaths.push(absolutePath);
	}
	return resolvedPaths;
}

export function ensureParentDir(filePath: string): void {
	const parent = dirname(filePath);
	if (!existsSync(parent)) {
		mkdirSync(parent, { recursive: true });
	}
}

export function ensureFileExists(filePath: string): void {
	mkdirSync(dirname(filePath), { recursive: true });
	appendFileSync(filePath, "");
}

export function ensureHookLogDir(filePath?: string): string {
	if (filePath?.trim()) {
		ensureParentDir(filePath);
		return dirname(filePath);
	}
	const dir = join(resolveClineDataDir(), "logs");
	if (!existsSync(dir)) {
		mkdirSync(dir, { recursive: true });
	}
	return dir;
}
