# D02 / K01 / K02 / K05：范围授权与显式环境映射

## 接口与范围

- 保持 `user` / `auto` 模式。`AuthorizationService` 只由用户 CLI / 确认界面调用，未提供模型可调用的授权工具。
- 会话记录新增 `authorizations`：包含 `id/session_id/scope/task_id/tools/paths/domains/address_ranges/env_from/headers_from/command_prefixes/source/created_at/revoked_at`，严格校验，不读取历史授权格式。
- `/authorize JSON` 创建 `session` 或当前 `task` 授权；`/authorizations` 查看；`/revoke id` 撤销并向关联作业发取消请求。`run_commands` 范围授权必须声明实际可执行文件及 argv 前缀。
- `PermissionPolicy.authorization_for` 匹配工具、规范路径、域名、环境映射及命令前缀；资源不足时要求具体确认。强注册验收和删除操作不能被范围授权或 `auto` 绕过。
- 删除固定 900 秒确认期限和 `expires` 接口；保留当前参数/资源预览摘要和批准摘要绑定。`agent.loop._confirm` 同步移除旧过期分支，避免确认后再次进入待确认循环。
- 排队作业记录授权编号，并在取得全局资源槽后用 `Session.inspect` 检查持久化授权与当前任务；不构造 Session 打断原会话，不执行被撤销的队列工作。
- 根代理负责 `current_authorization` → `ToolContext` → worker / builtin job 快照传播，以及活动轮次 `/revoke` 的 RunQueue 接缝。web 代理负责初始 URL 与每跳重定向的域名范围核验，公网 SSRF 边界保留。
- G06 接缝扩展：`ToolSpec.network_boundary` 为 `public/private`，private 仅允许网络工具声明。private 在 auto 也要求范围 grant 或具体批准。授权 `address_ranges` 规范化 IP/CIDR，匹配要求请求集合每项均为授权地址段子网，域名同时匹配；IPv6 字面量规范化。普通公网工具不因内网 grant 放开 SSRF。具体批准证据由根传播已验证的 `accepted_tool {name,digest}`，模型参数本身不构成批准。新增纯逻辑 user/auto、IPv4/IPv6、扩大 CIDR、混合范围、跨域及撤销用例，待环境恢复执行。
- `/constraints JSON` 显式设置 `read_only/network_forbidden`。`TaskService.update_constraints` 记录 CLI 来源、task 作用域及旧值撤销；CLI 不接受不可达的 turn 参数，内部用户消息仍可绑定真正的 turn 生命周期，保留历史，不在新任务继承旧任务约束。

## 环境隔离

- `safe_environment` 改为精确系统运行变量及非敏感应用配置名单，删除基于 KEY/TOKEN 等子串的推断过滤。
- `validate_environment_mapping / mapped_environment / environment_preview` 分别负责变量名契约、宿主值解析及摘要。Windows 同名大小写冲突拒绝；来源变量缺失明确失败。
- `run_commands.env_from` 与外挂 Tool `definition.env_from` 使用子进程目标名称到宿主来源名称映射。范围授权要求映射逐项匹配；具体审批摘要包含值摘要。Tool 测试与正式执行使用同一映射。
- 实际变量值只在进程启动输入中传递，并登记脱敏；不写入 job 元数据。内置工具使用的文档、OCR、ASR 配置属于独立非敏感快照。
- 已修复一次集成缺陷：根新增 builtin_tool 配置快照含 `None`，原通用 secret 收集误将它纳入脱敏集合，使 worker 的正常结果与错误发布都再次异常。现在只对 external_tool 的显式映射收集值，并统一过滤非字符串与空值；根另加 worker 输入验证与发布故障兜底。

## 实际证据

- `temp/authorization-first-7e31.xml`：24 通过、3 失败；3 个失败定位为 `_confirm` 残留 expires 分支，已修复，报告保留。
- `temp/authorization-scope-first-7e31.xml`：37 通过，41.30 秒，含授权专项、tool runtime、确认参数/资源绑定与恢复用例。
- `temp/authorization-environment-7e31.xml`：10 通过、6 失败，包含上述 builtin config/secret 集成缺陷，报告保留。
- `temp/authorization-environment-followup-7e31.xml`：28 通过、3 失败，50.95 秒。新授权 11 项及 file job audit 6 项已通过；剩余旧断言已按统一作业契约更新：后台等待包含 queued/stopping，拒绝读取的空数据为 `{}`。
- 新授权测试通过真实 Windows 命令子进程及完整 Tool 创建→冻结→测试→验收→执行链路验证隔离，不只是 mock。覆盖会话隔离、任务切换失效、长时间等待、命令前缀、映射、队列撤销及日志不泄露实际值。
- 后续 `temp/authorization-complete-8b03.xml` 轮次在环境整体响应异常时启动，持续无输出；根要求停止，执行句柄 52366 已 Ctrl-C，exit_code=1。此轮不计通过，已确认没有生成报告。没有覆盖先前证据。

## 待集成验证与边界

- 根完成 worker 故障兜底后复跑已更新的 3 个旧断言及授权完整专项；活动轮次 `/revoke` 需根 RunQueue 接缝回归。
- `AuthorizationWatch` 独立覆盖排队与运行生命周期，仅带授权编号的作业启用；无授权作业不读 Session、不启动监控线程。排队授权失效在未取槽时失败；运行失效由原 command 停止分支 / operation RunControl.cancel 收尾，副作用未知优先保留，资源槽不提前释放。关闭监控最多等待 0.2 秒，不在 publish 抛异常。域名 scope 快照本身不查询持久化授权。
- 路径范围、工作目录和命令前缀是调用批准边界，不是 OS 沙箱。任意批准的程序仍以当前系统账号权限运行；外部命令的文件和网络访问不能由这些参数推断为已隔离。
- 当前任务的真实模型 API 验收由根统一使用 Qwen Token Plan 执行；此子任务未独立调用付费模型，不把本地进程测试记作模型 API 测试。

## 后续接缝与静态审计

- `headers_from` 是独立正式授权字段：HTTP token 名称、大小写冲突及宿主变量名严格校验；匹配时头名规范为小写，和 env_from 分开，不为 HTTP 连字符放松进程变量命名。
- `/revoke` 改用 `JobService.stop_authorization` 聚合取消，先向全部关联作业发信号，再共享最多 5 秒的观察窗口，避免 N 个作业逐一等待 N×5 秒。根负责活动轮次先停 runner、释放 Session 后执行撤销。
- `require_active` 错误文案改为阶段无关，不再声称“作业未开始执行”。根负责在首次收取 staged effects 前再次核验授权，已完成 receipt 历史读取不重复提交效果。
- 显式保留运行期 `GENESISAI_RESULT_PATH`；所有用户 env_from 映射均拒绝覆盖该目标。I09 Tool 专用结果通道由文档代理注入实际值。
- A10 接缝：builtin_tool 作业 metadata.side_effect 来自冻结 descriptor 的 data.side_effect，非模型参数；未知和非 builtin 不猜作只读。
- 当前静态 AST 与重复定义审计已检查授权、权限、ToolSpec、jobs service/worker/watch、tasks、环境、CLI、新授权/HTTP 头测试，未发现重复字段/函数或语法错误。此前工具异常重复插入的 ToolSpec 和测试副本已幂等清理；没有保留重复兼容接口。

## 独立槽位故障

原 worker/LSP 对 Slot 的所有 OSError 均按“锁忙”重试，目录权限、初始化写入失败或其他锁错误可无限保持 queued。改为 SlotBusy(BlockingIOError)；只在锁调用阶段将 EACCES/EAGAIN/EWOULDBLOCK 转换，open/write/其他锁错误原样上抛，初始化失败关闭流。worker 与 LSP 仅捕获 SlotBusy。新增 tests/test_job_slots.py 覆盖锁忙错误分类、open 拒绝、初始化写满及非法描述符；尚未执行。

根的 temp/joberr-a7bb9451 现场另有明确证据：未发布临时文件已是 running + execution_started，实际已取得槽，卡在状态保存的 flush/fsync/replace 阶段。本修复不据此宣称解决现场停顿；原测试中断且不计通过，环境 I/O 问题单独保留。

## 最终待测列表

环境恢复后首先执行 tests/test_scoped_authorizations.py、tests/test_authorization_headers.py、tests/test_job_slots.py 与根 tests/test_job_error_reporting.py；重点是排队撤销、运行中任务替换、unknown 不被取消覆盖、无授权作业正常结束、HTTP 头/环境映射分离和内网 auto 范围判定。新增这些用例目前只完成语法/静态核对，不计测试通过。

随后复验已修改的 tests/test_external_session_regressions.py 三项旧断言，以及 tests/test_tool_runtime.py、确认摘要验收用例、tests/test_capability_lifecycle.py、tests/test_job_cancellation.py、tests/test_file_tool_job_audit.py；CLI RunQueue、真实内网 HTTP/DNS/重定向、I09 新结果通道由对应根/代理合并回归。最后由根完成真实 Qwen Token Plan 验收。
