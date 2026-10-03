---
description: @cline/llms 包的开发指导。
globs: "src/**/*.ts,src/**/*.tsx,*.md"
alwaysApply: true
---

# @cline/llms 开发指导

## 提供商选项路由（Provider Option Routing）

- `models.dev` 目录数据与 AI SDK 提供商行为是「常规模型/提供商支持」的默认事实来源。
- 不要构建宽泛的、由 Cline 维护的「模型能力或行为注册表」。
- `GatewayModelCapability` 是语义性的：描述模型能做什么，而不是提供商的怪癖、默认行为或线格式（wire format）细节。
- 稳定的「已知模型事实」应放在类型化的 `ModelInfo.metadata` 辅助工具或 `src/providers/model-facts.ts` 中。
- 稳定的「提供商路由事实」应放在 `GatewayProviderMetadata.routing` 与相关内置提供商 manifest 中。例如，若某个原生提供商为某条模型路由使用已知的推理线格式，请添加类型化的 `GatewayReasoningFormat` 值及路由元数据，而不是在规则谓词中直接匹配该提供商 id。
- 提供商线格式编码应放在 `PROVIDER_OPTION_RULES` 与 `src/providers/routing` 下的 codec 辅助工具中。
- 本地或动态的提供商回退（例如 Ollama 或路由模型 id 启发式）仅可作为「有文档、范围窄、有测试」的例外使用。
- 回退启发式需要负面测试或优雅降级测试，而不仅是理想路径（happy-path）测试。

修改提供商选项时，请保持职责拆分显式：

```text
请求意图 + 模型/提供商事实 -> 命名的提供商选项规则 -> 提供商线格式
```

若多个提供商需要同一套事实解析逻辑，请先使用共享辅助工具。只有重复出现的逻辑证明「规则表与辅助工具已不再够用」之后，才新增注册表。