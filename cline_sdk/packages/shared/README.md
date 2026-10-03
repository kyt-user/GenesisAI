# [experimental] @cline/shared

包级文档已集中放置：

- 总览：[`packages/README.md`](../README.md)
- 架构与交互：[`ARCHITECTURE.md`](./ARCHITECTURE.md)

`@cline/shared` 负责跨包共享的原语（会话通用类型/工具）。

仅限 Node 的文件系统路径解析器位于 storage 子路径导出下：

- `@cline/shared/storage`
- 示例：`resolveClineDataDir`、`resolveDbDataDir`、`resolveSessionDataDir`、`resolveTeamDataDir`

它还导出跨客户端日志契约（包括 `BasicLogger`），使运行时、SDK 与宿主应用可以共享同一个 logger 类型。

会话配置原语也集中在此，以便宿主/运行时组合出一个统一的基础形状，而不必反复重定义相似字段：

- `AgentMode`
- `SessionPromptConfig`
- `SessionWorkspaceConfig`
- `SessionExecutionConfig`（包含规范化的 `ToolPolicy` map 形状）

现在还导出跨 agents/core/CLI 使用的 Hook 会话上下文原语：

- `HookSessionContext`
- `resolveHookSessionContext(...)`
- `resolveRootSessionId(...)`
- `resolveHookLogPath(...)`

它还导出被多个宿主（`@cline/cli`、`@cline/code`）使用的跨客户端运行时载荷 DTO，避免请求/响应契约在传输层之外被重复定义：

- 聊天运行时载荷（`ChatStartSessionRequest`、`ChatRunTurnRequest`、`ChatTurnResult`）
- 提供商运行时载荷（`ProviderActionRequest`、`ProviderCatalogResponse`、`ProviderOAuthLoginResponse`）
- Cline 账号操作载荷（`ClineAccountActionRequest`）
- 提供商操作请求包含提供商目录/模型操作，以及面向设置宿主的「提供商添加/保存」操作
- 提供商操作载荷现在提供更细粒度的请求/类型契约以供复用：`AddProviderActionRequest`、`SaveProviderSettingsActionRequest`、`ProviderCapability` 与 `OAuthProviderId`

聊天运行时载荷说明：
- `ChatStartSessionRequest` 支持 `initialMessages`、可选的 `toolPolicies`、用于默认系统提示词装配的可选 `rules`，以及可选的 `logger` 运行时配置（`RuntimeLoggerConfig`），因此宿主可以跨传输边界传递序列化的 logger 设置。
- `RuntimeLoggerConfig.bindings` 允许宿主为所有运行时日志记录附加稳定的上下文字段（例如 `clientId`、`clientType`、`clientApp`）。
