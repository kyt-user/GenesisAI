# 仓库审计待办（暂缓处理）

- 审计基线：`fe5927b`，2026-11-03，Windows / Python 3.11。
- 用户决定：以下问题先归档；当前优先补齐完整输入输出日志，用于检查可用性。
- 原始离线测试：340 passed、5 failed、1 skipped；模拟模型与临时文件复现，未调用真实模型。

## 待办

- [ ] P1 Shell 仅校验 cwd 和命令黑名单，Python 可以读写工作区外路径；`git -C 路径 init` 可绕过 Git 写命令解析。位置：`core/extensions/tools/shell/process.py:25`。
- [ ] P1 `/allow writes` 同时放开 editor 删除、修改；`destructive=True` 未参与权限决策。位置：`core/tools/permissions.py:20`、`core/extensions/tools/kernel/editor/implementation.py:160`。
- [ ] P1 Windows 实际文件名 `.ENV` 可以绕过小写 `.env` 保护。位置：`shared/security.py:54`。
- [x] P1 撤销逻辑存在删除后重建覆盖与多次编辑冲突问题。2026-11-03 已撤下 `/undo` 及其实现。今后重新设计撤销时应覆盖这两种场景。
- [ ] P1 DeepSeek thinking 模式下，Runner 持久化及上下文重建丢失 reasoning 字段。2026-11-03 真实 API 验证返回 HTTP 400，具体测试构造、限制和日志定位见 [BUG-004](../docs/问题及解决清单/004-上下文预算与压缩续跑.md)。
- [ ] P1 `attempt_completion` 绕过开发变更、验证和证据检查；语法错误且待验证的代码也可标记 completed。位置：`agent/loop.py:486`。
- [ ] P1 测试识别依赖字符串关键词；`python -c "print('test')"` 能把有错误代码的任务标记 verified，实际没有执行测试。位置：`core/extensions/tools/kernel/run_commands/implementation.py:19`。
- [x] P1 批准执行漏记变更及验证状态。2026-11-03 已将更新集中到工具结果交付路径，覆盖批准修改、批准真实测试及连续追问回归。
- [x] P1 合法编辑导致历史哈希阻塞。2026-11-03 本地历史快照改为标记失效并提示重读，保留原记录；产物按已记录变更验证，仍拦截未记录的外部篡改。
- [ ] P2 MCP 写工具 prepare 返回 None，默认 ASK 模式直接 permission_error，无法进入确认流程。位置：`core/extensions/mcp/bridge.py:134`、`core/tools/executor.py:53`。
- [x] P2 架构契约失败：`agent.loop` 直接导入 `core.state.logger`。2026-11-03 日志改造将记录器改为端口注入，顺带消除此依赖，分层测试已通过；未扩大到其他架构重构。
- [ ] P2 测试资产缺失：`docs/SOURCE_BASELINE.json`、产品仿真 workspace 样例、`agent_tests_workspace`，共导致 4 项测试失败。
- [x] P2 使用文档已同步当前工具下发方式、项目任务记录和 `genesisai.app.evals` 评估入口，清理旧命令说明。

## 后续建议

先修覆盖/权限问题，再统一模型协议、工具执行后的状态更新和完成判定，最后处理会话版本一致性与测试资产。上述业务问题本轮不修改。
