# 异步执行接缝审计（已收尾）

本文件按时间保留增量和失败诊断。后文的历史待办已在最终整合中关闭；当前结论、全量与修复复验、真实Qwen证据见 [最终审计](2026-11-07-final-audit.md)。新增浏览器作业、写入协调、套件续跑及风险验证均已接通并验收。

## 已补的实现

- 作业 host 过滤非字符串/空 secret；错误信息脱敏失败时使用固定安全信息，不让错误报告再次抛出而留下 running + finished、无 result 的假状态。结果发布失败保留 unknown_execution，不自动重发。
- executor 对通过具体确认的工具设置 accepted_tool；tool worker 初始化与 authorize 消息、builtin job 快照传播确认名称和摘要，出队后重新准备并验证资源摘要。
- 首次收取 builtin effects 前重新检查范围授权；已完成 receipt 的历史读取不重新施加 staged effects。
- RunQueue.defer_until_stopped 在 runner 释放状态后由终端线程执行控制操作；运行中 /revoke 先停止轮次，再撤销授权和停止关联作业，close 路径同样处理已提交控制。
- inspect_project 是独立分页后台工具；run_commands 旧 operation 分派已移除。项目扫描不保留旧 5000 文件、40 个测试文件、8000 字符、100 依赖/HTML 限制；声明入口优先，多根入口及未明确子包不自动选择。

## 当前验证状态

- 此前 Qwen 异步 DOCX 实测：temp/live_async_tools_24cf9d1f6ab543e8/report.json，10/10；该报告早于本条最新确认/授权字段，不替代最终实测。
- 本次 11 个 root 改动文件 AST 检查：无语法错误、重复定义、重复字典键、相邻重复语句和确认协议重复字段。
- 最新宿主控制回归 `temp/async-recovery-final-controls.xml`：27 passed，覆盖宿主租约、非终态判定、跨会话文件作业、并发提交、取消队列和资源槽。
- 能力契约回归 `temp/async-recovery-capability-contracts.xml`：65 passed，1 failed、1 error 均为 Skill 测试夹具缺失；修正后 `temp/async-recovery-skill-stages.xml`：9 passed。
- 日志通道整批先为 26 passed、2 failed（Windows 换行与测试局部变量遮蔽）；修正后两项在 `temp/async-recovery-host-liveness.xml` 中通过。该报告还有一项宿主退出时锁异步释放断言失败，已修正观察窗口并包含在上述最新 27 passed 中。
- 最新 Qwen Token Plan 真实异步 DOCX：`temp/live_async_tools_7dfc626d231a45ba/report.json`，10/10，包含同一作业收取、实际随机尾段、无重发、来源导入和原件未改。
- H08 回归 temp/project-discovery-first.xml 未完成即中止，不计通过。保留原测试现场。
- 审计发现工具调用可能重复应用补丁，各作者已改为读回检查/幂等修正，必须再次核对最终文件。

## 环境阻碍与待办

用户已明确授权清理重复索引。清理前实际核验到 4229 个匹配的 VS Code rg；仅结束命令、可执行文件与创建时间匹配的索引进程，保留 VS Code 和文件。可用内存恢复到约 15 GB，测试已恢复。

已从本机扩展源码定位 `ms-python.vscode-python-envs` 1.42.0 的 `registerWorkspacePythonContext`：每个 Python 标记文件创建/删除触发 `findFiles`，仅过滤过期结果、没有取消旧扫描。清理后仍产生新扫描；另一次收尾结束了 180 个。是否暂时在当前工作区禁用该扩展另行询问，尚未收到答复，未修改编辑器设置。所有旧日志、会话和测试现场保留。

待完成：新的 worker/授权回归、H08 独立审计修正、并行读取调度与冲突协调、交互作业输入、能力套件恢复与剩余 MCP 接口审计，以及总台账核销。私网/浏览器、范围授权、分阶段 Skill 和完整结果通道仍有并行实现，未计完成。
## 本轮追加实现

- 宿主通过进程持有的 `host.lock` 确认存活；心跳慢但锁仍被持有时保留原运行状态并标记 `heartbeat_stale`。宿主丢失后保持 unknown，不自动重放。重复宿主及非 queued 作业不能再次执行。
- 日志发布要求执行期 `redacted=true` 与 `original_bytes`，传播到不可变 artifact 和 Session source，重复收取校验一致性，拒绝原始日志清单。
- H08 根 pytest 配置链接检查补齐调用；网络自身范围与当前范围交集已读回确认。私网工具清单补齐 category/enabled，目录初始化恢复。

## 交互输入与参数契约追加验收

- H07 已接通 pipe/PTY 宿主、分块输出、带 HMAC 的输入回执、可取消连接及拒绝取消后的延迟提交；新增 write_job_input 和 read_job(input_id)，CLI 运行中/空闲均支持 /job-input。CLI 不保留该命令原文日志或输入历史。
- Windows ConPTY 已实际使用 pywinpty 3.1.2 测试；按模式处理回车，非交互命令仍显式 DEVNULL。
- `temp/interactive-job-input-regression.xml`：27 passed；`temp/interactive-input-adversarial.xml`：12 passed，含写满管道后取消、转义字节配额、PTY 密钥回显、CLI 不提交活动 Session。
- 参数校验替换为完整 JSON Schema 2020-12，递归执行 array items、对象及 additionalProperties/enum/pattern 等约束，拒绝非有限 JSON 数值；删除旧手写浅层校验。`temp/nested-schema-cli-contracts.xml`：64 passed。
- Qwen Token Plan 实际交互验收 `temp/live_interactive_jobs_fd91e782ce664678/report.json`：11/11；单次启动/输入、相同作业收取、实际随机结果及引用、无待收取任务。
- Qwen Token Plan 价格派生来源验收 `temp/live_price_2a4d5f20111648a9/report.json`：7/7。
- 最新宿主复核重复索引数量为 0，可用内存约 16 GB；没有修改 VS Code 配置。
- 当前全量回归报告目标 `temp/full-regression-4af4c295.xml`；尚在执行，已有失败，不能计为通过。

- H07 的最新 Qwen 交互 API 验收发生在完整参数 Schema 与 CLI 接入之后；报告 `temp/live_interactive_jobs_fd91e782ce664678/report.json`，11/11。用户指南已增加 pipe/PTY、三种输入操作、回执与 CLI 示例。

## 本轮审计待修（P1）

代码核对发现通用日志脱敏器还用于 `jobs/worker.py` 的私有 `result.json` 和 `ToolJobStore.commit` 的 `effects.json`。如果显式环境变量值很短（例如 1 或 a），字符串替换会破坏 SHA、来源 ID、路径或输出契约；I/O 日志外层 event_id/timestamp 等结构字段也被一并替换。待用合成短环境值补回归，将执行协议的精确持久化与日志脱敏区分开，保留错误消息及 stdout/stderr 的脱敏；不得以跳过短密钥脱敏来修复。全量测试尚在运行，本项暂未修改生产代码，避免使该轮测试混用版本。


## 本轮全量失败修复及后续专项

- temp/full-regression-4af4c295.xml：1019项，995通过、13失败、11跳过。按真实错误修复 Skill 跨worker传递宿主回调、旧分阶段调用方、异步用例收取与跨进程测试计数；没有保留旧接口。
- 作业 result.json、effects.json、origin_call 保留精确协议数据，不再经过日志字符串替换。日志仅对 payload 脱敏，保留可信事件ID和时间；Path也按字符串脱敏。单字符密钥回归验证引用/哈希/路径不被损坏；明确JSON结果仍按声明保留原值，stdout/stderr和错误消息继续脱敏。
- 作业错误日志在独立宿主记录原异常，不因异步封装而丢失诊断。
- Windows嵌套Skill真实模拟发现临时文件名追加目标名与32位UUID越过路径上限；改用独立短临时文件，保留原子替换。原五场景产品模拟已通过；另加深目录回归。
- temp/async-audit-fixes.xml：142通过6失败，其中5项受同时变更OCR Schema的短暂语法错误影响，另1项遗漏旧Skill.load调用；均已修复，失败6项重新验证。temp/async-audit-failures-recheck.xml：10通过2失败，分别是网络夹具异常类型断言和嵌套Skill路径问题；已修正。temp/private-document-paths.xml：14通过1失败，来自测试进程的默认断网夹具阻止本机HTTP，现仅允许夹具端口。
- temp/private-protocol-final.xml：25项全通过，包含内网实际HTTP、授权撤销/缓存、私网边界、下载PDF实际OCR、短密钥协议、深目录原子写入、Skill阶段。
- G06：独立fetch_private_resource正式接通域名+CIDR授权、具体确认、每跳固定IP检查、授权绑定缓存、私网来源研究计数和Evidence校验。G10已补下载source_ref到OCR；浏览器渲染仍待实施。
- I06/J03–J06：MCP改request_timeouts分层、HTTP原生asyncio/httpx异步、stdio监督取消、目录发现后台作业；移除MCP旧timeout_seconds字段及调用方。5151工具/101目录页、稳定短名称完整描述、长URI/cursor、全量显式结构化链接、真实流式进度/总时限/心跳/重复进度/取消/no-replay：temp/mcp-layered-verified.xml 43通过。后续追加stdio递增进度与私网候选仅发现测试，待验。
- 全量最终回归、剩余目标实施、代码审计和最新真实Qwen验证尚未完成；尚未提交或推送。
