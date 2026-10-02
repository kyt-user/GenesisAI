# GenesisAI

GenesisAI 是面向 CLI 用户的单 Agent 开发助手。它在一个持续 Session 中完成代码开发、文件管理、公开网络研究、轻量 Memory、Skills 和基础办公文档处理。项目不依赖 xiaoerAI 运行，也不使用数据库或向量库。

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

P1.2 实际使用反馈整改已完成：最终全量回归为 334 passed、1 skipped，真实产品与非产品联网闭环均通过。

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

## 启动

以下三种入口等价：

```powershell
python cli.py
python -m genesisai
genesis
```

GenesisAI 不配置默认工作区。交互式启动时首先选择并确认已有工作区；在提示中直接回车表示用户明确选择当前目录。非交互模式必须提供 `--workspace PATH`。工作区确认后才进行远程数据外发确认，并可初始化由 GenesisAI 管理的 `agent_docs/` 项目工作记忆。非交互模式可使用 `--yes-agent-docs` 初始化，或使用 `--no-agent-docs` 跳过。

选择结果会作为可信运行时状态直接提供给 Agent，Agent 无需通过 Shell 探测自己的位置。输入目录和输出目录都不得越过该工作区；普通相对输出写入工作区根目录。会话、Trace、命令日志和备份统一保存在该工作区的 `.genesis/`，不会写入用户目录中的全局 workspace 缓存。

网络、工作区写入和 Shell 默认分别询问，可在当前 Session 中授权：

```text
/allow network
/allow writes
/allow shell
```

也可以在启动时使用 `--yes-search`、`--yes-writes`、`--yes-shell`。Session 授权会被保存；删除、覆盖、越界和 Git 写操作仍受固定边界约束。

## 连续会话

CLI 未退出且未输入 `/new` 时，普通输入均属于同一 Session。上一轮用户问题、回答、已读取来源、Active Tools 和已加载 Skills 会保留。长会话会压缩早期内容，但保留最近一轮、目标、来源、变更和待办。重启后用 `--session SESSION_ID` 恢复。

## 主要命令

| 命令 | 用途 |
| --- | --- |
| `/help`、`/status`、`/history`、`/model` | 查看使用方法和当前状态 |
| `/new`、`/resume`、`/cancel`、`/exit` | 管理 Session 与运行 |
| `/tools` | 查看 Core、Active、Available、Disabled 工具 |
| `/skills list\|search\|show\|load\|unload` | 管理按需 Skill |
| `/memory list\|search\|show\|add\|forget\|validate\|changes\|undo` | 管理 JSON+Markdown Memory |
| `/project`、`/project init` | 查看或初始化 `agent_docs` 项目工作记忆 |
| `/permissions`、`/allow`、`/approve`、`/reject` | 查看或处理权限 |
| `/budget`、`/trace`、`/compact` | 查看预算、脱敏执行链和压缩上下文 |
| `/diff`、`/undo [apply]` | 查看或撤销本次文件变更 |
| `/doctor`、`/clean [run]` | 诊断环境和清理旧运行数据 |

## 架构约束

- 模型客户端只由一个 `Runner` 驱动，不创建子 Agent。
- 初始模型请求只携带六个 Core 目录工具；业务 Tool 与 Skill 先检索，再按需加载。
- Prompt、Tool 和 Skill 使用严格 YAML 必要字段，均无版本字段。
- Session、Run、Trace、项目 Memory 和项目 Skill 均位于用户指定工作区的 `.genesis/`。
- Memory 用 `index.json` 和独立 Markdown 条目保存关键事实，不复制项目全文。
- `agent_docs/` 保存项目策划、实施计划、进度、决策、已验证命令、验收与交接；代码、配置、测试和 Git 始终优先。
- Git V1 只读；Shell 直接执行参数数组，不经过命令解释器。

## Python 小项目开发循环

代码任务按“策划 → 计划 → 执行 → 验收 → 修正”循环运行。Python 项目会识别 `pyproject.toml`、源码布局、项目虚拟环境和测试框架；`test_run` 可自动选择 pytest、unittest，或在没有测试时执行语法编译检查。文件发生修改后，即使随后读取 Git diff，Runner 仍保留待验证状态并在交付前运行测试。只有最近一次代码修改之后的真实验证通过，`agent_docs` 任务才能标记为 `verified`。

从零创建 `.py`、`.java`、`.html` 等源码文件以及开发任务短回复延续的补全方案已纳入主线。

`agent_docs/` 默认适合纳入 Git，目录只保存人类可审查的项目状态；会话、模型中间状态、完整命令日志与备份继续保存在 `.genesis/`，不进入项目文档。

## 文件能力

代码与常用文本支持读取、搜索、创建、哈希保护补丁、移动和删除，并保持可识别的编码、BOM 与换行。DOCX/XLSX 支持读取、创建和有限修改；PPTX/PDF 支持读取和基础创建。办公能力需要 `.[office]`，公式不会在 GenesisAI 中重新计算，任意原稿的高保真编辑不在 V1 范围内。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m genesisai.evals.simulation --mode offline
.venv\Scripts\python.exe -m genesisai.evals.python_development
.venv\Scripts\python.exe -m genesisai.evals.simulation --mode deepseek
```

Python 开发仿真使用 `agent_tests_workspace/` 作为只读源工作区，数据清单位于 `tests/python_development_dataset/`，覆盖完整开发循环、中断恢复与源码漂移、失败验收门禁。通用仿真数据位于 `tests/product_acceptance_dataset/`。真实模式会调用已配置的模型，其中网络研究案例会访问公开网络。

源码创建、短回复延续、完成门禁和终端安全的离线仿真可运行：

```powershell
.\.venv\Scripts\python.exe -m genesisai.evals.source_development
```

该套件覆盖 HTML 创建与静态验收、Python 创建与测试、Java 验证受限状态以及二进制扩展名拒绝，不调用远程模型。

详细用法见 [用户指南](docs/用户指南.md)，总体设计见 [GenesisAI 升级路线](docs/GenesisAI升级路线.md)。
