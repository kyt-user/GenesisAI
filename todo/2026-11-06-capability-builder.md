# 内置与外挂能力构建

日期：2026-11-06。基线提交：`8312d27`。工作分支：`codex/capability-builder`。

## 已确认的产品要求

- 一级按来源区分内置、外挂，二级按 Tool、Skill、MCP 区分；MCP 工具保留所属服务。
- 内置底座提供 `create_tool`、`create_skill`、`create_mcp`，用户可以自己构建，也可以让 Agent 构建。
- 创建先产生草稿；草稿可修改、测试和试用，正式注册前不进入常规能力目录。
- 注册必须同时具备该版本的通过报告和用户明确验收；自动权限模式也不能代替用户验收。
- 测试、验收、注册绑定内容摘要和修订号；修改后重新测试与验收。
- Tool 做实际执行测试；Skill 用任务试运行并检查产物；MCP 做发现及实际调用测试。
- 用户可以查看、导出、导入草稿；个人和项目作用域明确，重启后可复用注册能力。
- 替换旧接口时同步清理调用方、测试和文档，不新增旧格式迁移或兼容回退。
- 不删除、覆盖用户现有能力配置、历史会话和数据。

## 实施与验收

| 项目 | 状态 | 验收证据要求 |
| --- | --- | --- |
| 提交原工作区基线 | 已完成 | `8312d27`，73 个文件，提交后工作区干净 |
| 统一来源、类型、作用域和提供方 | 已完成 | ToolDescriptor、SkillDescriptor 与能力包区分 builtin/external、kind、scope、provider；`/tools` 与 `/capabilities` 展示；目录及 CLI 测试通过 |
| 草稿及不可变修订持久化 | 已完成 | 独立 SQLite 库及事务；生命周期测试覆盖创建、期望修订冲突、恢复、包路径检查、执行包篡改后退出目录 |
| 三类创建入口与 Agent 构建指引 | 已完成 | 3 个创建工具和同名 CLI 命令、capability_builder 路由／协议；测试覆盖先讨论后构建、完整 Agent 循环及终端实际入口 |
| 实际测试和试用报告 | 已完成 | Tool 真实 Python 子进程、Skill 独立 Runner 与实际文件、MCP 子进程握手／发现／调用及图片；失败修正、真实超时、独立试用通过 |
| 用户验收与注册 | 已完成 | 无测试及失败报告拒绝；auto 模式仍等待；直接调用注册服务无验收上下文被拒绝；修订或报告变化使审批失效；拒绝不注册 |
| 注册后调用及跨会话复用 | 已完成 | Tool、Skill、MCP 恢复调用通过；个人 Tool 可在另一项目执行、项目 Tool 不泄漏；新草稿不替换旧验收版本；停用撤下工具并关闭 MCP |
| 替换旧加载路径 | 已完成 | 删除 MCP 配置自动扫描和 Skill 目录覆盖；桥接接受明确服务定义；仿真夹具采用 JSON 包及实际测试验收；旧用户文件不被改写 |
| 文档与验证 | 已完成 | README、用户指南、架构文档及相关路线／规格同步；验证命令、范围与限制如下 |

## 验证记录

- `.venv/Scripts/python.exe -m pytest -q`：**600 passed, 1 skipped**，440.21 秒。覆盖当时全部 601 个用例，包括原有配置、恢复、权限、开发循环和首批 17 项能力测试。
- 最后补充了 MCP 图片、Skill/MCP 独立连接、桥接命名空间、跨作用域冲突、讨论转构建路由和 CLI 完整入口测试。图片用例复现 Windows 路径长度失败，原因是冗余能力编号目录使图片路径达到 266 字符；测试／试用改为按全局唯一运行编号直接建目录，保留报告中的能力引用，不迁移既有目录。
- `.venv/Scripts/python.exe -m pytest -q tests/test_capability_lifecycle.py tests/test_cli_ui.py tests/test_routing_budget_repair.py tests/test_product_simulation.py`：**68 passed**，86.07 秒，验证上述修复及相关调用方。
- 之后补强 Skill/MCP 恢复断言并新增个人作用域跨项目执行用例；最终 `.venv/Scripts/python.exe -m pytest -q tests/test_capability_lifecycle.py`：**25 passed**，49.89 秒。
- 当前收集 **611 个用例**。全量及随后针对变更的回归合计覆盖 610 个通过用例；唯一跳过是宿主不允许创建符号链接，Windows junction 路径另有已通过覆盖。未把增量结果冒充一次新的全量执行。
- `.venv/Scripts/lint-imports.exe`：237 个文件、409 条依赖，**2 contracts kept, 0 broken**。
- `git diff --check` 通过；基线仍为 `8312d27`，本轮代码保留在 `codex/capability-builder` 工作区供审阅，未推送。

## 验收入口与关键文件

- `src/genesisai/core/capabilities/`：纯数据包契约、草稿持久化、实际运行、报告与验收、注册投影。
- `src/genesisai/app/capability_commands.py`：查看、导入、修订、测试、试用、注册、导出、停用。
- `tests/test_capability_lifecycle.py`：三类端到端生命周期、恢复、版本及报告绑定、作用域、隔离和失败边界。
- `tests/test_cli_ui.py`：不需要模型的能力管理与错误处理，以及自动模式下仍等待用户批准的构建链路。
- `tests/product_acceptance_dataset/case_04_skill/capability.json`：当前协议的 Skill 示例；产品仿真通过真实测试和显式验收后使用。
- `docs/用户指南.md` 第 5 节与 `docs/记忆系统架构.md`：操作流程、目录与接入契约。

## 实现约束

应用维护能力库；生成程序在独立进程中执行，测试使用专门的工作目录和固定夹具，不以真实项目目录作默认测试目录。进程与目录隔离不宣称操作系统级安全沙盒。用户生成程序不能获得宿主 Session 或注册服务对象。用户验收由宿主执行层确认，模型参数中的“已同意”字段没有授权作用。

已有工具权限、ENV 优先及终端密钥回退、会话恢复、调用账本、未知结果不重放继续适用。现有用户配置不自动迁移；源码和验收夹具同步采用新接口。

本轮验证使用受控模型响应驱动真实 Runner 和文件工具，MCP 使用真实本地 stdio 服务；未访问线上 aihot 或计费模型 API。实际 Skill 测试入口会调用当前配置模型。包内容和接受记录被固定，宿主依赖版本与远端服务行为不冻结。测试覆盖及用户验收不等同于对任意生成程序的安全证明。

原用户 `~/.genesisai` 中的配置、历史与日志均未删除或转换。旧 aihot 配置保留，当前代码不自动加载旧 `mcp.json`；接入须使用当前能力包协议完成测试和验收。`E:/PROJECT/clinesdkPY` 未修改。
