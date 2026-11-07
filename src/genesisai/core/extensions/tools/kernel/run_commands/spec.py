from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('local_files', 'testing', 'coding', 'git_read', 'debugging'),
    parameters=object_schema(
        [],
        command={"type": "array", "description": '一次执行一个程序。第 0 项为程序路径，之后每项为一个参数，例如 ["flutter", "--version"]。多条命令分别调用。Windows 内置命令须显式使用 ["cmd.exe", "/d", "/c", "echo hello"]。',
                 "items": {"type": "string"}, "minItems": 1},
        background={"type": "boolean"},
        verification={"type": "string", "enum": ["build", "static"],
            "description": "将显式构建/静态检查命令的真实退出状态记录为验证证据；测试命令另按测试框架统计，不得把未执行的检查声明为通过"},
        write_paths={"type": "array", "items": {"type": "string", "minLength": 1},
            "description": "声明可能写入的工作区路径，用于冲突排队；省略时协调整个 cwd，纯只读命令或服务可显式传 []。这不是文件系统沙箱。"},
        interaction=object_schema(['mode'], mode={'type': 'string', 'enum': ['pipe', 'pty']},
            rows={'type': 'integer', 'minimum': 1, 'maximum': 32767},
            columns={'type': 'integer', 'minimum': 1, 'maximum': 32767}),
        cwd={"type": "string", "minLength": 1, "maxLength": 2000},
        env_from={"type": "object", "additionalProperties": {"type": "string"},
                  "description": "明确授权的子进程变量名到宿主环境变量名映射；参数不包含密钥值"},
        timeout_seconds={"type": "integer", "minimum": 1, "description": "可选执行总时限；超过短响应窗口的命令转为可取消持久作业"},
    ),
    permission="shell",
    path_scope="workspace",
    side_effect=True,
    timeout_seconds=605,
    max_output_chars=24000,
)
