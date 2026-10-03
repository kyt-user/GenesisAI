## 共享的 Agent 评审 UI

`@cline/ui` 导出仅用于展示的组件，用于显示会话的已更改文件与拉取请求状态。在导入 `@cline/ui/components.css` 之前，请先配置 Tailwind v4 与 token 入口，遵循[完整的样式设置](./packages/ui/ADOPTION.md#option-2-tailwind-mappings-without-base-styles)。然后从包根导入组件：

```tsx
import {
  AgentChangedFile,
  AgentChangesPanel,
  AgentPullRequestBar,
} from "@cline/ui";
```

`AgentChangesPanel` 拥有 Changes 标题头、计数、关闭操作、空状态与滚动区域。组合 `AgentChangedFile` 子组件来展示可折叠路径、复制反馈、增删行数、宿主提供的操作与渲染后的 diff 内容。宿主保留变更收集、剪贴板与编辑器集成，以及会话聚焦。

`AgentPullRequestBar` 接受规范化的 `AgentPullRequestData` 以及加载与错误状态。宿主拥有刷新、轮询、导航与遥测，并通过 `renderChecks` 提供其可访问的检查弹出层。原生宿主可用 `onNavigate` 拦截链接；Web 宿主可省略它以渲染外部锚点。`getAgentPullRequestMergeStatus` 与 `summarizeAgentPullRequestChecks` 为其他宿主的展示暴露相同的状态规范化。

## Fork 元数据

来自 `@cline/core` 的 `createForkSessionMetadata` 复制元数据、替换 fork 谱系，并移除继承的交接标记。调用方提供来源 ID、时间戳、source 以及可选的 `beforeRunCount`；标题与会话创建仍归调用方所有。

## 云会话（实验性）

`CloudSessionApi` 与 `CloudSessionController` 从 `@cline/core/cloud` 导出。API 处理 REST 请求；controller 处理远程 Hub 连接、会话生命周期、转录对账与审批。宿主提供 API URL 与一个获取新 token 的回调；功能门控与账户选择留在宿主中。导入此子路径不会启动本地 agent。

使用 `subscribe` 获取不可变快照与实时事件，用 `attach`/`readMessages` 打开会话，用 `send` 进行后续发送。附加一个正在配置或已失败的会话会返回其回执而不建立连接。`detach` 关闭此查看器，而不是远程任务。宿主关闭时调用 `dispose`。用 `readMessages` 水合活跃运行的查看器会在完成时对账规范历史，即使它们错过了运行开始事件与更早的内容增量。

在凭据刷新期间替换 controller 的宿主可以共享 `pendingInitialTasks: Map<string, CloudCreationOptions>` 构造函数选项。它会保留首任务的审批/思考/推理偏好，包括通过 `restoreCreationOptions` 进行的更新，直到内部任务被找到或创建（或外部会话被删除）。`dispose` 会保留注入的 map；没有注入时，controller 拥有并清除其待处理状态。仅恢复选项永远不能授权重建缺失的既有任务。

## 实验性云交接

`loadCloudModels` 与 `CloudSessionController.listModels()` 提供符合条件的云模型。交接需要所选模型；它绝不替换为其他模型。

使用 `create({ handoff, ... })` 进行配置与种子化，或用 `seedHandoff(id, seed)` 处理既有目标。通过回调持久化目标 ID 与派发标记；在派发不确定后使用 `recoverOnly` 以避免重复对话。交接创建需要 `onCreating`：在返回前持久保存意图，并在重启后拒绝先前未确认的意图。回调错误保留其原始类型；它们不能证明更早的 POST 被拒绝，也不允许清除其意图。`CloudHandoffSeedRejectedError` 表示没有种子被派发；重试已保存目标前只需清除种子标记。`verifyHandoffTranscript` 检查种子化的历史；`waitUntilReady(id)` 等待配置完成。

## 语音输入模型

来自 `@cline/core` 的 `getLocalTranscriptionModels(providerId, config?)` 返回某提供商的转写传输所支持的语音模型。用它构建语音选择器，而不是过滤打包的聊天目录。Vercel 使用其实时模型列表与公布的流式标签；不可用或格式错误的响应会使发现失败，而不是退回陈旧的打包模型。语音选择会被保存，批量与流式执行都会通过同一服务重新校验。

来自 `@cline/shared` 的 `isTranscriptionModel`（也为浏览器导出）接受精确的仅音频输入与仅文本输出模态。显式的转写标签不会覆盖额外的输入或输出模态。多模态实时模型被归类为 `realtime`，目前没有内置传输支持，并被排除在语音与聊天选择器之外。专用转写仍可使用批量或流式模式。仅凭分类不能证明提供商实现了所需的传输。

`createStreamingAudioTranscriptionSession` 铸造短期的 Vercel 或一次性 ElevenLabs 凭据。其共享响应包括 `transport` 与 `sampleRate`；浏览器客户端必须以该采样率采集 PCM（Google 实时路由要求 16 kHz）。ElevenLabs 分别暴露批量 `scribe_v2` 与实时 `scribe_v2_realtime`。

## SSH 远程环境

`RemoteEnvironmentService`（由 `@cline/core` 与 `@cline/sdk` 导出）拥有 SSH 配置文件、连接测试、辅助程序安装、已认证的环回隧道、远程命令、状态变更与清理。它运行在客户端的 Node 宿主中；浏览器客户端通过其宿主传输暴露此 API。不需要任何桌面代码。支持 OpenSSH 配置别名、身份文件与 ssh-agent 认证。连接使用批处理模式，并要求 OpenSSH known_hosts 中已有受信任的主机密钥（或 `knownHostsPath`）。首次使用前，请通过可信渠道核实服务器指纹，并用你的 SSH 客户端录入。未知或已变更的密钥会在检查、上传或执行之前被拒绝。

```ts
import { ClineCore, RemoteEnvironmentService } from "@cline/core";

const environments = new RemoteEnvironmentService({
  helperBinaryDirectory: "/opt/my-client/remote-helpers",
  onStatusChange: (status) => console.log(status),
});
const profile = await environments.upsert({ name: "Build host", host: "builder" });
const connection = await environments.connect(profile.id);
const core = await ClineCore.create({
  clientName: "my-client",
  backendMode: "remote",
  remote: {
    endpoint: connection.endpoint,
    authToken: connection.authToken,
    workspaceRoot: connection.workspaceRoot,
  },
});
try {
  // 普通的会话、工具、审批与事件 API 都在此宿主上执行。
  // 像其他远程 hub 一样，在会话配置中提供提供商凭据。
  console.log(await core.list());
} finally {
  await core.dispose();
  await environments.dispose();
}
```

该服务还暴露 `list`、`upsert`、`delete`、`test`、`disconnect`、`run`、`getConnection`、`getActive`、`activateConnection` 与 `getStatuses`。`onConnectionLost` 让客户端在隧道失败后退役运行时绑定。每个服务实例都有唯一的远程 Hub 发现记录，因此另一个客户端连接到同一主机无法停止其 Hub。连接/断开/配置变更会被串行化；并发连接复用同一隧道。先销毁 `ClineCore` 运行时，再断开其环境。不要把连接的身份认证 token 暴露给浏览器或日志。

配置文件默认位于 `~/.cline/data/settings/remote-environments.json`，以 0600 权限原子写入。它们包含身份文件路径，绝不包含私钥。选项包括 `profilesPath`、`sshPath`、`knownHostsPath`、进程超时、`helperBinaryPath` 与 `helperBinaryDirectory`。对应的辅助程序/SSH 配置变量为 `CLINE_REMOTE_HELPER_BINARY`、`CLINE_REMOTE_HELPER_DIRECTORY`、`CLINE_SSH_PATH` 与 `CLINE_SSH_KNOWN_HOSTS_FILE`。

客户端使用 `@cline/core/remote/helper-entry` 可执行入口打包一个匹配的自包含辅助程序，用 Bun 针对远程操作系统与架构编译。使用 `remoteHelperBinaryFilename({ platform, arch })` 生成文件名（`cline-remote-helper-<target-triple>`）。支持 x64/arm64 上的 Linux 与 macOS。辅助程序必须包含与客户端相同的 SDK 构建；缺少辅助程序会产生明确错误，而不会从网络安装运行时。该辅助程序实现了 `--remote-hub-ensure --cwd <path> --discovery-path <path>` 与核心分离守护进程哨兵。Agent 工具与持久化在远程运行；宿主只管理 SSH 并转发已认证的 hub 连接。


## 并发的子 agent 工具调用

`spawn_agent` 与配置的 `subagent_*` 工具声明 `executionMode: "parallel"`。即使父运行时使用默认的顺序模式，一次模型响应中对这些工具的连续调用也会并发运行。每次调用仍返回其子任务的完成答案，工具结果保持模型原始调用顺序。

工具可选的 `executionMode` 会覆盖运行时的 `toolExecution` 设置。未标记的工具继承运行时设置。顺序调用形成顺序边界：`read_file → [spawn A, spawn B] → edit_file` 先执行读取，然后两个子运行一起执行，最后在两者都完成后执行编辑。这不会改变子 agent 内部工具的执行模式。

准备阶段在整个响应中保持串行，先于任何工具执行。因此所有 before-tool hook 与必需的审批都在并行组开始前完成。before-tool 的 `skip` 阻止其自身调用；before-tool 的 `stop` 阻止整个响应的执行，与之前一致。待处理的审批可能延迟同级执行。不引入后台运行句柄或新的并发限制。

### 配置的子 agent 审批

配置的 agent 不暴露工具审批策略设置。父级的 `subagent_<name>` 调用遵循父会话的审批策略；子级执行其可用工具时不继承该策略或审批回调。其配置的 `tools` 允许列表与禁用工具过滤仍然生效。运行时 hook 仍然被继承，并可阻止工具执行。


## 共享 UI 会话行

`@cline/ui` 导出 `AgentSessionRow`、`AgentSessionRowEditor` 与 `AgentSessionOverview`，以及它们的公共 props 类型。这些是用于会话导航、重命名与元数据内容的展示原语；宿主保留会话数据、路由、菜单、权限、格式化与交互策略。使用宿主主题导入共享组件样式表。

`AgentSessionRow` 拥有行几何、选中/悬停外观、时间戳位置，以及 pending/provisioning/running/unread 状态点的优先级。宿主提供已格式化的 `label` 与 `timestamp`、可选的 `leading` 与 `pinnedIndicator` 内容，以及同级的 `action`。根 DOM props 与 refs 透传到行包装器，用于宿主拥有的上下文菜单或悬停卡片触发器。

默认控件是桌面原生 `button`；其 `disabled` 与 `onSelect` props 仅在该模式下生效。URL 或路由导航使用互斥的 `renderControl` 模式，它接收共享的导航 `className` 与行 `children`，供宿主的链接或路由控件使用。宿主控件拥有其 href、可访问性、禁用行为与事件处理。控件联合类型防止将 `renderControl` 与 `disabled` 或 `onSelect` 组合，并保持可选操作作为同级，避免交互元素嵌套。

`AgentSessionRowEditor` 提供匹配的编辑框，而宿主拥有重命名输入框、焦点、Enter/Escape/blur 处理与保存状态。`AgentSessionOverview` 渲染标题与 `[label, value, fullValue?]` 元数据行；宿主拥有悬停卡片生命周期、定位与元数据格式化。导入、插槽与触发器/ref 示例见[会话行采用指南](./packages/ui/ADOPTION.md#session-rows)。

## 共享上下文用量展示（`@cline/ui`）

`AgentContextUsage` 通过其 `children` 渲染回调暴露桌面的上下文环与 token 明细。它不添加任何包装：宿主接收 `AgentContextUsagePresentation`（`triggerLabel`、`ring` 与 `details`），并保留自己的可访问触发器、弹出层、定位、焦点与键盘行为。

`AgentContextUsageProps` 接受 `usage: AgentContextUsageData`、可选的 `costLabel: ReactNode` 以及必需的渲染回调。Usage 包含 `tokensIn`、`tokensOut`、`cacheReadTokens` 与可选的 `contextWindow`。请提供当前请求的指标与模型的权威上下文容量，而不是累计的会话 token 流量。当 usage 为空或上下文容量不可用或非正时，组件不渲染任何内容。缓存 token 属于输入用量的一部分，而不是额外的上下文消耗。

`costLabel` 是单独由宿主格式化的成本。数值零会显示；先前隐藏的 falsy 值保持省略。桌面保留其现有的成本格式化器与用量来源。从 `@cline/ui` 导入该组件与全部三个公共类型；主题设置、样式与组合示例见 [UI 采用指南](packages/ui/ADOPTION.md)。

## 共享命令输出与图像展示（`@cline/ui`）

`AgentCommandOutput` 渲染 `output`，并由 `isRunning` 控制运行中的光标。可选的 `children` 让宿主保留 ANSI 渲染或规范化控制字符。它初始跟随新输出，用户滚动离开时暂停，并在距离底部 24px 内恢复。宿主拥有输出收集、限制与会话标识；切换命令时重新挂载它。`tabIndex` 与 `classNames.viewport` / `classNames.cursor` 允许宿主特定的可访问性样式。

`AgentImageLightboxContent` 渲染一张图像与两个调用 `onClose` 的关闭控件。宿主拥有对话框、定位、Escape 处理、焦点管理与图像导航。`backdropTabIndex` 可将背景从托管对话框的 Tab 顺序中排除。图像源验证与解析仍归宿主所有；提供商生成的 URL 在渲染前必须经过显式的宿主信任策略。此展示原语不替代 `GeneratedMediaContent` 或其内联字节验证。