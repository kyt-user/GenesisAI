# 审计与实施记录导航

本目录保留按日期形成的审计待办、实施记录和验收证据。早期未勾选条目、源码行号和限制值是记录时的状态，不能直接汇总为当前未解决问题；也不能仅凭较新报告的总标题推定某个旧问题已在所有提供商和环境中验证。

## 整合验收入口

- [2026-11-07 最终审计](2026-11-07-final-audit.md)：记录整合结果、失败后的专项复验、真实 Qwen 场景及保留边界。
- [机器可读证据](2026-11-07-validation-evidence.json)：报告路径、哈希、测试计数和跳过项。
- [149 项实施核销台账](2026-11-06-long-task-checklist.md)：逐项决定及实现、验证入口；对应[原始限制快照](2026-11-06-long-task-limits-audit.md)和[实施过程](2026-11-06-long-task-implementation.md)。

报告中的 1048 通过、0 未解决失败、10 跳过是对全量运行及专项复验按用例身份去重后的记录，不是一次零失败全量运行，也不是本次文档更新重新跑出的结果。

## 专项记录

| 范围 | 记录 |
| --- | --- |
| 长任务、路由和上下文 | [路由与预算](2026-11-06-routing-tools-budget-review.md)、[早期上下文评估](2026-11-05-context-assembly-review.md) |
| 独立作业和交互输入 | [作业审计](2026-11-07-durable-jobs-audit.md)、[异步接缝](2026-11-07-async-controls.md)、[早期交互审计](2026-11-04-interactive-runtime-audit.md) |
| 授权与环境 | [范围授权专项](2026-11-07-D02-K01-K02-K05-authorization.md) |
| 能力构建和阶段 Skill | [构建实施](2026-11-06-capability-builder.md)、[结果与阶段契约](2026-11-07-capability-contracts.md) |
| 文档、媒体、图片 | [文档与媒体](2026-11-07-document-access.md)、[文字图片接入记录](2026-11-05-text-image-change-assessment.md) |
| 记忆、索引、LSP | [后台维护](2026-11-07-async-maintenance.md)、[记忆引用](2026-11-07-F07-memory-references.md)、[项目探测](2026-11-07-h08-review.md)、[LSP](2026-11-07-lsp.md) |
| 网页和价格证据 | [网页资源](2026-11-07-web-resources.md)、[价格计算](2026-11-07-price-calculations.md)、[标识符边界](2026-11-07-price-identifiers.md) |
| 原始故障 | [11 月 3 日仓库审计](2026-11-03-repository-audit.md)、[外部会话审计](2026-11-06-external-session-review.md) |

现行产品行为从[文档导航](../docs/README.md)查阅。核销具体问题时应匹配当前源码、对应测试和场景证据；没有对应证据的条目保持原始记录，不自动改为已完成。
