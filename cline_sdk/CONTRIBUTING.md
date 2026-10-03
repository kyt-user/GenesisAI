# 为 Cline SDK 做贡献

本文档涵盖入门、开发工作流与发布。开发过程中的包边界与变更归属见 [AGENTS.md](./AGENTS.md)。架构与运行时流程见 [ARCHITECTURE.md](./ARCHITECTURE.md)。

本仓库是一个用于构建与编排 AI 智能体的 WIP（进行中）框架。当重构能改进架构、且所有调用点均已更新时，全面的重构是可以接受的。

## 工作区概览

### 已发布的 SDK 包

| 包 | 职责 |
|---------|------|
| `@cline/shared` | 契约、schema、路径辅助函数、hook 引擎、扩展注册表 |
| `@cline/llms` | 提供商设置、模型目录、清单、handler 创建 |
| `@cline/agents` | 无状态 agent 循环、工具编排、hook/扩展运行时 |
| `@cline/core` | 有状态编排、会话生命周期、存储、配置、遥测、hub 运行时服务、hub 发现、分离守护进程，以及 hub 客户端适配器（`@cline/core/hub`、`@cline/core/hub/daemon-entry`） |

### 应用

- `apps/cli`：CLI 宿主与本地 hub 管理
- `apps/examples/desktop-app`：Tauri + Next.js 桌面应用示例
- `apps/examples/vscode`：VS Code 扩展示例
- `apps/examples/menubar`：hub 通知菜单栏示例
- `examples`：插件、hook 与 cron 自动化示例（基于 Cline SDK 的定制）

## 开发工作流

### 常用命令

| 命令 | 用途 |
|---------|---------|
| `bun install` | 安装依赖 |
| `bun run build` | 构建 SDK 与 CLI |
| `bun run build:sdk` | 仅构建 SDK 包 |
| `bun run dev` | 以开发模式构建 |
| `bun run cli` | 交互式运行 CLI |
| `bun run test` | 运行 Vitest 测试套件 |
| `bun run types` | 类型检查所有包 |
| `bun run lint` / `format` / `fix` | 代码质量与格式化 |

包级命令：

```sh
bun -F @cline/core build|test|typecheck
bun -F @cline/agents build|test|typecheck
```

### 重新构建

对已发布 SDK 包的更改需要运行 `bun run build:sdk`。直接运行 CLI 会立即使用重新构建的包。开发期间可使用 `dev:*` 脚本进行自动重新构建。

CLI 构建（`bun -F @cline/cli build`）从各包编译后的 `dist/` 打包，而不是其 TypeScript 源码。如果你编辑了某个包，却没有先重新构建该包就构建 CLI，CLI 二进制会静默包含旧的包代码。端到端测试更改时，务必在构建 CLI 前运行 `bun run build:sdk`（或相关的 `bun -F @cline/<pkg> build`）。

Hub 背书的宿主使用共享工作区发现与归属感明确的守护进程启动逻辑。如果你改动了 hub bootstrap，请保留启动锁与所有者作用域的发现行为，使多个构建可以安全共存。

### 调试构建

- 设置 `CLINE_BUILD_ENV=development` 启用调试构建。派生的 Node/Bun 子进程会获得一个 inspector 端点以及 `--enable-source-maps`。
- 默认情况下，子进程 inspector 端口为临时端口（`--inspect=127.0.0.1:0`），以避免并行开发运行之间的冲突。
- 设置 `CLINE_DEBUG_HOST` 与 `CLINE_DEBUG_PORT_BASE` 可选择启用确定性的、按角色分配的端口。当 `CLINE_DEBUG_PORT_BASE=9230` 时，各角色映射为 hub `9230`、hook worker `9231`、plugin sandbox `9232`、connector child `9233`、fallback sandbox `9234`。
- 回退链：`CLINE_BUILD_ENV` → `NODE_ENV` → Bun `--conditions=development`。
- 调试 CLI 进程本身：`cd apps/cli && CLINE_BUILD_ENV=development bun --conditions=development --inspect-brk=6499 ./src/index.ts "hey"`。
- 工作区包含一个 VS Code 启动配置（`Launch CLI Debugger`），它使用 `"type": "bun"`（需要 `oven.bun-vscode`）。

### 测试

跨包信心的根命令：

```sh
bun run test        # 所有测试
bun run types       # 类型检查所有包
bun run check       # lint + 构建 + 类型检查 + check-publish
```

如果你改动了 hub/bootstrap/session 流程，建议同时添加单元覆盖与端到端冒烟检查。

## 发布

### SDK 发布

`bun release sdk` 脚本自动化 SDK 发布流程：版本升级、lockfile 重新生成、验证与发布。

```sh
bun release sdk              # 自动递增 patch 版本
bun release sdk 0.1.0        # 显式指定版本
bun release sdk --tag next   # 使用自定义 npm dist-tag 发布
bun release sdk --dry-run    # 预览而不产生副作用
```

额外的 SDK 标志：`--skip-tests`、`--skip-git-tags`。

脚本在开始前会检出 `main`（并拉取最新代码）。如果工作树是脏的，它会中止。

SDK 流程依次运行：测试 → 版本升级 → lockfile 重新生成 → tarball 验证 → 发布（shared → llms → agents → core）→ 可选的 `sdk-v{VERSION}` 标签创建。

### CLI 发布

CLI 通过 npm 发布。在 `apps/cli` 中使用 `publish-cli` skill 发起发布。该 skill 应引导发布准备工作，然后提供 GitHub Actions 发布路径与本地发布路径。

在底层，每次发布都以相同方式开始：准备一个发布提交，然后选择发布方式。

从你想要发布的代码准备发布提交：

1. 根据自上个 `cli-vX.Y.Z` 标签以来的提交，起草面向用户的发布说明。
2. 选择发布版本。
3. 更新 `apps/cli/package.json`。
4. 将已批准的发布说明添加到 `apps/cli/CHANGELOG.md`。
5. 运行要求的检查。
6. 提交版本与更新日志变更。

然后用以下路径之一发布该发布提交。

路径 A：从 GitHub Actions 发布。

用于常规发布。将发布提交合并到 `main`，创建并推送对应的发布标签，然后运行：

```sh
git tag -a cli-vX.Y.Z -m "CLI vX.Y.Z"
git push origin refs/tags/cli-vX.Y.Z
gh workflow run cli-publish.yml -f publish_target=main -f git_tag=cli-vX.Y.Z -f confirm_publish=publish
```

该工作流会检出所提供的 `cli-vX.Y.Z` 标签，验证它与 `apps/cli/package.json` 匹配，构建各平台包，使用 `latest` dist-tag 发布到 npm，创建 GitHub release，并向 Slack 发送通知。

路径 B：本地发布。

当你从已认证的本地机器发布时使用。从发布提交处的干净检出开始：

```sh
gh auth status
npm whoami
git tag -a cli-vX.Y.Z -m "CLI vX.Y.Z"
git push origin refs/tags/cli-vX.Y.Z
bun release cli
gh release create cli-vX.Y.Z --verify-tag --title "CLI vX.Y.Z" --notes "Paste the approved release notes here."
```

本地辅助脚本会验证工作树干净、验证 `cli-vX.Y.Z` 在本地与 `origin` 上都指向 `HEAD`、运行测试、构建各平台包，并发布到 npm。

Nightly 发布：

```sh
gh workflow run cli-publish.yml -f publish_target=nightly
```

Nightly 也会按计划运行。它会将 `X.Y.Z-nightly.TIMESTAMP` 以 `nightly` dist-tag 发布到 npm；如果过去 24 小时内没有提交则跳过，除非强制运行。

### 手动 SDK 发布

如果你需要对各个步骤进行细粒度控制：

1. `bun run test`
2. `bun version <version>` —— 更新所有工作区包版本、重新生成模型、格式化并构建。
3. `rm bun.lock && bun install --lockfile-only` —— 重新生成 lockfile，使 `bun pm pack` 将 `workspace:*` 解析为新版本。
4. `bun scripts/check-publish.ts` —— 打包 tarball、验证依赖对齐、测试隔离安装与模块解析。
5. `npm login` —— 确保你已通过 npm registry 认证。
6. 按依赖顺序发布：
   ```sh
   cd packages/shared && bun publish && cd ../llms && bun publish && cd ../agents && bun publish && cd ../core && bun publish && cd ../../
   ```
7. 对于带标签的生产发布，创建并推送 git 标签：`git tag -a sdk-v{VERSION} -m "SDK v{VERSION}" && git push origin sdk-v{VERSION}`。

### 工作区依赖规则

- 源码清单使用 `workspace:*`，以便 `bun install` 与本地构建正确解析。
- 已发布的运行时工作区包保留在 `dependencies` 中。被打包的内部包放在 `devDependencies`，以免泄漏到打包后的清单中。
- `bun publish` 在打包时将 `workspace:*` 解析为具体版本。

### 验证单个包

检查将要发布的确切清单：

```sh
cd ./packages/core
tmpdir=$(mktemp -d)
bun pm pack --destination "$tmpdir" >/dev/null
tar -xOf "$tmpdir"/*.tgz package/package.json | jq '.version, .dependencies'
```

在消费方项目中检查已安装版本：

```sh
bun pm ls @cline/core @cline/agents @cline/llms
```

### CI

CI 发布工作流（`.github/workflows/sdk-publish.yml`）遵循相同顺序：构建 → 版本 → check-publish → 发布（shared → llms → agents → core）。它支持 `nightly` 与 `latest` 渠道，由手动触发或每日 cron 触发。

### 根自动化范围

根脚本有意比完整工作区更窄：

- 根 SDK 的构建/测试/版本/发布流程仅针对可发布的 SDK 包。
- 内部包仍可直接构建/测试，但不应被意外卷入发布自动化。
- 如果你新增内部包，请让它避开根发布/版本/构建清扫，除非你明确打算发布它。