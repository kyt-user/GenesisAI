# 统一作业独立审计与复验

审计范围：`core/jobs` 的取消、宿主退出/重开、未知结果、禁止重放、权限变化和资源槽位；并复验上一轮 `Maintenance` 关闭/重启修复。未提交 Git，未删除用户数据，未调用付费模型。真实模型集成由主任务使用 Qwen Token Plan 验证。

## 已实证问题

| 编号 | 等级 | 问题与影响 | 实证 | 修复与状态 |
| --- | --- | --- | --- | --- |
| JOB-A01 | P1 | `run_operation` 捕获 `unknown_execution` 后，取消分支又无条件将状态覆盖成 `stopped` / `timed_out`。`operation_result` 因状态非 unknown 清除 pending，丢失未核对副作用的完成门禁。 | 独立 worker 中 MCP 测试传输先写入一次副作用，再等待取消并抛 unknown。旧结果为 stopped，code 仍为 unknown_execution，pending 被清空。 | 已修：unknown 优先于停止/超时的状态覆盖。MCP、外挂 Tool、内置 Tool 均适用。明确发送前取消仍为 stopped。 |
| JOB-A02 | P1 | `JobService.stop` 对心跳失联派生的 unknown 提前返回，不写 stop.json；`stop_all` / 权限降级复用该路径，失联期间无法留下停止请求。unknown 不是进程已退出的证据。 | 仅暂停探针自建独立 worker 16 秒，实际命令子进程仍继续写入夹具。旧 stop 返回 unknown 且无停止标记。 | 主任务已修：unknown 仍写 durable stop 请求，返回 unknown + cancel_requested，不伪造停止确认。已做状态专项和真实 worker 复验。 |
| JOB-A03 | P1 | 已授权外挂 Tool 排队后只保留 bundle 路径；worker 出队后未核对冻结 manifest，可能执行排队期间被替换的代码。 | 经创建→冻结→测试→注册→正式调用，先占满唯一执行槽位；queued 后替换夹具 tool.py。宿主 materialize 已能发现 capability_integrity，旧 worker 仍 succeeded，返回 999 并执行新副作用。 | 已修：出队后、启动程序前完整核对文件集合、hash、size、目录/祖先/内部链接；不一致返回 capability_integrity，操作不执行，无自动重放。宿主 materialize 复用同一校验函数。 |

### 原始证据（保留）

- JOB-A01 原复现：[report.json](../temp/auditjobs-unknown-7e31/report.json)；修复后：[report.json](../temp/auditjobs-unknown-fixed-7e31/report.json)。修复后状态为 unknown，pending 保留同一 job_id，副作用仅一次。
- JOB-A02 原复现：[report.json](../temp/auditjobs-stale-stop-worker-7e31/report.json)；修复后：[report.json](../temp/auditjobs-stale-stop-fixed-7e31/report.json)。修复后停止标记存在，状态仍 unknown；探针恢复自己的 worker 后显式停止并等待退出。
- JOB-A02 首个探针暂停了 Windows venv launcher，而非 job.json 中的实际 worker PID，未触发失联；[该报告](../temp/auditjobs-stale-stop-7e31/report.json)属于无效复现，未作为缺陷依据。后续探针使用自建作业记录中的实际 host_pid。
- JOB-A03 原复现：[report.json](../temp/auditjobs-bundle-7e31/report.json)。修复后的真实排队验证记录在 pytest 报告内；改动后的程序未运行，工作区未出现正常或被替换程序的副作用文件。
- 可复现脚本：[probe_unknown.py](../temp/auditjobs-7e31/probe_unknown.py)、[probe_stale_stop.py](../temp/auditjobs-7e31/probe_stale_stop.py)、[probe_bundle.py](../temp/auditjobs-7e31/probe_bundle.py)。运行时须传入尚不存在的新临时目录；原 report 不应覆盖。旧 bundle 脚本按旧成功行为编写，修复后会在读取失败结果时抛 capability_integrity；正式修复断言使用新增 pytest。

## 修改范围

- `core/jobs/worker.py`：仅 `run_operation` 取消终态覆盖的 unknown 优先分支。
- `core/jobs/operations.py`：仅 `external_tool` 分支启动前校验；主任务新增的 builtin_tool 分支保留。
- `core/capabilities/assets.py`：新增 `verify_bundle`，复用已有流式 file_identity、files_under 和链接检测。
- `core/capabilities/store.py`：materialize 复用完整性校验，清理原重复实现及未用导入。
- `tests/test_job_cancellation.py`：未知结果×取消/超时×三种作业；明确发送前取消；心跳失联和已记录 unknown 仍可请求停止。
- `tests/test_job_bundle_integrity.py`：真实 worker 排队后替换包；新增/缺失/修改文件；真实 Windows junction 祖先路径（其他平台使用目录 symlink）。
- `service.stop` 的修复由主任务完成，本审计仅补充测试。

## 实际验证

所有 pytest 使用新建短 basetemp，未覆盖旧报告。

| 报告 | 结果 | 范围 |
| --- | --- | --- |
| `temp/audit-job-cancellation-7e31.xml` | 11 passed，3.94 秒 | 初版取消专项 7 项 + 既有 durable_jobs 4 项 |
| `temp/audit-job-cancel-stop-7e31.xml` | 9 passed，0.95 秒 | 追加 unknown 停止请求的取消专项 |
| `temp/audit-job-integrity-7e31.xml` | 19 passed，20.32 秒 | 包完整性 5 项、取消 9 项、既有 durable_jobs 4 项、既有 bundle 篡改 1 项；此时链接测试使用模拟链接属性 |
| `temp/audit-job-integrity-link-7e31.xml` | 1 failed，0.73 秒 | 替换为真实 junction 后，受限执行环境不能创建测试链接；尚未执行产品断言，原报告保留 |
| `temp/audit-job-integrity-link-elevated-7e31.xml` | 1 passed，0.44 秒 | 提权环境创建真实 junction，确认产品拒绝链接祖先 |
| `temp/audit-maintenance-generations-7e31.xml` | 3 passed，1.64 秒 | close/enqueue 交叉时序；跨实例持 SQLite 写锁时取消；关闭后重新打开恢复 |

既有 durable_jobs 通过了实际提交宿主退出后继续执行、完整分页日志、跨 JobService 重建读取、排队/运行取消、并发槽位、明确日志配额失败等路径。未观察到自动重放。

## 限制与后续范围

- 真实 junction 创建需要允许该文件系统操作的测试环境；已在提权执行中验证。新增测试不对这项安全断言静默跳过。
- 出队校验覆盖持久排队带来的检查/使用间隔，不承诺抵御同权限宿主进程在校验结束后、解释器打开文件前的恶意竞态；独立作业不是 OS 沙箱。
- 主任务正在构建的 `builtin_tool` staged effects 适配器仍需单独审计收取幂等性、授权变化、观察事件和事务错误；本轮仅证明它的 unknown 错误不会被 run_operation 的取消覆盖。
- 资源槽位的既有回归覆盖一个队列的并发与取消。嵌套 Skill/能力测试是否共享同一全局配额、队列公平性与多个隔离 runtime 的配额继承，本轮尚未完成实证，不列为已确认缺陷。
- 本轮取消、冻结包完整性和维护恢复验证均使用真实本地独立 worker 或针对状态分支的离线测试；没有将其描述为真实 Qwen API 验证。

## E01 文件作业适配器追加审计

主任务完成 `tool_operation.py` 初稿后要求追加审计；以下均运行真实独立 worker，临时测试为 [test_file_job_audit.py](../temp/auditjobs-7e31/test_file_job_audit.py)，不修改 E01 实现，先报告主任务处理。测试使用纯夹具内容，无真实密钥。

| 编号 | 等级 | 已复现问题 | 证据/参考修复 |
| --- | --- | --- | --- |
| E01-A01 | P1 | `collect_tool` 重复返回相同 resource_changed；第二次 read_job 新 call_id 再次产生变更观察，会重新标脏和使新来源失效。已有测试只验证 changes 列表去重，未验证事件去重。 | `temp/audit-file-tool-initial-7e31.xml` 第一项；持久化收取/观察投递状态，保证重复读结果不重复施加变更，同时考虑收取提交后宿主退出的事件恢复。 |
| E01-A02 | P1 | 已完成 read_files 的源文件随后改名，collect_tool 使用 Access.read(strict=True) 导致 FileNotFoundError，已保存的结果不可收取、pending 无法解除。 | 同报告第二项；授权校验应支持历史快照路径，不应要求原文件仍存在；当前状态与历史读取结果分开表达。 |
| E01-A03 | P1 | ToolJobStore 新 RunLogger 未继承原宿主已登记 secret；独立 worker 环境已过滤密钥变量，result.json 外层脱敏正常，但 effects.json 和内部 job/logs/session_*.jsonl 仍写入纯夹具 secret 原文。 | 同报告第三项；原宿主 secret 集合应仅经内存引导传播到独立执行器、内部 logger、effects 和子工具输出脱敏器，不写原文配置。 |
| E01-A04 | P1 | `list_files` 没有来源/产物/变更登记，其 effects 为空，绕过 collect_tool 对路径的权限复核。auto 下完成对 grants 外测试目录的读取，降为 user 后仍能收取该目录列表。 | `temp/audit-file-tool-scope-7e31.xml`；收取权限不能只依赖 source/artifact 登记，需要真实访问记录或覆盖所有结果的原授权范围校验。 |
| E01-A05 | P1 | 副作用与 effects 已成功，result.json 超配额时被 run_operation 记为 failed/job_error；read_job 因此清空 pending，主会话尚未导入 changes。 | `temp/audit-file-tool-quota-7e31.xml`；执行成功后的发布失败应保留 unknown_execution 和核对门禁，区分执行失败与结果发布失败。 |

原始报告：首批 3 failed/6.20 秒，权限范围 1 failed/3.14 秒，结果配额 1 failed/2.67 秒。这些是期望断言定位问题的失败证据，不是修复通过报告。各测试 fixture 关闭并停止其作业；仅改名/修改自建文件，未操作用户内容。

E01-A05 已按主任务授权修复：`run_operation` 将 execute 已返回后的类型校验、脱敏及结果保存独立为发布阶段，失败统一报告 unknown_execution。发布失败不会触发重试，也不会清空待核对作业。新增五类 operation（MCP、外挂 Tool、内置 Tool、能力测试、能力试用）×实际结果配额/磁盘错误共十项；真实内置作业已写入文件后结果超配额仍保持 unknown 与 pending。`temp/audit-result-publication-fixed-7e31.xml`：20 passed，3.53 秒。

追加审计五项断言均已迁入 `tests/test_file_tool_job_audit.py`，使用独立 fixture。主任务已修复 E01-A01～A04：job_receipts 绑定首次已完成 collector 的 call_id，未完成 collector 可移交观察；Access.read_reference 按当前范围核验历史绝对路径并标 stale；完整 access 快照控制所有结果的收取；secret 集合只经 stdin 传播到独立 logger。

修复后独立复验：`temp/audit-file-tool-followup-7e31.xml`，五项审计断言 + 原五项 file_tool_jobs **10 passed，19.89 秒**。另新增 receipt 中断恢复专项：effects/receipt 已提交、原调用尚未 finish，重开 Session 将 started 转 unknown，新 collector 接管观察，第三次收取不再重复观察，changes/artifacts 各一条；`temp/audit-file-tool-receipt-recovery-7e31.xml`，**1 passed，3.27 秒**。

至本记录最后更新，JOB-A01～A03 与 E01-A01～A05 均已实现并得到上述专项证据；主任务的全量回归、真实 API 测试和 Git/GitHub 提交流程仍由主任务执行。
