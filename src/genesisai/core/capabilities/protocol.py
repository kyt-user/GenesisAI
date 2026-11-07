"""A versioned draft carries data, files and executable acceptance examples."""


def protocol():
    return {
        "workflow": "先查看已有能力并确认需求，create_tool/create_skill/create_mcp 创建草稿；文件用 path 引用工作区实现，revise_capability 增量更新；freeze_capability 校验并冻结当前版本，test_capability 通过后展示报告，用户可 try_capability 试用；最后 register_capability 请求用户验收。禁止把静态检查或草稿生成描述为已注册。",
        "package": {"kind": "tool|skill|mcp", "name": "2–48 个小写字母/数字/下划线，以字母开头",
                    "description": "清楚描述用途和副作用", "scope": "project 或 user",
                    "definition": "按下列类型定义", "files": {"包内相对路径": {"path": "工作区源文件路径（文本或二进制）"}}, "tests": "至少一个预期成功的实际执行用例"},
        "tool": {"definition": {"runner": "python", "entry": "tool.py", "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"], "additionalProperties": False}, "output_schema": {"type": "object", "required": ["result"]}, "timeout_seconds": 300, "routes": ["coding"], "network": False},
                 "execution": "runner=python 使用 entry；runner=command 使用命令参数数组（支持 {python}/{bundle}），可运行 Node 或其他程序。程序从 stdin 读取一个 JSON 对象；唯一结果通道是环境变量 GENESISAI_RESULT_PATH 指向的 UTF-8 JSON 文件，支持对象、数组、字符串、数值、布尔及 null；stdout/stderr 只作日志，不从日志推断结果。测试 expect 检查原始 JSON 值；正式返回 value_included/value/result_ref/logs，超出内联页时只给不可变引用，可用 memory_read 分页。日志与结果分别计配额，结果受资产导入配额约束。cwd 是授权工作目录，包内辅助文件通过 __file__ 定位；不向程序传递宿主 Session/Runtime。默认仅继承基础系统环境；definition.env_from 显式映射子进程变量名到宿主变量名，值不进入能力包，测试和正式执行使用同一映射。运行遵守命令执行审批。",
                 "files": {"tool.py": {"path": "draft/tool.py"}},
                 "tests": [{"name": "double", "input": {"value": 3}, "expect": {"type": "object", "properties": {"result": {"const": 6}}, "required": ["result"]}}]},
        "skill": {"definition": {"entry": "SKILL.md", "stages": {"run": {"depends_on": [], "required_tools": ["read_files", "editor"], "optional_tools": []}}},
                  "execution": "使用真实模型和独立测试 Session 运行 input.prompt。input.stage 指定阶段，依赖图先加载前置阶段；缺必需工具时报错，缺可选工具明确报告。使用 use_skill(name, stage) 加载。只开放该阶段依赖闭包中的可用工具及完成/作业读取工具。宿主服务需显式注册 module:function 隔离工厂，生产服务实例不进入测试；禁止递归构建能力。结果为 status/answer/artifacts；artifacts 按相对路径列出不可变产物引用 id、sha256、size、kind 与格式元数据，不内联正文。测试要求 status=completed，可用 expect 核对文本/二进制哈希、大小、图片格式与宽高；read_capability_artifact 按 id 分页读文本或 base64 字节。文本不限20万字节，总文字不限50万字符；受可配置资产存储/导入配额约束。",
                  "tests": [{"name": "write greeting", "input": {"stage": "run", "prompt": "按 Skill 生成 greeting.txt"}, "expect": {"type": "object", "properties": {"artifacts": {"type": "object", "properties": {"greeting.txt": {"properties": {"sha256": {"const": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}, "kind": {"const": "text"}}, "required": ["sha256", "kind"]}}, "required": ["greeting.txt"]}}, "required": ["artifacts"]}}]},
        "mcp": {"definition": {"transport": "stdio", "command": ["{python}", "-B", "{bundle}/server.py"], "read_only": True, "env_from": {}, "request_timeouts": {"connect": 30, "first_response": 300, "idle": 300, "total": None}},
                "execution": "MCP stdio 服务，可创建服务源码或配置已有服务。{python} 是当前解释器，{bundle} 是该修订包目录；env_from 映射服务变量名到宿主环境变量名，不保存密钥值。测试要完成 server/discover、工具发现与一次实际调用。远程服务直接使用 transport=streamable_http/url/read_only/headers_from 定义，无需生成代理；headers_from 将 HTTP 头名映射到宿主环境变量名。支持 MCP 2026-07-28，每请求携带协议元信息、resultType=complete；不使用 initialize/会话头/GET恢复，不重发结果未知的调用。",
                "tests": [{"name": "echo", "input": {"method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hello"}}}, "expect": {"type": "object", "required": ["text"]}}]},
        "test_cases": "不限用例数量；fixtures 使用 path 文件引用，支持二进制。每项含 name/input 和 expect(JSON Schema) 或 expect_error(错误码)，可用 fixtures 提供独立测试文件。准备有代表性的成功、边界和错误用例，验证真实需求。只运行失败用例不足以注册。",
        "incremental": {"create": "只需 name/description，scope 默认 project；definition/files/tests 可稍后补充",
                        "revise": {"id": "cap_...", "expected_revision": 1, "changes": {"files": {"tool.py": {"path": "draft/tool.py"}}, "tests": [{"name": "case_name", "input": {}, "expect": {"type": "object"}}]}},
                        "merge": "definition 按字段合并，null 移除字段；files 按路径更新，null 仅移除当前草稿引用；directory 导入目录快照；tests 按唯一 name 更新，remove_tests 移除用例。原始用户文件和旧修订保留。",
                        "assets": "文件按 SHA-256 保存，引用返回 sha256/size。默认单文件导入512MiB、资产库5GiB，可由宿主 capabilities/limits.json 配置；没有整包2MB或100文件限制。"},
        "versioning": "每次修改生成新修订和 digest；旧注册版本在新版本验收前保持可用。不得直接改写执行包目录。测试报告与用户验收绑定同一修订、摘要和报告。",
    }
