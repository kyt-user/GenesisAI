from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('local_files', 'testing', 'coding', 'git_read', 'debugging'),
    parameters=object_schema(
        [],
        operation={"type": "string", "enum": ["run", "inspect"]},
        command={"type": "array", "description": '一次执行一个程序。第 0 项为程序路径，之后每项为一个参数，例如 ["flutter", "--version"]。多条命令分别调用。Windows 内置命令须显式使用 ["cmd.exe", "/d", "/c", "echo hello"]。',
                 "items": {"type": "string", "minLength": 1, "maxLength": 4000}, "minItems": 1, "maxItems": 64},
        background={"type": "boolean"},
        cwd={"type": "string", "minLength": 1, "maxLength": 2000},
        timeout_seconds={"type": "integer", "minimum": 1, "maximum": 600},
    ),
    permission="shell",
    path_scope="workspace",
    side_effect=True,
    timeout_seconds=605,
    max_output_chars=24000,
)
