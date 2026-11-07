# B09 计算型价格证据（实施中）

接口 `calculate_price` 支持 `sum`（同币种费用合计）、`multiply`（单价×明确数量）、`convert`（金额×方向明确汇率）。每个操作数包含type/value/source_ref/quote；金额需currency，汇率需from_currency/to_currency。数值使用Decimal字符串，可含规范千分位；不用float，结果显式ROUND_HALF_UP舍入至0–8位小数。

证据约束：

- source_ref必须已在当前会话登记且未过期。网页正文按保存哈希验证；文件引用需仍在当前授权范围、哈希匹配，并流式查找逐字quote；派生来源重验上游证据与实际运算。
- source_ref=user仅表示当前用户原消息给定值，绑定消息索引与正文哈希。数量必须有明确数量/单位，汇率必须有类似 `1 USD = 7.2 CNY` 的方向，不能把任意数字当系数。
- 结果保存kind=price_calculation的结构化来源，包含操作数证明、公式、币种、舍入与依赖。恢复门禁只接受可重验结果，不接受答案里的任意算式。用户给定值必须在最终答案标注“基于用户给定前提”等表述，不赋予市场实时报价身份。
- 新会话不能直接复用另一会话的派生来源，需重新登记授权来源并计算；原会话重开仍可验证。没有历史接口或旧格式迁移。

文件：shared/prices.py、shared/price_evidence.py、core/price_calculations.py、kernel/calculate_price；agent/recovery仅价格解析、验证和补证据提示变更。未改research.py，web协议提示由对应代理同步。

验证尚未完成，不核销B09：

- 新测试 `tests/test_price_calculations.py` 覆盖金额合计/数量/汇率、千分位/负数/舍入/超float精度、来源变化、授权收紧、用户哈希、跨会话和篡改结果；等待宿主稳定后执行。
- `temp/price-decimal-parser.xml`：23通过8失败；纯标识符/金额测试通过，8项既有网络fixture因后台未取得evidence_refs而IndexError，主代理集中排查异步宿主，不将失败归为价格已通过。
- 主代理要求暂停所有新测试/安装以排查宿主失联及系统负载；既有句柄63759、95796、19803已结束，未重放未知作业。

关联复验：`temp/document-bootstrap-verified.xml` 14通过1失败，ASR配置未传入；主代理/授权代理补齐明确配置后 `temp/asr-bootstrap-config-verified.xml` 1通过1失败，已进入source阶段但job宿主心跳失联。失败证据保留，未计通过。
