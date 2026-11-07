from genesisai.core.tools.base import ToolSpec, object_schema

ENTRY = object_schema(
    ['title', 'kind'],
    title={'type': 'string', 'minLength': 1, 'maxLength': 200},
    kind={'type': 'string', 'enum': ['decision', 'constraint', 'experience', 'architecture', 'hypothesis']},
    body={'type': 'string', 'minLength': 1, 'description': '事实正文；与 body_ref 二选一'},
    body_ref={**object_schema(['ref'],
        ref={'type': 'string', 'minLength': 1},
        revision={'type': 'integer', 'minimum': 1},
        view={'type': 'string', 'enum': ['record', 'content']},
        offset={'type': 'integer', 'minimum': 0},
        length={'type': 'integer', 'minimum': 1}),
        'description': 'memory_search/memory_read 可读取的精确正文引用；按字符范围冻结为事实。文件需 file_ 快照，用户原文需 event_ 和 view=content'},
    sources={'type': 'array', 'items': {'type': 'string'},
             'description': '不重复的可验证来源；内联 body 必须提供。body_ref 自动成为来源'},
    ref={'type': 'string', 'minLength': 1},
    expected_revision={'type': 'integer', 'minimum': 1},
    status={'type': 'string', 'enum': ['active', 'unverified', 'stale', 'superseded']},
    shared={'type': 'boolean'},
)

SPEC = ToolSpec(
    routes=('memory',),
    parameters=object_schema(['entries'], entries={'type': 'array', 'minItems': 1, 'items': ENTRY,
        'description': '原子保存一个或多个独立事实；可按同一文档引用的不同字符范围拆分，不重复发送大正文'}),
    permission='core', side_effect=True, max_output_chars=24000,
)
