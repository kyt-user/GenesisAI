# B09 标识符误判专项

真实 Qwen DOCX 验收失败 `temp/live_document_pages_80dabcbe04184922/report.json`，会话 `session_6eb41766172043eb` 已成功读取第3105段随机码；模型在答案附带源 SHA256 时触发价格门禁，最终被恢复逻辑改写为“本轮没有取得可核实的价格资料”。

误判证据：原 PRICE_CLAIM 对 SHA256 `1ea072356b5a2323e960e83c5c21c3a516ca91cf5cce2742226f89cadbc064ee` 提取为 `CAD 89`；`MARKER_123cad456789abcd` 提取为 `CAD 123`。币种代码与数字缺少 ASCII 标识符边界，既不是工具没有读到末段，也不是文档正文真的包含价格。

修复仅在 `agent/recovery.py` 增加币种代码/金额的 ASCII 标识符边界。保留 USD9、9USD、$9.99、人民币10元等真实价格，以及来源正文支持/错误币种拒绝机制；不按任务主题粗暴禁用价格校验。

验证：

- `temp/price-identifier-regression.xml`：78通过，64.875秒，包括真实日志同款SHA与随机码、段落偏移、真实金额、Runner答案完成、无来源价格拒绝、来源支持与错误币种拒绝。
- 补充审计发现小数匹配可回退至数字前缀（如USD9.99identifier），已将金额模式改为完整数字分组并禁止在未结束的小数中回退。`temp/media-registry-price-verified.xml` 31项通过包含新边界测试；最终联合回归中全部价格测试通过。
- `temp/live_document_pages_5ac4ce27fd4a4768/report.json`：Qwen Token Plan / qwen3.8-flash 真实调用通过。3106段隔离DOCX，提示没有随机验收码答案；实际 docx_read(offset=3100) 返回尾段，最终答案随机码正确，原文件保留。
- `temp/live_document_pages_36d68ea583de40c8/report.json`：普通沙箱网络受阻或超时未观察到的独立失败保留。超过10分钟只持久化首轮请求，受控结束本测试PID后重跑；不将该轮算API成功。主代理已获知请求时限异常待核验。

此记录只核销 B09 的标识符假价格阻断。**推导/计算型价格的证据表达及核销仍未完成**，不能把B09整项自动判为完成。现有证据及日志未删除。
