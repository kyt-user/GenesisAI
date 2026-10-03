# 提供商 VCR 测试夹具（fixtures）

这些 cassette 在常规测试运行中无需凭据即可驱动 `@cline/llms` 中真实的提供商适配器。

在 `sdk/` 下运行回放：

```sh
bun -F @cline/llms test:vcr
```

使用本地提供商凭据刷新 cassette：

```sh
LLMS_PROVIDER_VCR_RECORD=1 bun -F @cline/llms test:vcr
```

按提供商 id 或 cassette 名称刷新单个 cassette：

```sh
LLMS_PROVIDER_VCR_RECORD=1 LLMS_PROVIDER_VCR_TARGET=cline bun -F @cline/llms test:vcr
```

录制模式优先使用常规的 Cline CLI 提供商设置路径。若要使用其他文件，请设置 `LLMS_PROVIDER_VCR_SETTINGS_PATH=/path/to/providers.json`。
`ANTHROPIC_API_KEY` 与 `CLINE_API_KEY` 可在没有设置文件的情况下使用，但 ChatGPT OAuth 需要已保存的 `openai-codex` 提供商设置。本地 Cline API 录制还需设置 `CLINE_ENVIRONMENT=local`。

录制后，测试会规范化动态响应字段，例如响应 ID、加密的推理载荷、prompt 缓存键与安全标识符。