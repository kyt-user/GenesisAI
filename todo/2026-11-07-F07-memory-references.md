# F07 文档正文引用与原子事实条目

目标：落实原149项限制审计 F07。保留精确来源、版本和工作区隔离，使大型设计文档能按引用保存并按范围拆成独立事实，不再把完整正文重复放进模型参数。

## 当前接口

`memory_write` 及 `/memory write JSON路径` 只接受 `{"entries": [...]}`。旧单条顶层参数不再接受；`MemoryService.write_note` 已删除，调用者改为 `write_notes(entries)`。没有历史数据迁移或旧调用格式适配。

每条 entry 包含 `title`、`kind`，正文二选一：

- `body`：内联事实正文，必须提供非空 `sources`。
- `body_ref`：`ref` 为 `memory_search` / `memory_read` 可精确读取的原文引用；可带 `revision`、`view`、`offset`、`length`，按原文字符范围读取。省略 length 读取至末尾，越界明确拒绝，不截断。该引用自动加入 sources。

文件正文使用 `file_` 不可变快照；用户原文使用 `event_` 与 `view=content`；存档结果可用 `output_`；知识可用 `note_` 并指定当前 revision。`read_files` 的 `src_` 来源目录不是正文快照，不会把其中可变路径偷偷当作不可变正文。Office文档的新快照接缝由主任务另行对接。

示例：

```json
{
  "entries": [
    {"title": "设计全文", "kind": "architecture", "body_ref": {"ref": "file_..."}},
    {"title": "验收条件", "kind": "constraint", "body_ref": {"ref": "file_...", "offset": 210000, "length": 800}}
  ]
}
```

修改 entry 仍须 `ref` 与 `expected_revision`；默认 `status=unverified`，`shared` 仅控制新建作用域。取消旧32000字符正文和20个来源的硬门槛；标题200字符边界保留。返回 entries 元数据、body_chars 和 read_more，不回传大正文。真实完整正文保存到 notes / note_versions，全文可检索、导出及按原有分页接口读取。

## 完整性与原子性

全部条目先验证和读取正文，再在一个事务内校验目标 CAS、来源版本/存在性、active 文件当前性与依赖环；任一失败整批回滚，包括搜索派生表和失效传播。禁止同批重复修改同一引用。

需要用户确认时，prepare 阶段冻结实际解析的正文、范围、SHA-256和来源版本，预览显示短正文片段与完整正文哈希；execute 提交相同 prepared 批次。审批前正文来源发生变化会使预览摘要变化并触发 confirmation_invalid；审批后至提交间的来源版本变化则由事务内 CAS 拒绝。与主任务 F09 的 index_dirty 表对接：标脏 file_ 返回 current=false，active 事实的事务提交同时拒绝该路径，不能在后台索引更新前使用旧快照冒充当前证据。

body_ref 的原文、版本、范围和 SHA-256 记为可选 body_reference 元数据；内联正文保持现行内联条目结构。引用内容冻结到本条版本，源文档变化不会改写已保存正文；原有依赖失效传播将引用条目标为 stale，历史版本仍可精确读取。正文和标题继续走日志密钥清理器，遵循现行敏感内容处理。

工作区外原文不可读，file_ 读取继续受 CodeScope 限制。显式 shared 条目沿用项目共享语义；它会发布所保存的事实正文，不会自动放开其底层私有来源的读取权限。

配合主任务 F09，rebuild_search 在派生记录之间检查取消，取消抛 cancelled 并回滚事务，保留已有搜索投影；同时清理重建路径遗留的128000字符裁剪，避免全文在重建后失去尾部检索。

## 验证记录

- 首轮 `temp/F07-references-first.xml`：29通过/9失败。新增夹具2处参数重名、1处未确认写权限已修；6处索引空结果由主任务修复独立工作区处于外层Git忽略目录的过滤错误。
- 第二轮 `temp/F07-references-followup.xml`：38通过，25.96秒。
- 扩展回归 `temp/F07-references-final.xml`：39通过/2失败，失败为正在替换的维护 run_pending 旧调用，以及变更观察后未同步失效知识。主任务负责后两项修复。
- 接入 dirty 状态与审批冻结后，`temp/F07-references-approval.xml`：42通过/1失败，F07及新增审批/竞争/取消测试均通过；仅旧测试同步搜索尚未完成的后台新索引失败，主任务将更新为等待维护完成。该失败报告保留，不记为整组通过。
- 独立专项 `temp/F07-references-focused.xml`：25通过，22.92秒。范围：完整大文档跨20万分页读取、范围拆分、版本冻结及源变化失效、整批CAS回滚、源版本竞争、工作区/文件范围隔离、正文/来源数量放宽、密钥清理、CLI/工具新契约、审批期间来源变化拒绝、取消重建回滚、全文重建尾部检索，以及分页、工具契约与架构测试。
- 修复dirty来源复活后，`temp/F07-references-dirty.xml`：26通过，16.14秒，包含新增重建不发布dirty证据的回归。
- 最后补充“prepare后文件被标脏、commit前拒绝active事实”的事务竞争断言，`temp/F07-references-provenance.xml`：F07全部12项通过，5.57秒。

## 对F09接缝的并发审查

- `temp/audit-maint-e6ef6f07/report.json` 实证跨Maintenance实例取消只写DB，没有进度回调的合作式handler不能感知；同实例取消能感知。主任务负责独立取消信号修复。
- `temp/audit-cancel-lock-45cfbd00/report.json` 实证 cancel 先抢SQLite写锁会被重建长事务挡住，handler未感知取消，最终completed。主任务正在改为先发独立取消标记与本地Event，监测不依赖SQLite写锁，close重排与用户取消分开。
- `temp/audit-dirty-7053139f/report.json` 实证标脏来源被rebuild_search重新发布：搜索旧值从0条回到1条，但current=false。已修：重建同一事务查询中排除index_dirty，新增dirty来源不可复活及新快照完成后恢复检索的回归。
- `temp/audit-parser-36f20e23/report.json` 验证最新主任务代码的解析器版本快照：扫描中注册新parser不混用，下一轮自动重解析全部文件；此项通过，不登记缺陷。
- 主任务独立取消标记修复后，`temp/audit-cancel-fixed-2876fedb/report.json` 复验通过：跨实例取消正在持有SQLite写事务且无progress回调的handler，16ms返回，handler真实感知并终态cancelled。
- `temp/audit-close-6c077ea7/report.json` 实证close/restart竞态：close打断handler但其退出前enqueue→start清掉_closing，旧作业被标cancelled而非queued，且没有用户取消标记；新作业已completed。已报告主任务，待其固定每次执行的shutdown重排意图或禁止关闭实例重新start。

本子任务未调用真实API、未提交Git。最终Qwen Token Plan验收、全量回归及149项总审计由主任务统筹。
