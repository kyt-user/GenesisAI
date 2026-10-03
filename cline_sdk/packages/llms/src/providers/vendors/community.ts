import { accessSync, existsSync, constants as fsConstants } from "node:fs";
import { createRequire } from "node:module";
import { delimiter, dirname, join } from "node:path";
import type { GatewayResolvedProviderConfig } from "@cline/shared";
// 保持此导入为静态的，使 VS Code 扩展包包含 SAP
// provider。把它藏在计算型动态导入后面会让已发布的
// 扩展在运行时尝试从 node_modules 加载 @jerome-benoit/sap-ai-provider，
// 但 VSIX 打包使用的是打包后的扩展输出。
import { createSAPAIProvider } from "@jerome-benoit/sap-ai-provider";
import { createDifyProvider } from "dify-ai-provider";
import { resolveApiKey } from "../http";
import type { ProviderFactoryResult } from "./types";

type SapModel = Record<PropertyKey, unknown>;
const SAP_SERVICE_KEY_METHODS = new Set<PropertyKey>([
	"doGenerate",
	"doStream",
	"doEmbed",
]);
let sapServiceKeyQueue: Promise<void> = Promise.resolve();

function readOptions(
	config: GatewayResolvedProviderConfig,
): Record<string, unknown> {
	return (config.options as Record<string, unknown> | undefined) ?? {};
}

function findExecutableOnPath(name: string): string | undefined {
	const extensions =
		process.platform === "win32" ? [".exe", ".cmd", ".bat", ""] : [""];
	for (const dir of (process.env.PATH ?? "").split(delimiter)) {
		if (!dir) continue;
		for (const ext of extensions) {
			const candidate = join(dir, `${name}${ext}`);
			try {
				accessSync(candidate, fsConstants.X_OK);
				return candidate;
			} catch {
				// 不在这里；继续找
			}
		}
	}
	return undefined;
}

// agent SDK 生成一个由各平台可选包
//（@anthropic-ai/claude-agent-sdk-<platform>-<arch>[-musl]）提供的 `claude` 可执行文件。
// 这些不再默认安装（约 250MB），因此解析显式路径：
// 存在时使用捆绑的平台二进制，否则使用用户从 PATH 安装的
// Claude Code。此处不能依赖 SDK 自己的解析：
// 在 Bun 编译的二进制内，它锚定在虚拟 bunfs 上，那里
// node_modules 查找永远看不到磁盘上的包。
function resolveClaudeExecutable(): string | undefined {
	const suffixes =
		process.platform === "linux"
			? [
					`${process.platform}-${process.arch}`,
					`${process.platform}-${process.arch}-musl`,
				]
			: [`${process.platform}-${process.arch}`];
	const executableName = process.platform === "win32" ? "claude.exe" : "claude";
	// 首先锚定真实可执行文件位置，使解析在编译后的
	// 二进制中也能工作；对普通 node 回退到此模块的位置。
	const anchors = [join(dirname(process.execPath), "noop.js"), import.meta.url];
	for (const anchor of anchors) {
		for (const suffix of suffixes) {
			try {
				const manifest = createRequire(anchor).resolve(
					`@anthropic-ai/claude-agent-sdk-${suffix}/package.json`,
				);
				const executable = join(dirname(manifest), executableName);
				accessSync(executable, fsConstants.X_OK);
				return executable;
			} catch {
				// 继续找
			}
		}
	}
	return findExecutableOnPath("claude");
}

export async function createClaudeCodeProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	// 动态导入是有意的：ai-sdk-provider-claude-code 是
	// 可选 peer 依赖，因此默认安装会跳过其约 250MB 的
	// @anthropic-ai/claude-agent-sdk 平台二进制。它还在模块作用域
	// 运行 createClaudeCode()，因此惰性加载将该副作用限制在
	// 实际使用 Claude Code 时。
	let createClaudeCode: typeof import("ai-sdk-provider-claude-code").createClaudeCode;
	try {
		({ createClaudeCode } = await import("ai-sdk-provider-claude-code"));
	} catch (error) {
		throw new Error(
			"The Claude Code provider requires the optional 'ai-sdk-provider-claude-code' package. " +
				"Install it alongside @cline/llms to use this provider.",
			{ cause: error },
		);
	}
	const { cwd: workspaceCwd, ...options } = readOptions(config);
	const defaultSettings: Record<string, unknown> = {
		...((options.defaultSettings as Record<string, unknown> | undefined) ?? {}),
	};
	if (defaultSettings.pathToClaudeCodeExecutable === undefined) {
		const executable = resolveClaudeExecutable();
		if (executable !== undefined) {
			defaultSettings.pathToClaudeCodeExecutable = executable;
		}
	}
	// 宿主将工作区根作为顶层 `cwd` 选项转发（例如
	// @cline/core 的 buildGatewayProviderOptions）。将生成的 agent
	// 会话锚定在那里；否则它会继承宿主进程的 cwd（GUI
	// 扩展宿主中为 `/`）并拒绝其外的写入。以存在性为守卫：
	// provider 对缺失目录会硬失败设置验证。
	if (
		defaultSettings.cwd === undefined &&
		typeof workspaceCwd === "string" &&
		workspaceCwd.length > 0 &&
		existsSync(workspaceCwd)
	) {
		defaultSettings.cwd = workspaceCwd;
	}
	// provider 默认 settingSources 为 []——会话将既不读取
	// ~/.claude/settings.json 也不读取项目 .claude/settings.json，因此
	// 用户配置的权限规则会静默地永不生效。
	if (defaultSettings.settingSources === undefined) {
		defaultSettings.settingSources = ["user", "project"];
	}
	// Cline 没有接入 CLI 会话的交互式权限提示
	//（没有 canUseTool），因此任何未预先批准的内容都会被直接拒绝。在
	// 默认模式下那意味着每次文件写入都失败。acceptEdits
	// 自动批准 cwd 下的文件编辑，同时让命令执行
	// 仍由用户自己的 Claude 设置门控（通过 settingSources 加载）。
	if (defaultSettings.permissionMode === undefined) {
		defaultSettings.permissionMode = "acceptEdits";
	}
	const provider = createClaudeCode({ ...options, defaultSettings });
	return {
		operations: { language: (modelId) => provider(modelId) },
	};
}

export async function createOpenAICodexProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	// 动态导入是有意的：ai-sdk-provider-codex-cli 是可选
	// peer 依赖，因此默认安装会跳过其约 105MB 的 @openai/codex
	// 可选依赖。provider 本身在捆绑二进制缺失时
	// 优雅降级（npx -y @openai/codex，然后是 PATH 上的 `codex`）。
	let createCodexExec: typeof import("ai-sdk-provider-codex-cli").createCodexExec;
	try {
		({ createCodexExec } = await import("ai-sdk-provider-codex-cli"));
	} catch (error) {
		throw new Error(
			"The OpenAI Codex provider requires the optional 'ai-sdk-provider-codex-cli' package. " +
				"Install it alongside @cline/llms to use this provider.",
			{ cause: error },
		);
	}
	const provider = createCodexExec(readOptions(config));
	return {
		operations: { language: (modelId) => provider(modelId) },
	};
}

// ai-sdk-provider-opencode-sdk 注册 process.once("SIGINT") 和
// process.once("SIGTERM") 处理器，会立即调用 process.exit()。
// 库绝不能劫持进程生命周期——那是宿主
// 应用的职责。这些处理器阻止宿主应用（如
// Kanban）执行优雅关闭（例如持久化状态、
// 清理 worktree），因为 opencode 处理器先触发并
// 强制退出进程。
//
// 变通方案：在创建 provider 之前快照监听器，然后移除
// 库添加的任何新 SIGINT/SIGTERM 监听器。
//
// TODO：一旦 ai-sdk-provider-opencode-sdk 不再从信号处理器
// 调用 process.exit() 就移除此变通方案。
async function stripRogueSignalHandlers<T>(fn: () => Promise<T>): Promise<T> {
	const signals = ["SIGINT", "SIGTERM"] as const;
	const before = new Map(
		signals.map((sig) => [sig, new Set(process.listeners(sig))]),
	);
	const result = await fn();
	for (const sig of signals) {
		for (const listener of process.listeners(sig)) {
			if (!before.get(sig)?.has(listener)) {
				process.removeListener(sig, listener);
			}
		}
	}
	return result;
}

export async function createOpenCodeProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	// 动态导入是有意的：ai-sdk-provider-opencode-sdk 在模块作用域
	// 运行 `var opencode = createOpencode()`，它注册
	// process.once("SIGINT") / process.once("SIGTERM") 处理器并调用
	// process.exit(0)。在 stripRogueSignalHandlers 内导入它确保
	// 模块副作用和显式 createOpencode() 调用都被
	// 捕获，从而移除流氓处理器。
	// TODO：一旦上游包不再从信号处理器
	// 调用 process.exit() 就改回静态导入。
	const provider = await stripRogueSignalHandlers(async () => {
		const { createOpencode } = await import("ai-sdk-provider-opencode-sdk");
		return createOpencode(readOptions(config));
	});
	return {
		operations: { language: (modelId) => provider(modelId) },
	};
}

export async function createDifyProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	const apiKey = await resolveApiKey(config);
	const provider = createDifyProvider({
		baseURL: config.baseUrl,
		headers: config.headers,
		fetch: config.fetch,
		...readOptions(config),
	});
	return {
		operations: {
			language: (modelId) =>
				provider(modelId, {
					apiKey,
				}),
		},
	};
}

function readStringOption(
	options: Record<string, unknown>,
	key: string,
): string | undefined {
	const value = options[key];
	return typeof value === "string" && value.trim().length > 0
		? value.trim()
		: undefined;
}

function normalizeSapTokenBaseUrl(tokenUrl: string): string {
	const trimmed = tokenUrl.replace(/\/+$/, "");
	return trimmed.replace(/\/oauth\/token$/i, "");
}

function hasExplicitSapConnectionConfig(
	config: GatewayResolvedProviderConfig,
	options: Record<string, unknown>,
): boolean {
	return Boolean(
		config.apiKey?.trim() ||
			config.baseUrl?.trim() ||
			readStringOption(options, "clientId") ||
			readStringOption(options, "clientSecret") ||
			readStringOption(options, "tokenUrl"),
	);
}

function buildSapServiceKey(
	config: GatewayResolvedProviderConfig,
	options: Record<string, unknown>,
): string | undefined {
	const clientId = readStringOption(options, "clientId");
	const clientSecret =
		readStringOption(options, "clientSecret") ?? config.apiKey?.trim();
	const tokenUrl = readStringOption(options, "tokenUrl");
	const baseUrl = config.baseUrl?.trim();
	if (!clientId || !clientSecret || !tokenUrl || !baseUrl) {
		if (!hasExplicitSapConnectionConfig(config, options)) {
			return undefined;
		}
		const missing = [
			!clientId ? "sap.clientId" : undefined,
			!clientSecret ? "sap.clientSecret" : undefined,
			!tokenUrl ? "sap.tokenUrl" : undefined,
			!baseUrl ? "baseUrl" : undefined,
		].filter(Boolean);
		throw new Error(
			`SAP AI Core provider is missing required configuration: ${missing.join(
				", ",
			)}.`,
		);
	}
	return JSON.stringify({
		clientid: clientId,
		clientsecret: clientSecret,
		serviceurls: {
			AI_API_URL: baseUrl.replace(/\/+$/, ""),
		},
		url: normalizeSapTokenBaseUrl(tokenUrl),
	});
}

function resolveSapApi(options: Record<string, unknown>) {
	const api = options.api;
	if (api === "orchestration" || api === "foundation-models") {
		return api;
	}
	if (options.useOrchestrationMode === false) {
		return "foundation-models";
	}
	return "orchestration";
}

async function withSapServiceKey<T>(
	serviceKey: string | undefined,
	fn: () => T,
): Promise<Awaited<T>> {
	if (!serviceKey) {
		return await fn();
	}

	const previousQueue = sapServiceKeyQueue.catch(() => {});
	let releaseQueue!: () => void;
	sapServiceKeyQueue = new Promise<void>((resolve) => {
		releaseQueue = resolve;
	});

	await previousQueue;
	const previous = process.env.AICORE_SERVICE_KEY;
	process.env.AICORE_SERVICE_KEY = serviceKey;
	try {
		return await fn();
	} finally {
		restoreSapServiceKey(previous);
		releaseQueue();
	}
}

function shouldWrapSapServiceKeyMethod(property: PropertyKey): boolean {
	return SAP_SERVICE_KEY_METHODS.has(property);
}

function restoreSapServiceKey(previous: string | undefined): void {
	if (previous === undefined) {
		delete process.env.AICORE_SERVICE_KEY;
		return;
	}
	process.env.AICORE_SERVICE_KEY = previous;
}

function wrapSapModelWithServiceKey(
	model: unknown,
	serviceKey: string | undefined,
): unknown {
	if (!serviceKey || !model || typeof model !== "object") {
		return model;
	}
	return new Proxy(model as SapModel, {
		get(target, property, receiver) {
			const value = Reflect.get(target, property, receiver);
			if (
				typeof value !== "function" ||
				!shouldWrapSapServiceKeyMethod(property)
			) {
				return value;
			}
			return (...args: unknown[]) =>
				withSapServiceKey(serviceKey, () => value.apply(target, args));
		},
	});
}

export async function createSapAiCoreProviderModule(
	config: GatewayResolvedProviderConfig,
): Promise<ProviderFactoryResult> {
	const options = readOptions(config);
	const serviceKey = buildSapServiceKey(config, options);

	const deploymentId = readStringOption(options, "deploymentId");
	const provider = createSAPAIProvider({
		name: config.providerId,
		...(deploymentId
			? { deploymentId }
			: { resourceGroup: readStringOption(options, "resourceGroup") }),
		api: resolveSapApi(options),
		...(typeof options.defaultSettings === "object" &&
		options.defaultSettings !== null &&
		!Array.isArray(options.defaultSettings)
			? { defaultSettings: options.defaultSettings }
			: {}),
		requestConfig: {
			headers: { "ai-client-type": "Cline" },
			// 镜像 `getAxiosSettings()` 的标准 cline axios 设置
			adapter: "fetch",
			...(config.fetch ? { fetch: config.fetch } : {}),
			maxBodyLength: Number.POSITIVE_INFINITY,
			maxContentLength: Number.POSITIVE_INFINITY,
		},
	});
	return {
		operations: {
			language: (modelId) =>
				wrapSapModelWithServiceKey(provider(modelId), serviceKey),
		},
	};
}
