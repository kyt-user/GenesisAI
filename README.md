# GenesisAI

GenesisAI 是面向 CLI 用户的单 Agent 开发助手。它在一个持续 Session 中完成代码开发、文件管理、公开网络研究、Skills 和基础办公文档处理，并通过 `agent_docs` 维护项目工作记忆。项目不使用数据库或向量库。

本项目处于开发阶段，以当前实现和最新决定为准；不维护旧接口和历史会话格式的兼容或迁移。开发约定见 [AGENTS.md](AGENTS.md)。

## 安装

需要 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,office]"
```

基础安装不包含办公库：

```powershell
.venv\Scripts\python.exe -m pip install -e .
```

## 模型配置

网络确认时，输入 `/approve` 或“确认”只批准当前请求；`/allow network run` 允许本轮后续网络请求，`/allow network` 允许本会话后续请求。使用 `/permissions network ask` 撤销预授权；`/new` 恢复逐次确认。等待确认不消耗执行时间，但过期的确认需要重新预览。自然追问缺少资料时会补查，无须另说“执行搜索”。

内部异常会显示诊断编号，可用 `/trace` 查看脱敏堆栈位置。部分结果显示“部分完成”，不会因已取得官网页面而自动标记完成。

`config/model.yaml` 只保存用户需要修改的四项信息：

```yaml
provider: deepseek
model: deepseek-v4-flash
generation:
  temperature: 0.6
  max_tokens: 2048
```

当前只维护 OpenAI 兼容模型协议。Provider 的端点、环境变量名、超时和兼容差异由 `src/genesisai/model/` 中的代码固定。DeepSeek 密钥写入工作区根目录或 GenesisAI 根目录的 `.env`：

```dotenv
DEEPSEEK_API_KEY=replace_me
```

程序不会显示密钥，也不会读取 xiaoerAI 的环境文件。

模型 API Key 的优先级为：**进程环境变量 > 工作区 `.env` > GenesisAI 根目录 `.env` > 终端保存的 Key**（空值和纯空格视为未配置）。`--env-file` 指定文件时替代两个自动查找的 `.env`。按当前选定的提供商读取 `DEEPSEEK_API_KEY` 或 `DASHSCOPE_API_KEY`。

环境和终端配置都没有可用 Key 时，交互式启动才提示输入；有环境 Key 时首次启动也不要求终端配置。`--auth` 或 `/auth` 仍可选择提供商和模型，但有对应环境 Key 时跳过密钥输入，且不将环境 Key 复制到 `~/.genesisai/config.json`。原有终端 Key 保留作回退。修改 `.env` 后重启程序生效；配置界面会显示实际使用的密钥来源。

## 启动

以下三种入口等价：

```powershell
python cli.py
python -m genesisai
genesis
```

默认工作区是 `~/GenesisAI/workspace`（Windows 为 `%USERPROFILE%\GenesisAI\workspace`），首次启动自动创建，不随应用安装目录或启动位置变化。交互式和 `--prompt` 模式都可直接使用默认值；使用 `--workspace "E:\my-project"` 指定其他任务目录，不存在时自动创建。工作区确定后才进行远程数据外发确认，并可初始化 `agent_docs/` 项目工作记忆；非交互模式使用 `--yes-agent-docs` 初始化或 `--no-agent-docs` 跳过。

选择结果会作为可信运行时状态直接提供给 Agent。输入目录和输出目录都不得越过当前任务工作区；普通相对输出写入该工作区根目录。默认工作区同时是应用历史的统一存放地：所有项目的会话、完整输入输出日志、Trace、命令日志、来源缓存及备份均写入 `~/GenesisAI/workspace/.genesis/`。会话保存原工作目录，启动界面分别显示任务与历史路径。指定其他工作区不会改变历史存储位置。

`--session` 只从统一历史目录读取当前格式的会话，不转换历史格式。清理已有用户数据前必须确认范围。模型终端配置保存于 `~/.genesisai/config.json`（可通过 `--config-dir` 指定）。

网络、工作区写入和 Shell 默认分别询问，可在当前 Session 中授权：

```text
/allow network
/allow writes
/allow shell
```

也可以在启动时使用 `--yes-search`、`--yes-writes`、`--yes-shell`。Session 授权会被保存；删除、覆盖、越界和 Git 写操作仍受固定边界约束。

## 连续会话

完整输入输出日志统一写入 `~/GenesisAI/workspace/.genesis/logs/<session_id>.jsonl`，可用 `/logs` 查看路径。记录完整模型请求与响应、工具参数与结果、确认、重试及错误；认证信息脱敏。不再生成重复的截断摘要 `.log`。详见 [日志与可用性检查](docs/日志与可用性检查.md)。

CLI 未退出且未输入 `/new` 时，普通输入均属于同一 Session。上一轮用户问题、回答、已读取来源和已加载 Skills 会保留。长会话会压缩早期内容，但保留最近一轮、目标、来源、变更和待办。重启后用 `--session SESSION_ID` 恢复，自动使用会话原工作目录；如同时提供 `--workspace`，必须与原目录一致。原目录已不存在时报告错误，不自动重新创建。

## 主要命令

| 命令                                                    | 用途                                     |
| ------------------------------------------------------- | ---------------------------------------- |
| `/help`、`/status`、`/history`、`/model`        | 查看使用方法和当前状态                   |
| `/`、`/sessions`、`/new`、`/resume`、`/cancel`、`/exit` | 选择历史会话、创建会话或管理当前运行 |
| `/tools`                                              | 查看启用与禁用的工具                     |
| `/skills list\|search\|show\|load\|unload`                | 管理按需 Skill                           |
| `/logs`                                               | 查看当前会话完整 JSONL 日志路径          |
| `/project`、`/project init`                         | 查看或初始化 `agent_docs` 项目工作记忆 |
| `/permissions`、`/allow`、`/approve`、`/reject` | 查看或处理权限                           |
| `/budget`、`/trace`、`/compact`                   | 查看预算、脱敏执行链和压缩上下文         |
| `/diff` | 查看本次文件变更记录 |
| `/details` | 展开或收起工具输出 |
| `/workflow [名称]` | 列出工作区工作流，或展开指定工作流并执行 |
| `/auth` | 配置提供商、密钥与模型 |

交互终端支持输入 `/` 实时选择命令、Tab 补全、Enter 发送、Alt+Enter 换行。`/help` 打开命令选择器，`/sessions` 选择历史会话，操作确认使用方向键和回车。

## 架构约束

- 模型客户端只由一个 `Runner` 驱动，不创建子 Agent。
- 模型请求携带所有已启用且满足平台和依赖条件的工具定义；Skills 按需加载。
- Prompt、Tool 和 Skill 使用严格 YAML 必要字段，均无版本字段。
- Session、Run、Trace、日志与备份统一保存在默认工作区的 `.genesis/`；项目 Skill 和 Workflow 仍从任务工作区的 `.genesis/` 读取。
- `agent_docs/` 保存项目策划、实施计划、进度、决策、已验证命令、验收与交接；代码、配置、测试和 Git 始终优先。
- Git V1 只读；Shell 直接执行参数数组，不经过命令解释器。

## Python 小项目开发循环

代码任务按“策划 → 计划 → 执行 → 验收 → 修正”循环运行。Python 项目会识别 `pyproject.toml`、源码布局、项目虚拟环境和测试框架；`run_commands` 可自动选择 pytest、unittest，或在没有测试时执行语法编译检查。文件发生修改后，即使随后读取 Git diff，Runner 仍保留待验证状态并在交付前运行测试。只有最近一次代码修改之后的真实验证通过，`agent_docs` 任务才能标记为 `verified`。

从零创建 `.py`、`.java`、`.html` 等源码文件以及开发任务短回复延续的补全方案已纳入主线。

`agent_docs/` 默认适合纳入 Git，目录只保存人类可审查的项目状态；会话、模型中间状态、完整命令日志与备份保存在默认工作区的 `.genesis/`，不进入项目文档。

## 文件能力

代码与常用文本支持读取、搜索、创建、哈希保护补丁、移动和删除，并保持可识别的编码、BOM 与换行。DOCX/XLSX 支持读取、创建和有限修改；PPTX/PDF 支持读取和基础创建。办公能力需要 `.[office]`，公式不会在 GenesisAI 中重新计算，任意原稿的高保真编辑不在 V1 范围内。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m genesisai.app.evals.simulation --mode offline
.venv\Scripts\python.exe -m genesisai.app.evals.python_development
.venv\Scripts\python.exe -m genesisai.app.evals.simulation --mode deepseek
```

Python 开发仿真使用 `agent_tests_workspace/` 作为只读源工作区，数据清单位于 `tests/python_development_dataset/`，覆盖完整开发循环、中断恢复与源码漂移、失败验收门禁。通用仿真数据位于 `tests/product_acceptance_dataset/`。真实模式会调用已配置的模型，其中网络研究案例会访问公开网络。

源码创建、短回复延续、完成门禁和终端安全的离线仿真可运行：

```powershell
.\.venv\Scripts\python.exe -m genesisai.app.evals.source_development
```

该套件覆盖 HTML 创建与静态验收、Python 创建与测试、Java 验证受限状态以及二进制扩展名拒绝，不调用远程模型。

详细用法见 [用户指南](docs/用户指南.md)，总体设计见 [GenesisAI 升级路线](docs/GenesisAI升级路线.md)，已复现问题及修复记录见 [问题及解决清单目录](docs/问题及解决清单/目录.md)。
