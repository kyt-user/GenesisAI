from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('coding', 'capability_builder'),
    parameters=object_schema(
        ['operation'],
        operation={'type': 'string', 'enum': ['read', 'update']},
        section={'type': 'string', 'enum': ['plan', 'objective', 'instructions', 'constraints', 'handoff']},
        offset={'type': 'integer', 'minimum': 0},
        limit={'type': 'integer', 'minimum': 1, 'maximum': 100},
        max_chars={'type': 'integer', 'minimum': 1, 'maximum': 200000},
        parent={'type': 'string', 'minLength': 1},
        status={'type': 'string', 'enum': ['pending', 'in_progress', 'completed', 'cancelled']},
        expected_revision={'type': 'integer', 'minimum': 0, 'description': '更新必填：使用当前 plan_revision，冲突后先重读'},
        upsert={'type': 'array', 'items': object_schema(['id'],
            id={'type': 'string', 'minLength': 1},
            kind={'type': 'string', 'enum': ['phase', 'step', 'acceptance']},
            text={'type': 'string', 'minLength': 1},
            parent={'type': 'string', 'description': '父阶段ID；空字符串表示根节点'},
            status={'type': 'string', 'enum': ['pending', 'in_progress', 'completed', 'cancelled']},
            requires_verification={'type': 'boolean'},
            evidence_calls={'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
            reason={'type': 'string', 'minLength': 1},
        )},
        remove={'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
        non_goals={'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
    ),
    permission="core",
    # Task bookkeeping, like loading a Skill, is available during discussion.
    # It cannot modify project files or grant permissions.
    side_effect=False,
    max_output_chars=8000,
)
