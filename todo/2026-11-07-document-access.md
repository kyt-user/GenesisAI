# 文档与长篇内容实施记录（L01–L08、L10–L11）

这是一组实施与定向验证记录，不代表整轮149项最终核销。用户数据、旧报告和日志未删除，未提交Git。

| 编号 | 当前实现与证据 | 仍需核验或缺口 |
| --- | --- | --- |
| L01 | `inspect_ooxml` 统一所有OOXML入口的展开字节/成员数/压缩倍率防护；默认256MiB、100000成员、2000倍（大于1MiB成员）；重复名/加密容器拒绝。环境配置 `GENESISAI_DOCUMENT_MAX_EXPANDED_BYTES`、`MAX_ENTRIES`、`MAX_COMPRESSION_RATIO`（三者都有相同前缀）。DOCX/XLSX使用XML迭代器，通用字符页不再拼接整篇正文。5011成员和统一配额拒绝已测试。 | 第三方PPTX/部分写入库仍装载完整包；流式XML自身也保留解析树中的清空节点，非恒定内存承诺。资源上限不是已读取全文的保证。 |
| L02 | DOCX统一 `items`、`coverage`、`next`；section=body/paragraphs/tables/headers/footers，offset按单元，text_offset续读超长单元，next绑定源哈希。去掉创建总段落/段落正文/表格行数上限；create(source=...)追加；edit覆盖段落、表格、页眉页脚。3105段后尾部、205行表格、长段逐页重建及嵌套表格坐标实测。 | 只提取文字单元，不声称布局/图像/脚注/批注等所有OOXML语义；复杂文本框仍未承诺保真。 |
| L03 | 默认分页工作表目录；sheet/range指定区域，view=cells/formulas/styles/merges独立查询，所有非空XML单元可达，返回坐标/值类型/公式属性/样式ID。25表、BC1501、超过2100个公式/样式位置实测。 | 公式不重算；共享/数组公式引用明确返回属性，不伪造完整计算式；样式ID不是渲染验证。 |
| L04 | PPTX取消读取200页/创建100页总量边界，分页文字/表格/组合对象文字与备注，create(source=...)追加。105+100页生成与200页以后读取实测。 | 创建仍是基础标题/项目符号布局，图像/图表语义不属于文字提取。 |
| L05 | PDF按页/页内字符续读，205页尾部实测；空白或扫描页明确 `no_text_ocr_required`。新增独立 `pdf_ocr` 本地RapidOCR/PDFium，模型显式本地路径，不隐式联网；OCR页/页内游标绑定源哈希。新增 `/pdf-unlock 输入.pdf 新输出.pdf` 终端隐藏密码，错误密码无产物，正确密码结构重开与原子新建，密码不进入模型/会话参数。 | 合成两页扫描PDF实际识别成功，正确/错误密码真实PDF验证通过；OCR机器识别需核对原页，不承诺手写/任意语言/复杂排版准确率。CLI解锁暂是用户直接同步操作，未用持久作业保存密码。 |
| L06 | 删除每行120字符静默截断以及固定行数/每行正文schema上限；按实际字体宽度换行与分页，CJK字体用于基础文字；长行末尾与多页PDF重读实测。 | 字体覆盖和视觉效果仍不等于渲染验收，任意Unicode/复杂排版不承诺保真。 |
| L07 | 保留宏、外部关系、嵌入对象的有限编辑拒绝；新增 `.rels` 外链检测，公式未重算/未渲染的限制继续返回，实际结构重开后发布。 | 无新增OCR、Office计算引擎、视觉渲染验证，不扩大这些能力声明。 |
| L08 | 所有Office生成/编辑走共同发布：显式overwrite + expected_sha256，预览绑定目标和source哈希，临时产物格式/结构重开验证，写前哈希核对，旧文件备份与change记录后原子发布。Windows新建rename拒绝并发占位，POSIX同卷link；覆盖使用replace。覆盖冲突/备份原文/授权边界、真实Windows junction拒绝越界及工作区外显式output授权验证通过。 | 写前最终哈希与replace之间仍有极短外部进程竞争窗口，不能声称跨进程文件系统CAS；未扩大Access其他路径权限。 |
| L10 | chapter_read单页最多50000字符，默认按任务上下文预算调页，流式字符解码；目录扫描没有新增总文件数上限，实际1005章节目录计数/尾章验证通过。 | chapter_list仍全目录扫描，超大目录效率与复杂链接环策略需单独评估。 |
| L11 | 章节按块输出原文段落片段、人物/显式年龄候选、覆盖范围、源哈希、next；novel_facts作为会话来源持久化。跨章事实表按当前文件哈希过滤过期数据、合并覆盖区间，支持fact_offset/fact_limit；显式年龄差异附原始证据，只提示需人工/时间线判断。重开会话后跨章证据有效、源变化剔除旧事实、后5000字事实可达测试通过。 | 事实抽取是有边界的启发式（提及、明确年龄），不是模型级全书语义一致性证明；事实表范围明确是本会话已覆盖章节，未扫描章节从不标为一致。 |

## 新接口与调用方

- Office读接口统一返回 `items`，不保留旧 paragraphs/sheets/slides/pages 映射。每条item含单元offset、text_offset、total_chars，next可直接作为下一次工具参数。
- read_files支持PPTX及上述文档的流式字符页，输出coverage/ocr/rendering边界；文件引用仍是实时文件src_加哈希，**不是可交给memory_read的不可变正文快照**。
- Office工具Schema、tool.yaml、office协议、产品模拟调用方和现有测试同步更新。Access.write仅增加正式可选overwrite参数，其余调用维持默认拒绝覆盖。

## 验证

- 初始回归28通过3失败：Office新建使用Windows硬链接受环境拒绝，改为Windows原子rename；两项索引计数与同时进行的后台索引实现有关，已告知主代理，不修改对方测试。报告 `temp/document-initial.xml`。
- 一次专项8项setup错误为并行开发中Maintenance构造与ToolRuntime接缝尚未同步；保留 `temp/document-paging.xml`，未算通过。
- `temp/document-followup.xml`：18通过，16.65秒。
- `temp/document-novel-regression.xml`：27通过，52.32秒。
- `temp/document-final.xml`：15通过，35.09秒，含产品模拟及架构。
- `temp/document-audit-final.xml`：24通过，90.29秒，含外部关系拒绝、配额与产品模拟。
- `temp/document-read-followup.xml`：2通过，PDF空页标记与XLSX合并范围；嵌套表格坐标1项独立通过。
- `temp/novel-read-followup.xml`：2通过；`temp/document-create-limits.xml`：真实1001段DOCX和5001行PDF创建通过。
- `temp/document-overwrite-links-verified.xml`：4通过，含真实Windows junction。普通沙箱拒绝创建junction的失败报告保留，受控环境复验通过。
- Qwen真实DOCX尾页入口 `tests/manual/live_document_pages.py`，强制qwen_token_plan，只生成隔离3106段文档，实际调用docx_read(offset=3100)核对随机标记。前两轮 `live_document_pages_25fcc50d0c33445b`、`live_document_pages_8dca5bb0c02a412a` 只因测试断言不接受来源引用或重复同一正确随机码而失败，保留原报告。第三轮 `live_document_pages_80dabcbe04184922` 找到真实B09错误：SHA256被误认为CAD价格，详见 `2026-11-07-price-identifiers.md`。第四轮 `live_document_pages_36d68ea583de40c8` 为沙箱网络/时限异常失败，已终止本测试进程并保留观察报告。
- **真实API通过**：`temp/live_document_pages_5ac4ce27fd4a4768/report.json`，qwen_token_plan/qwen3.8-flash，completed、actual_tail_page、observed_marker、original_preserved全部true，未向模型提供验收码答案。
- `temp/local-media-documents.xml`：初轮2通过2失败（测试快照属性及PDFium页对象关闭方式）；修复后 `temp/local-media-documents-followup.xml`：4通过8.70秒，包括真实离线OCR、加密PDF、FFmpeg抽帧与音轨提取。没有把接口mock当本地引擎通过。
- `temp/document-local-final.xml`：36通过、1跳过（普通沙箱禁止创建junction，已有提权通过证据）、1排除（media注册接缝待接）。
- `temp/ocr-job-verification.xml`：1通过12.30秒，实际OCR独立子进程、read_job收取与来源导入。
- `temp/document-cli-architecture-followup.xml`：24通过26.75秒，CLI与层级架构验证。
- `temp/local-media-job-verification.xml`：14通过2失败；media目录尚未接入registry导致unknown_tool，另一个既有测试因E01将异常契约从execution_error变job_error，已通知主代理；未改对方契约测试。
- `temp/media-registry-price-verified.xml`：31通过22.15秒，修复media类别注册，实际媒体后台作业产物/来源/change导入，新增小数标识符价格边界验证。
- `temp/local-speech-real.xml`、`temp/local-speech-retry.xml` 保留沙箱SAPI无法选择语音引擎的失败；受控环境 `temp/local-speech-verified.xml`：2通过3.52秒，实际Windows SAPI合成语音、CPU int8转写、read_job收取及关键词断言，不是mock转写。
- `temp/document-media-complete.xml`：联合100项中99通过、1失败（71.85秒）。唯一失败read_files作业跨越主代理E01并行修改，老进程提交的作业数据没有新network字段，读入时KeyError；当前代码单项复验 `temp/document-catalog-seam-recheck.xml` 1通过1.89秒。未增加历史格式兼容层。
- 最终联合回归实际包含真实Windows junction、扫描件OCR、FFmpeg、Windows SAPI与Whisper本地转写，没有跳过这些实测项。工具结果中的实际转写为 “The blue bicycle is beside the quiet river. Please remember the number 7.”，保留于 `temp/speech-b4d7f79a/test_real_synthetic_speech_thr0/state/jobs/session_d9d30a17afe04169/job_c24d644dce0b49b2/result.json`。

## 统一异步接缝

主代理已提供统一JobService文件工具执行接缝，并将Office全部spec（含pdf_ocr）设为execution=job；新增媒体工具同样opt-in。5秒前台窗口后返回job_id，由read_job收取正文/来源/产物/修改记录。未修改jobs或ToolRuntime建立第二个宿主。专项作业验证仍在进行，以主代理E01最终报告为准。

## L09 当前媒体分界

新增本地 `media_probe`、`video_frames`、`media_audio`：只调用绝对PATH找到的ffmpeg/ffprobe，固定argv、本地file/pipe协议与自包含容器解码白名单，不接收URL/播放列表。抽帧按时间片生成PNG，可交read_image查看；音轨按时间片生成16kHz单声道WAV，结果明确不是转写。输出仅新路径、结构验证、产物引用与change记录。原提示中禁止尝试音视频的绝对禁令替换为实际工具边界。

新增 `audio_transcribe`：faster-whisper CPU int8、显式绝对 `GENESISAI_WHISPER_MODEL`，完整本地模型文件检查，禁止隐式下载，独立后台作业。按时间片和片内文字继续，源SHA绑定，返回时间戳/语言概率/识别指标/模型哈希，不承诺说话人分离与任意语音准确率。实际Windows SAPI合成英语句子经真实模型识别出bicycle与river，read_job收取与来源导入通过。

本地测试用 [官方tiny多语模型目录](https://huggingface.co/Systran/faster-whisper-tiny/tree/main) 约78.2MB存放 `temp/models/faster-whisper-tiny`，不入Git；model.bin SHA256为 `dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1`，下载报告保存在该缓存目录，核对官方公布值。包版本faster-whisper1.2.1、CTranslate2 4.8.5。静态图片解码/裁剪仍属于主代理对应实现，本分支不声称新增动态图像理解。

OCR依赖依据 [RapidOCR官方安装说明](https://github.com/RapidAI/RapidOCRDocs/blob/main/docs/install_usage/rapidocr/install.en.md) 与 [PDFium官方API](https://pypdfium2.readthedocs.io/en/stable/python_api.html)；已安装rapidocr3.10.0、onnxruntime1.31.1、pypdfium2 5.15.0，使用包内3个模型显式路径。没有新增云OCR服务。
