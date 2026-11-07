"""The formal staged Skill dependency contract, shared by creation and loading."""

import re


STAGE_NAME = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
STAGE_FIELDS = frozenset({'depends_on', 'required_tools', 'optional_tools'})
STAGES_SCHEMA = {
    'type': 'object', 'minProperties': 1,
    'propertyNames': {'pattern': STAGE_NAME.pattern},
    'additionalProperties': {
        'type': 'object', 'required': sorted(STAGE_FIELDS), 'additionalProperties': False,
        'properties': {name: {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1}}
                       for name in STAGE_FIELDS},
    },
}


def stage_order(stages, selected):
    """Return dependency-before-dependent order without recursion depth limits."""
    if not isinstance(selected, str) or selected not in stages:
        raise ValueError('未知 Skill 阶段：' + str(selected))
    visiting, visited, ordered = set(), set(), []
    pending = [(selected, False)]
    while pending:
        name, complete = pending.pop()
        if complete:
            visiting.remove(name)
            visited.add(name)
            ordered.append(name)
            continue
        if name in visited:
            continue
        if name in visiting:
            raise ValueError('Skill 阶段依赖存在循环：' + name)
        if name not in stages:
            raise ValueError('Skill 引用了未定义阶段：' + name)
        visiting.add(name)
        pending.append((name, True))
        pending.extend((dependency, False) for dependency in reversed(stages[name]['depends_on']))
    return ordered


def validate_stages(stages):
    if not isinstance(stages, dict) or not stages:
        raise ValueError('Skill stages 必须为非空阶段映射')
    for name, stage in stages.items():
        if not isinstance(name, str) or not STAGE_NAME.fullmatch(name):
            raise ValueError('Skill 阶段名称无效')
        if not isinstance(stage, dict) or set(stage) != STAGE_FIELDS:
            raise ValueError('每个 Skill 阶段必须且只能包含 depends_on/required_tools/optional_tools')
        for field, values in stage.items():
            if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values) or len(values) != len(set(values)):
                raise ValueError('Skill 阶段依赖必须为唯一非空字符串列表：' + field)
        if set(stage['required_tools']) & set(stage['optional_tools']):
            raise ValueError('同一阶段不能把工具同时声明为必需和可选')
    for name in stages:
        stage_order(stages, name)
    return stages


def all_tools(stages):
    return {tool for stage in stages.values() for field in ('required_tools', 'optional_tools') for tool in stage[field]}


def resolve_stage(stages, selected, available):
    ordered = stage_order(stages, selected)
    required = {tool for name in ordered for tool in stages[name]['required_tools']}
    optional = {tool for name in ordered for tool in stages[name]['optional_tools']} - required
    availability = {tool: bool(available(tool)) for tool in required | optional}
    missing = sorted(tool for tool in required if not availability[tool])
    if missing:
        raise ValueError('Skill 阶段必需工具不可用：' + ', '.join(missing))
    return {'stage': selected, 'stages': ordered,
            'tools': sorted(required | {tool for tool in optional if availability[tool]}),
            'missing_optional_tools': sorted(tool for tool in optional if not availability[tool])}
