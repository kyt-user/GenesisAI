# 遥测架构合并：测试保证映射

基线：客户端 #13974（`42a700f2d`，27 个遥测测试）。
已集成：#14070（`368082480`；此处新增 collector 契约用例之前有 26 个单元用例和 8 个真实流用例）。测试数量并不构成覆盖率等价的声明：直接导出（direct-export）的所有权契约是有意变更的。

| 基线保证 | 合并后覆盖 / 有意变更 |
| --- | --- |
| 初始化前后的非 Cline 排除；Cline/ClinePass 启用 | `keeps third-party providers disabled before and after direct initialization`；`shares one isolated exporter between concurrent Cline backend requests` |
| 关闭前刷写（flush） | `uses direct credentials even when a non-relay host owns an immutable tracer` 断言 SDK 拥有的「关闭前刷写」且无宿主清理 |
| 直接/代理中继标记抑制重复直接导出 | `recognizes a relay marker directly on the global provider`、`never registers cleanup or touches the host when direct export is declined`、`takes the relay path even after direct integration has been cached`，以及真实流直接/中继共存 |
| 直接 exporter 附加到可变宿主或代理委托 | **有意替换：** 现在使用隔离的 provider，从不附加到外部宿主。真实流的「清理后宿主存活」与无关调用检查保护隔离性 |
| 被压缩（minified）/无操作的全局 provider 可被替换；拒绝注册失败或不可变的外部槽位 | **有意替换：** 直接导出不再占用全局槽位，因此外部不可变性/注册被拒不再是禁用有效凭据的理由。不可变宿主测试证明直接导出成功且不修改宿主 |
| 直接内容/退出（opt-out）行为 | 保留现有的 direct-path 决策与 direct-opt-out 测试；真实流直接导出证明集成选择 |
| 默认 100%、显式 0、仅元数据、内容标志 | 保留现有决策用例；新增真实流内容缺失/存在用例 |
| 低于 100% 时的稳定任务哈希与缺失键拒绝 | 保留现有决策用例；被采样排除的流不发出 span |
| 全局退出、畸形 JSON、不可读文件、显式 false | 保留现有用例；新增真实流会话中途退出 |
| 仅控制台的 tracer 不计为中继 | 保留现有用例；不可变非中继直接宿主与真实流宿主所有权也被覆盖 |
| 中继消失后直接（导出）恢复 | 保留现有用例；销毁后重建隔离的直接运行时 |
| 缺失 host/config 时保持禁用 | 保留现有用例；不完整凭据被显式覆盖 |
| 真实 AI SDK 调用触发已注册的集成 | **有意替换：** 按调用集成而非全局注册；真实流发出 span，而无关 SDK 调用不继承集成 |

额外覆盖保护「异步直接初始化期间的中继注册」（采样关闭、已退出、仅元数据）、tracer 替换、独立异步 trace 上下文，以及 infra fixture 使用的实际 generation/step/tool scope/parent 契约。扩展生命周期测试仍单独保留；共享 SDK 测试不证明扩展的保留/销毁或打包。