# 最终整合审计：通过

本轮对应149项限制清单：117项删除、放宽或重构，32项保留。已同步修改调用方、测试和文档；未增加历史格式迁移或兼容层。现行ENV优先、终端密钥回退、会话恢复与错误处理保留。原始诊断、失败报告、历史会话和用户文件保留。

机器可读证据、报告SHA-256及完整跳过列表见 [validation-evidence.json](2026-11-07-validation-evidence.json)。本记录所在Git提交是交付版本，推送结果在本轮交付说明中记录。

## 验收结果

- 最终产品源码的全量运行：1058项，1044通过、4失败、10跳过，耗时1377.54秒（temp/full-regression-final2.xml）。
- 4个失败均为测试时序假设：三个生命周期用例未收取转后台的作业；HTTP取消用例在请求发出前就触发定时取消。仅修改测试夹具和脚本模型步骤，产品源码未变更。
- 强制后台生命周期复验：3/3通过（temp/final-background-lifecycle-verified.xml）；完整MCP HTTP复验：17/17通过（temp/final-mcp-http-verified.xml）。按用例身份去重后的最新结果为1048通过、0未解决失败、10跳过；不把原全量报告写成零失败。
- 10个跳过均因Windows未授予符号链接创建权限，junction路径保护另有实际测试。真实Chromium、RapidOCR、Windows SAPI和本地Whisper转写已执行；本轮显式配置本地模型，语音用例未跳过。
- 架构检查：288文件、691依赖，2个约束保留、0违规。314个改动Python文件AST解析通过。
- 独立干净目录构建wheel：399个声明文件齐全，42个内置工具可加载；包内所有文件与最终源码逐字节一致。旧build缓存曾带入已删除shell/jobs.py，未交付该包；原缓存保留。
- 578个候选文本文件与2项本机已配置密钥比对，未发现泄漏；暂存区不包含.env、temp、模型缓存、运行日志、数据库或私钥。Git差异空白检查通过。

## 已关闭的主要发现

| 等级 | 问题 | 修复与证据 |
| --- | --- | --- |
| P1 | 任意子串脱敏破坏作业哈希、ID、路径与结果契约 | 协议值精确保留，日志负载独立脱敏；test_protocol_redaction.py |
| P1 | Windows嵌套Skill原子写临时路径过长 | 使用短临时文件名；真实嵌套Skill与深路径回归 |
| P1 | MCP固定总时长截断长调用、目录发现阻塞主机 | request_timeouts分层、异步HTTP/stdio取消、独立发现/调用作业；完整HTTP和5151工具/101页回归 |
| P1 | 中断测试套件可能重放未知用例 | 冻结版本检查点、OS独占锁、显式retry/mark_failed；test_capability_suite_resume.py |
| P1 | 后台写入相互竞争，editor绕开队列 | 父子路径写入协调；等待不占执行槽；editor进入作业、执行前再准备和复核哈希；test_write_coordination.py |
| P2 | 所有修改被强制运行全项目测试 | 按验收要求选择测试/构建/静态检查；普通文档内容核对；test_risk_verification.py |
| P2 | 格式拒绝发生在执行开始后，被误记为unknown | 无副作用正文/格式/来源校验前置到prepare；二进制拒绝回归 |
| P2 | 准备步骤无法用实际读取协议/工具发现完成计划 | 允许准备证据，仍与实际执行、验证证据分开；真实Qwen能力构建及任务/完成门禁回归 |
| P2 | .rst/.adoc文档分类与验收要求不一致 | 编辑器和验证策略统一分类；真实写入回归 |
| P2 | MCP发现的回环地址被标记为公网候选 | 按地址与入口标记网络边界；发现不授予访问权；test_p11_agent_runtime.py |
| P2 | 测试仍依赖旧同步执行、自动全套验证及定时取消竞态 | 跨进程故障注入、显式验证、同作业收取、流开始事件同步；四个最终失败全部复验关闭 |

## 真实Qwen Token Plan

均使用qwen_token_plan / qwen3.8-flash。8组报告共68项断言通过，各报告覆盖对应阶段；最新完整能力构建和浏览器链路分别为8/8、9/9。

| 场景 | 通过 | 报告 |
| --- | --- | --- |
| 构建、冻结、实际测试、独立试用、正式会话恢复；未注册 | 8/8 | temp/live_capability_32ed600a26544a18/report_resume_00ca71ed65b44a1e.json |
| 真实Chromium动态页面、同一job收取、来源引用 | 9/9 | temp/live_browser_18eabae07acf41d1/report.json |
| 异步DOCX实际尾段、来源、无重发 | 10/10 | temp/live_async_tools_7dfc626d231a45ba/report.json |
| 交互作业输入、实际随机结果及持久回执 | 11/11 | temp/live_interactive_jobs_fd91e782ce664678/report.json |
| Decimal价格推导与来源证据 | 7/7 | temp/live_price_2a4d5f20111648a9/report.json |
| 6张原图、分批发送、裁剪随机码、恢复 | 12/12 | temp/live_image_batch_190b867844214a32/report.json |
| 长原文大页及尾部读取、恢复 | 9/9 | temp/live_memory_pages_f61f75d3a13f4460/report.json |
| 新异步传输的流式工具调用与非流式往返 | 2/2 | temp/live_request_2a158e5e2bd7496a/report.json |

浏览器HTTP内容来自隔离夹具，模型API与Chromium是真实执行；不将其称为真实公网站点验收。能力构建曾耗尽手动验收预算，原报告保留，最终通过正式resume完成。用户指定Qwen之后的上述实测没有改用DeepSeek。

## 保留边界与环境说明

- 浏览器是独立无登录的受控GET读取器，DOM为时点快照；动态文档重定向显式阻止；非文档子资源重定向保留原浏览器URL。
- 写入声明是协作范围，不是OS沙箱；应用外编辑仍需前态哈希和备份保护。生成能力使用当前系统账号。
- 用户授权、数据保护、未知结果不重放、真实测试及注册验收继续生效。32项保留边界详见核销台账。
- 运行期间重复VS Code索引曾瞬时积累373个、CPU100%；后续核查已退出，没有修改编辑器设置。之前获准结束的重复索引按原记录执行。
- GitHub直连超时；使用Windows已启用的127.0.0.1:7897代理后远程读取正常，不修改全局Git代理或remote。

当前无待修代码审计发现；环境跳过与上述能力边界已明确记录。
