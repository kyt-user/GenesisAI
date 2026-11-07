# GenesisAI

GenesisAI 是面向 CLI 用户的单 Agent 开发助手。它在一个持续 Session 中完成代码开发、文件管理、公开网络研究、Skills 和基础办公文档处理，并通过应用管理的 SQLite 历史库、任务状态与项目知识维持连续工作。无需外部数据库服务。

本项目处于开发阶段，以当前实现和最新决定为准；不维护旧接口和历史会话格式的兼容或迁移。开发约定见 [AGENTS.md](AGENTS.md)。

## 使用与商业授权

GenesisAI 采用 **Apache License 2.0 附 Commons Clause 1.0**，以源码可用方式发布。个人和企业可以用它开发自己的产品，包括收费产品。如果你向第三方收取费用或其他对价，而所提供的产品或服务的价值全部或主要来自 GenesisAI 的功能，请先向授权方 `kyt-user` 取得单独商业授权。完整条款见 [LICENSE](LICENSE)，常见场景见[商业使用说明](docs/商业使用说明.md)。

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

### Windows 桌宠

在仓库根目录安装桌宠依赖并启动 Genesis。

```powershell
.venv\Scripts\python.exe -m pip install -e ".[pet]"
.venv\Scripts\genesis.exe
```

在 Genesis 输入 `/pet`，从菜单选择黄色毛球或雷伊，再次输入可切换角色。取消菜单会保留当前角色。雷伊使用原版图集，待机眨眼，拖拽或附近鼠标触发左右飞行，思考使用第七行。毛球的行为如下。左键拖动时随方向左右跳，右键关闭窗口，再次输入 `/pet` 即可重新打开。待机时轻跳、左右看，鼠标进入约三个桌宠宽的半径内时视线跟随鼠标。执行任务时敲电脑，失败时拿喇叭提醒；等待回答时戴耳麦，等待确认或部分完成时拿放大镜，完成后短暂展示终端牌。状态标签提示等待回答、等待确认和完成，批准或拒绝操作仍在终端输入。退出 CLI 时桌宠自动关闭。

## 模型配置

等待审批时，输入 /approve 或“确认”只批准当前调用。等待时间不计入执行预算，过期后重新展示操作。自然追问缺少资料时会补查，无须另说“执行搜索”。

内部异常会显示诊断编号，可用 `/trace` 查看脱敏堆栈位置。部分结果显示“部分完成”，不会因已取得官网页面而自动标记完成。

默认模型配置随包安装。要自定义模型，可复制[默认配置](src/genesisai/model/default.yaml)，修改后通过 `genesis --config <文件路径>` 加载。例如：

```yaml
provider: deepseek
model: deepseek-v4-flash
generation:
  temperature: 0.6
  max_tokens: 8192
```

当前只维护 OpenAI 兼容模型协议。Provider 的端点、环境变量名、超时和兼容差异由 `src/genesisai/model/` 中的代码固定。DeepSeek 密钥写入工作区根目录或 GenesisAI 根目录的 `.env`：

```dotenv
DEEPSEEK_API_KEY=replace_me
```

程序不会显示密钥，也不会读取 xiaoerAI 的环境文件。

模型 API Key 的优先级为 **进程环境变量 > 工作区 `.env` > GenesisAI 根目录 `.env` > 终端保存的 Key**（空值和纯空格视为未配置）。`--env-file` 指定文件时替代两个自动查找的 `.env`。按当前选定的提供商读取 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY` 或 `QWEN_TOKEN_PLAN`。

阿里云 Token Plan 用户在 `.env` 中填写 `QWEN_TOKEN_PLAN=套餐专属密钥`，然后运行 `genesis --auth` 或在会话中输入 `/auth`，选择「阿里云 Token Plan」及模型。默认模型为 `qwen3.8-flash`，支持自定义模型 ID。套餐使用 `https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1` 和 `sk-sp-` 开头的专属密钥。模型 YAML 对应 `provider: qwen_token_plan`。套餐接入与可用模型以[官方文档](https://platform.qianwenai.com/docs/token-plan/personal/token-plan-personal-quickstart)为准。

环境和终端配置都没有可用 Key 时，交互模式首次提交任务时才提示输入；有环境 Key 时首次启动也不要求终端配置。`--auth` 或 `/auth` 仍可选择提供商和模型，但有对应环境 Key 时跳过密钥输入，且不将环境 Key 复制到 `~/.genesisai/config.json`。原有终端 Key 保留作回退。修改 `.env` 后重启程序生效；配置界面会显示实际使用的密钥来源。

## 启动

以下三种入口等价：

```powershell
python cli.py
python -m genesisai
genesis
```

普通启动先进入本地首页。直接输入第一条任务开始新会话，空输入框按 ↑ 选择当前工作区最近的五段对话，或用 `/sessions` 浏览所有工作区的历史。打开后按原顺序显示最近三轮，`/history` 显示当前对话全文。仅浏览历史无需模型密钥，也不会创建空会话。

默认工作区是 `~/.genesisai/workspace`（Windows 为 `%USERPROFILE%\.genesisai\workspace`），首次启动自动创建。设置 `GENESISAI_HOME` 后，默认工作区随应用目录变为 `GENESISAI_HOME/workspace`。交互式和 `--prompt` 模式共用这个默认值，也可以用 `--workspace "E:\my-project"` 指定其他任务目录。提交任务时，所选模型会收到本次请求的会话上下文和相关工具结果，任务记录和工程索引由应用维护。

选择结果会作为可信运行时状态直接提供给 Agent。输入目录和输出目录都不得越过当前任务工作区；普通相对输出写入该工作区根目录。所有项目的历史和附件统一保存于 `~/.genesisai/`（可用 `GENESISAI_HOME` 覆盖）。会话事件与执行状态写入 `state/genesis.sqlite3`，图片写入 `assets/images/`，项目扩展配置位于 `projects/<project_id>/`。会话保存原工作目录，使用 `/status` 查看任务与历史路径。指定其他工作区不会改变历史存储位置。

`--session` 只从统一历史目录读取当前格式的会话，不转换历史格式。清理已有用户数据前必须确认范围。模型终端配置保存于 `~/.genesisai/config.json`（可通过 `--config-dir` 指定）。

新会话默认使用用户审批。读取本地授权文件直接执行。写入、网络请求、运行命令、启动或调用 MCP 服务在有效范围授权内执行，其他调用单独审批。

```text
/permissions
/permissions user
/permissions auto
/authorizations
```

自动审批允许已启用的工具直接执行，内置文件工具可用绝对路径读写工作区外的宿主文件，命令以当前系统账号运行。可用 `--permissions user` 或 `--permissions auto` 指定启动模式。恢复会话时使用该会话保存的模式，`/new` 回到用户审批。

删除操作及外挂能力注册始终需要明确确认；外挂验收绑定当前测试版本。用 `/authorize JSON` 创建会话或当前任务的授权，`/revoke auth_...` 撤销；单次确认不按固定时间过期，但参数或资源变化必须重新确认。命令和外挂环境默认隔离，用 `env_from` 显式映射宿主变量。语法及边界见[范围授权与环境隔离](docs/范围授权与环境隔离.md)。

## 连续会话

完整输入输出日志统一写入 `~/.genesisai/logs/<session_id>.jsonl`，可用 `/logs` 查看路径。记录完整模型请求与响应、工具参数与结果、确认、重试及错误；认证信息脱敏。不再生成重复的截断摘要 `.log`。详见 [日志与可用性检查](docs/日志与可用性检查.md)。

`/new` 回到当前工作区首页，`/workspace 路径` 切换工作区；在首页发送任务才建立新会话。进入某个会话后，普通输入均追加到该会话。上一轮用户问题、回答、已读取来源和已加载 Skills 会保留。长会话会压缩早期内容，但保留最近一轮、目标、来源、变更和待办。重启后用 `--session SESSION_ID` 恢复，自动使用会话原工作目录；如同时提供 `--workspace`，必须与原目录一致。原目录已不存在时报告错误，不自动重新创建。

## 主要命令

| 命令                                                    | 用途                                     |
| ------------------------------------------------------- | ---------------------------------------- |
| `/help`、`/status`、`/history`、`/model`        | 查看使用方法和当前状态                   |
| `/`、`/sessions`、`/new`、`/resume`、`/cancel`、`/exit` | 选择历史会话、创建会话或管理当前运行 |
| `/answer 文字或编号`                                    | 回答当前任务的澄清问题                   |
| `/tools`                                              | 按内置／外挂查看工具及可用状态           |
| `/capabilities` | 查看内置与外挂的 Tool、Skill、MCP，以及草稿和报告 |
| `/create_tool 需求`、`/create_skill 需求`、`/create_mcp 需求` | 让 Agent 构建、测试并提交用户验收 |
| `/queue`、`/queue resume`、`/queue clear` | 查看、继续或清空本次交互的排队消息 |
| `/pet` | 选择或切换毛球、雷伊桌宠，任务运行中也可使用 |
| `/skills list\|search\|show\|load\|unload`                | 管理按需 Skill                           |
| `/logs`                                               | 查看当前会话完整 JSONL 日志路径          |
| `/project`、`/project rebuild`                         | 查看项目任务与知识，重建工程索引 |
| `/permissions`、`/approve`、`/reject` | 查看或处理权限                           |
| `/budget`、`/trace`、`/compact`                   | 查看预算、脱敏执行链和压缩上下文         |
| `/diff` | 查看本次文件变更记录 |
| `/details` | 展开或收起工具输出 |
| `/workflow [名称]` | 列出工作区工作流，或展开指定工作流并执行 |
| `/auth` | 配置提供商、密钥与模型 |

交互终端支持输入 `/` 实时选择命令、Tab 补全、Enter 发送、Alt+Enter 换行。`/help` 打开命令选择器，`/sessions` 选择历史会话，操作确认使用方向键和回车。

## 记忆库与上下文

默认上下文窗口统一为 **400K tokens**；预留输出和安全余量后，默认输入预算为 394880。近期历史目标预算单独设为 32000，较早原文通过 `memory_search` / `memory_read` 按需查阅，图片按引用重新加载。视频未接入。

`/memory` 支持 list、search、read、write、delete、export、rebuild、diagnostic、backup。知识修正使用版本号，删除先预览并显式确认。`/budget` 显示窗口、输入用量与图片预算。详见[记忆系统与扩展接口](docs/记忆系统架构.md)。

## 架构约束

- 主会话使用单个 `Runner`；显式测试 Skill 时，顺序运行独立测试会话，不共享生产工具服务。
- 每轮按任务和已加载 Skill 选择工具定义，模型可通过 `discover_tools` 查找其他可用能力。
- 内置 Prompt、Tool 和 Skill 使用严格 YAML；外挂使用统一 JSON 能力包，能力库管理不可变修订和测试报告。
- 历史、任务、索引、附件、日志与备份由应用维护；外挂 Tool、Skill、MCP 位于应用目录的 `capabilities/`，Workflow 与 LSP 配置位于 `projects/<project_id>/`。
- 项目知识携带来源、状态与版本；源码变化使依赖结论失效，原始历史仍可按引用查阅。用户项目只读取可选的 `AGENTS.md`。
- Git V1 只读；Shell 直接执行参数数组，不经过命令解释器。

## Python 小项目开发循环

代码任务按“策划 → 计划 → 执行 → 验收 → 修正”循环运行。Python 项目会识别 `pyproject.toml`、源码布局、项目虚拟环境和测试框架；`run_commands` 可自动选择 pytest、unittest，或在没有测试时执行语法编译检查。代码变更后，Runner 保留待验收状态，由项目验证器或模型按风险选择测试、构建、静态检查；普通文档可使用内容和差异核对，不会自动强制运行项目全套测试。run_commands 的 verification=build/static 记录显式检查的真实退出状态，要求验证的验收项仍须绑定当前修改版本。

从零创建 `.py`、`.java`、`.html` 等源码文件以及开发任务短回复延续的补全方案已纳入主线。

项目知识可通过 `/memory export 新文件.md` 导出供人工审阅。应用不会把内部状态写入用户仓库。

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
