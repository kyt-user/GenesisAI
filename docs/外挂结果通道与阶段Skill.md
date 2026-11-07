# 外挂结果通道与阶段 Skill

## Tool：结果与日志

程序从 stdin 读取参数对象，并把唯一的 UTF-8 JSON 结果写入 `GENESISAI_RESULT_PATH`。stdout 与 stderr 只记录诊断日志。两条日志通道保留完整内容，按作业日志额度约束；结果按 capability 资产导入额度约束。不存在 stdout JSON 兼容读取路径。

```python
import json, os, sys
from pathlib import Path
args = json.load(sys.stdin)
print("开始处理", file=sys.stderr)
Path(os.environ["GENESISAI_RESULT_PATH"]).write_text(
    json.dumps({"result": args["value"] * 2}, ensure_ascii=False, allow_nan=False),
    encoding="utf-8",
)
```

`output_schema` 和测试 `expect` 针对原始 JSON 值校验，支持对象、数组、字符串、数字、布尔值和 null。结果中的重复对象键、NaN、Infinity、非 UTF-8、超额度文件及符号链接/目录联接被拒绝。完整 schema 校验和哈希复核通过后，才发布不可变资产。

调用结果外层统一包含 `value_included`、`result_ref`、`logs`、`result_bytes`、`sha256`。当 `value_included=true` 时存在 `value`，它可以是 null、false、0 或空数组；超过内联页时省略 `value`。使用 `memory_read(ref=result_ref, offset=..., limit=...)` 读取正文，续页以 `next_offset` 为准。日志同样使用 `logs.stdout` 和 `logs.stderr` 引用分页；失败作业返回失败状态与日志引用。

结果在 capability 内容寻址资产库中保存，不复制进知识笔记。同一作业重复收取复用同一 artifact/source 引用。旧作业对照其冻结修订，升级当前注册版本不影响旧结果。首次收取核验作业归属和执行授权；已收取历史结果按当前会话、工作区访问权限和不可变哈希读取。授权撤销不改写已有结果。`read_capability_artifact` 也执行结果归属检查。

## Skill：阶段依赖图

内置 manifest 使用 `name/description/entry/stages`；外挂 definition 使用 `entry/stages`。每阶段须同时声明三个列表：

```yaml
stages:
  inspect:
    depends_on: []
    required_tools: [read_files]
    optional_tools: [pdf_ocr]
  edit:
    depends_on: [inspect]
    required_tools: [editor]
    optional_tools: []
```

`use_skill(name, stage)` 加载指定阶段及前置阶段闭包。必需工具缺失会报错；可选工具缺失通过 `missing_optional_tools` 报告。没有总工具数上限；重复声明、未定义前置阶段和循环依赖会被拒绝。同一工具在闭包中被必需阶段声明时按必需处理。

会话 `active_skills` 保存 `{名称: 阶段}`。加载另一阶段替换该 Skill 当前阶段，不累计任意工具。CLI 使用 `/skills load bug_fix run`；内置 Skill 统一提供 run 阶段。外挂测试 `input.stage` 必须指向包内阶段。没有旧扁平 tools 或旧会话列表迁移。

## 隔离宿主服务适配

宿主开发者通过 `adapters(runtime).register(tool_name, "module:function")` 注册顶层工厂。工厂只接收 `TrialServices`，其中含独立 trial runtime、workspace 以及该工具声明的权限。启动数据只保存工厂导入标识和工具描述，不保存生产服务实例、绑定方法或闭包。

参考工厂 `genesisai.core.capabilities.trial_adapters:memory_search` 重建只读 MemoryService 查询适配器，绑定新 trial 的记忆库。每次 Skill trial 都创建新服务实例。未注册工厂的宿主依赖不能冒充已经隔离；递归构建/测试/注册能力仍被禁止。试运行工具只限选定阶段闭包及完成、作业读取/停止工具。
