from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('skill', 'local_files'),
    parameters=object_schema(
        ["name", "stage"],
        name={"type": "string", "minLength": 1, "maxLength": 64},
        stage={"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$", "description": "要加载或卸载的明确阶段，内置 Skill 使用 run"},
        operation={"type": "string", "enum": ["load", "unload"], "description": "按阶段持久加载或卸载 Skill；默认 load"},
    ),
    permission="core",
    path_scope="none",
)
