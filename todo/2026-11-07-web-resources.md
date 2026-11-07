# G03/G07–G09/G11–G13：完整网页源、分页与统一网络预算

本轮按原149项审计目标替换实现；不保留旧截断、旧provider返回list接口或旧参数兼容层。总台账、用户指南、全量审计和Git由主代理整合。

## 已实现

- G03：GET临时状态408/425/429/500/502/503/504按指数退避、抖动和Retry-After重试，受一次操作的总时间预算约束，无两次硬上限。永久HTTP失败不自动再请求；外部状态确已改变时显式refresh。取消不将URL永久封禁。
- G07：NetworkLimits统一抓取、搜索、重定向和退避预算。默认总120秒、单次阻塞IO30秒、最多10跳；每跳重新解析并固定已校验公网IP，跨域只保留非认证通用头，同源可保留凭据。
- G08：响应默认32MiB，解码默认64MiB；network_limits可配置总时间、IO、字节、跳转、退避资源边界，超额明确失败，不返回貌似完整的前缀。按块读取和受限解压；gzip多member完整处理，Brotli使用1.2.1的有界输出接口。
- G09：完整原始资源存为sources/{source_ref}.bin；元数据含长度、哈希、媒体类型。HTML原文、全文提取和PDF全部页均保留；取消20万字符、PDF30页总截断。text/source/links/structured/structured_raw/document_pages/page各区可分页，Research缓存读取共用page_source。
- G11：SearchQuery与schema同为500字符，移除额外50词限制；result_count 1–100、offset、provider、语言/国家/新鲜度/域名过滤显式化。Provider统一返回SearchPage(hits,next_offset,coverage)，不推测存在下一页。Brave按官方20条/页和最多10页处理，DuckDuckGo根据响应Next表单继续，Bing RSS只在实际完整RSS响应内分页并标明coverage，不假装其支持服务端翻页。
- G12：全部HTTP(S)链接可发现，保留长URL和跨域引用，按links区域分页；默认无固定两层登记上限，可用discovery_depth控制本轮登记深度、include_cross_domain筛选。发现仅记录溯源，实际访问仍经授权与地址检查。
- G13：HTML/JSON-LD无独立前缀裁剪、无20事实总截断。每个JSON-LD/meta块保留raw、facts、解析状态；可按block与fields定位，块原文按字符范围读取，损坏块也有完整引用。主正文摘要继续优先JSON-LD报价，meta冲突数据保留在独立块中。

## 异步与授权接缝

fetch_web_content使用execution='job'，前台短等待后返回作业引用；长操作由统一作业槽位托管，源记录以独立作业delta收取。后台不覆写宿主run_runtime。Search失败返回PartialToolResult及diagnostics，宿主Research负责记录。

Research对queued/running/stopping/unknown提交不计检索/抓取或证据；read_job最终结果携origin_call、result、operation_error后，按原调用记账并按作业/调用去重。完成收取前不能当作已获得正文。

域名范围协议为None或{authorization_id, domains}；exact精确匹配、*.example.org仅匹配子域、*表示显式任意域名。Network(...,scope=...)及web.budget.active_scope对每跳请求域名校验；所有scope取交集，跨worker/job保留授权。即使domains=['*']也不允许私网、保留地址或混合DNS。

## G06/G10：已实现并完成专项验证

- fetch_private_resource 是单独入口，要求显式会话/任务授权或具体调用确认。域名与CIDR同时校验，每跳重新解析并固定IP，禁止特殊地址；缓存也重新检查有效授权。授权记录没有自动过期，不以等待时间使批准失效。
- render_web_page 显式启动独立 Chromium，以受控公网GET读取主文档/子资源，禁用Service Worker/WebSocket/写请求；范围、统一时间和可配置字节/请求预算均显式。完整DOM与正文保存为web_render来源，清楚标注时点快照与重定向限制。普通HTTP读取不隐式执行JS。
- pdf_ocr 可接受当前会话的下载source_ref，经来源种类、固定目录、原始文件哈希/长度和PDF类型校验后进行本地OCR；页范围与实际OCR覆盖明确。
- temp/private-protocol-final.xml：25项通过，含真实本地HTTP权限/撤销/重定向，实际PDF OCR与协议完整性。
- temp/browser-render.xml：16项通过，1项仅测试把正式stopped状态写成cancelled；修正后套件与浏览器联合33项通过，剩余两项属于其它测试断言，浏览器三项均通过。
- temp/live_browser_18eabae07acf41d1/report.json：真实Qwen Token Plan + 实际Chromium，9项通过；网络内容为隔离夹具，非公网可达性宣称。

## 实测记录

- temp/web-network-first.xml：5通过、2失败；查询参数扩展时误改object.__setattr__参数，已修正。
- temp/web-resources-first.xml：8通过，4.93秒。覆盖大HTML/JSON-LD、350链接、35页PDF、四次重试、取消、跨域剥离凭据、可配置解压和Brave分页。
- temp/web-regression-first.xml：105通过、4失败、1跳过。两项网页旧期望已更新；工具job_error接缝由主代理修复；Windows junction创建被环境拒绝，未伪称通过。
- temp/web-complete-regression.xml：148通过、1失败、2未选；唯一失败是文档组旧OCR抛错断言，document_access已同步修正并单独通过。
- temp/web-provider-contract.xml：30通过，19.63秒。
- temp/web-paging-regression.xml：118通过、2未选，114.25秒。
- temp/web-async-accounting.xml：2通过，1.48秒，验证最终源去重记账与失败诊断收取。
- temp/web-real-smoke-sandbox.json：公网受沙盒网络限制，总预算明确终止。
- temp/web-real-smoke.json：实际Python官方asyncio页面成功，完整25411字节源、39链接；DDG连接不可达；Bing两次忽略offset返回同样10条，促使实现改为明确RSS响应内分页。未伪称DDG在线可用或Bing服务端翻页通过。
- temp/web-job-scope.xml：切换job后8通过、5失败；前两项发现内置Network中的测试回调跨进程序列化缺少模块；后面遇到主代理同步升级worker协议的中间态（expected11 got10）。正在协调接缝，未计为已完成异步端到端验收。
- temp/web-network-scope.xml：7通过，1.32秒；每跳域名scope、SSRF、完整gzip/Brotli预算与取消通过。
- temp/web-job-verified.xml：12通过、1失败，18.45秒；异步慢抓取/最终源去重、完整HTML/PDF均通过；失败是不可pickle的httpx.Client测试夹具，改为纯Network进程夹具，未添加生产兼容回退。
- temp/web-real-smoke-v2.json：真实Python官方网页再次通过；Bing RSS分页3+3条且第二页新增3条，明确complete_rss_response_only；DDG本机连接仍不可达。
- temp/web-runtime-live.json：真实ToolRuntime作业读取官方文档通过，完整25411字节源保存，缓存links偏移30返回9/39条，fetch计数1、evidence_ref仅1个。
- temp/web-job-regression.xml：94通过、14失败。旧夹具依赖宿主对象可变计数，已改为跨进程文件记录；授权确认链的剩余旧expires判断由授权代理修复，没有放松digest或资源校验。
- temp/web-job-approval.xml：19通过、1失败，62.09秒；唯一失败来自测试把宿主全局时间跳到未来901秒，导致真实子进程心跳被判失联。改为调整确认暂停起点模拟等待，保留真实作业时钟。
- temp/web-last-network.xml：5通过；temp/web-architecture-final.xml：架构检查通过。

历史扩大回归曾发现共用job宿主结果发布异常：作业已有source/effects，job.json却同时status=running与finished，且没有result.json；具体目录与日志已报主代理，暂停新增运行时回归等待修复。例：temp/web-fa73b201/test_complete_html_source_link0/app/jobs/session_ce1975c31cc44f31/job_9b43a2e376854dd6。不得据此把未收取结果当证据或重发原操作。此缺口已在结果发布与协议脱敏修复中关闭，最新证据见2026-11-07-async-controls.md；历史失败现场保留。

协议参考：[Brave分页契约](https://api-dashboard.search.brave.com/app/documentation/web-search)、[HTTP Retry-After语义](https://datatracker.ietf.org/doc/html/rfc9110)。异步端到端与真实公网验收记录见上述报告，整轮最终全量结果统一登记在2026-11-07-final-audit.md。
