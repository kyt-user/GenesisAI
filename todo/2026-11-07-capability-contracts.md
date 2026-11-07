# I09 / I11 / I12 正式契约实现与整合记录

状态：本批实现已整合并经过结果通道、阶段Skill、真实隔离试运行与授权专项验证；早期暂停状态已结束。详细结果见2026-11-07-async-controls.md，最新整轮审计见2026-11-07-final-audit.md；下表“必须补验”为原整合检查范围，最终以专项与全量报告核销。历史测试/失败现场保留。

| 编号 | 实现范围 | 当前证据 | 必须补验 |
|---|---|---|---|
| I09 | execution.py 唯一 GENESISAI_RESULT_PATH；json_results.py 全 JSON/schema/哈希/父链校验；tool_results.py + artifacts.py 不可变发布；CapabilityService 收取；read_job/memory_read 精确路由 | 新 tests/test_tool_result_channels.py；旧 Tool fixture/manual/protocol 已同步；静态报告 temp/document-price-capability-static.json | 所有 JSON 根；重复键/非有限数；独立结果/日志额度；大结果尾页；失败日志；同 job 幂等；跨会话/工作区拒绝；首次收取撤销；已收取历史；升级后旧 job |
| I11 | shared/skill_graph.py、SkillRegistry、contracts/tools/protocol、activation/service/trial、七个 builtin manifests、use_skill、Session 默认/schema、CLI/context/测试 | tests/test_skill_stages.py；active_skills 正式字典；无 flat tools 兼容 | 前置闭包顺序/循环；必需与可选缺失；200 工具/1100 阶段；CLI加载卸载/会话恢复；真实 Skill trial |
| I12 | trial_adapters.py 显式工厂注册；skill_trial 重建；jobs/operations.py 仅传工厂标识/描述；参考隔离记忆查询工厂 | tests/test_skill_stages.py 的服务实例隔离用例 | 实际后台 Skill trial 工厂重建；生产记忆不穿透；未注册工厂拒绝；最小工具集/递归能力构建禁止 |

## 根整合需知

- `active_skills`：由列表替换为 `{skill_name: stage_id}`。`SkillDescriptor.stages` 替代 `.tools`；`SkillRegistry.load(name, stage, current, available=...)`；正式调用 `use_skill(name, stage)`。所有新会话/schema/CLI必须使用字典。旧用户历史保留，按用户要求不加旧格式迁移。
- Skill definition：`entry/stages`，阶段字段固定 `depends_on/required_tools/optional_tools`；tests 的 input 增加 stage。宿主快照 `trial_adapters` 保存导入工厂标识，不序列化生产服务。
- 外挂 Tool 作业数据增加 `owner={id,revision,digest}`、`result_max_bytes`。脚本结果写 `GENESISAI_RESULT_PATH`；`output_schema`/测试 expect 仍校验原始 JSON。工具外层为 `value_included/value/result_ref/logs`，不能用 truthiness 判断值是否存在。
- `artifacts.publish` 支持稳定 ID/ownership；有 owner_session_id 的 job 资产须通过 session/access 校验。`capability_result` source 绑定 session/workspace/job/stream/hash。`memory_read` 可读取该来源或 capability artifact；未将其伪装为普通文件来源或知识笔记。
- `read_job` 仅修改 external_tool 完成/失败分支。memory_reference 可独立追加 input_id 早期控制分支。没有改 JobService/worker 授权逻辑。
- 根指定的 `ToolRuntime.submission_batch(calls)` 委托方法已加入；helper/Agent loop/ports 由根负责。
- GENESISAI_RESULT_PATH 在 shared.process_tree 的私有变量禁止映射规则由 memory_reference 负责，run_tool 最后注入实际路径。

## 恢复验证顺序

先运行纯价格/阶段/通道专项，再运行实际 worker/Skill/授权与大结果收取；最后 Qwen Token Plan 的 live_document_pages、live_price_calculations、live_capability_build。pytest basetemp 使用系统 TEMP 下未存在的新 `gai-<8位>` 短目录；XML/JSON 报告继续写仓库 temp。必须保留此前失败报告及现场。

待核接缝：ExternalTool env_from 的宿主解析路径仍在静态核对，不扩大密钥向普通 worker 的传播。F07 body_ref 不自动宣称支持新增 capability_result；目前明确正文访问接口为 memory_read/read_capability_artifact。
