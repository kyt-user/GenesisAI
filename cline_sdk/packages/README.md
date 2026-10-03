# 包概览

本目录是包级职责的唯一文档来源。

- 高层包角色：本文件（`packages/README.md`）
- 包之间的交互与运行时流程：[`ARCHITECTURE.md`](/sdk/ARCHITECTURE.md)

## 包职责

| 包 | 主要职责 | 典型消费方 | 内部依赖 |
| --- | --- | --- | --- |
| `@cline/shared` | 跨包共享原语（路径解析、会话通用类型、索引辅助函数） | `@cline/agents`、`@cline/core`、应用 | 无 |
| `@cline/llms` | 模型目录 + 提供商设置 schema + handler 创建 SDK | `@cline/agents`、`@cline/core`、应用 | 无 |
| `@cline/agents` | 无状态 agent 运行时循环（工具、hook、扩展、团队、流式输出） | `@cline/core`、应用 | `@cline/llms`、`@cline/shared` |
| `@cline/core` | 有状态运行时编排（运行时组合、会话生命周期/存储、本地与 hub 运行时服务、hub 发现与客户端辅助函数） | CLI/桌面应用 | `@cline/agents`、`@cline/llms`、`@cline/shared` |
| `@cline/ui` | 内部框架无关的 Web 主题、Tailwind 适配器与可选基础样式 | Cline Web 应用 | 无 |

## 包如何协同工作

1. `@cline/llms` 定义模型/提供商能力并构建具体 handler。
2. `@cline/agents` 在这些 handler 与工具执行原语之上运行 agent 循环。
3. `@cline/core` 将运行时行为与持久化会话/存储以及本地或 hub 背书的运行时服务组合在一起。
4. `@cline/core` 的 hub 服务编排定时运行时执行、执行历史与计划命令处理。
5. 当宿主需要共享守护进程时，`@cline/core/hub` 暴露发现、分离的 hub 守护进程与面向会话的客户端 API（`HubSessionClient`、`HubUIClient`）。
6. `@cline/shared` 提供整个栈使用的共享契约与路径/会话原语。

## 实用的边界规则

- 将提供商/模型 schema、目录化与 handler 接线放在 `@cline/llms`。
- 将循环/工具/hook/团队执行行为放在 `@cline/agents`。
- 将持久化、会话生命周期与运行时装配放在 `@cline/core`。
- 将定时执行与计划持久化放在 `@cline/core` 的 hub 服务。
- 将 hub 发现、attach 流程与面向会话的客户端适配器放在 `@cline/core/hub`。
- 将跨包工具类型与路径/会话常量放在 `@cline/shared`。
- 将 remote-config schema、物化、遥测规范化与 blob 上传原语放在 `@cline/shared/remote-config`。
- 将共享 Web token 与视觉基础放在 `@cline/ui`；字体、外壳布局与产品特定的动画保留在各消费应用。

## 运行时入口点

- 在包暴露独立 Node 别名的地方，存在面向 Node 的导入。
- `@cline/core` 本身现在就是面向 Node/运行时的宿主/会话服务入口点。
- 有意发布浏览器表面的包仍保留浏览器入口点，但 `@cline/core` 已不再如此。

## 文档整合备注

在引用更新为指向此处后，嵌套包的 `README.md` 与 `ARCHITECTURE.md` 文件可以减少或移除。