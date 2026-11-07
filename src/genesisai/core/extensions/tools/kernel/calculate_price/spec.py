from genesisai.core.tools.base import ToolSpec, object_schema

OPERAND = object_schema(['type', 'value', 'source_ref', 'quote'],
    type={'type': 'string', 'enum': ['money', 'quantity', 'rate']},
    value={'type': 'string', 'minLength': 1, 'maxLength': 120},
    currency={'type': 'string', 'minLength': 3, 'maxLength': 3},
    from_currency={'type': 'string', 'minLength': 3, 'maxLength': 3},
    to_currency={'type': 'string', 'minLength': 3, 'maxLength': 3},
    source_ref={'type': 'string', 'minLength': 1, 'maxLength': 64, 'description': '当前会话已读src_引用；user表示当前用户原消息'},
    quote={'type': 'string', 'minLength': 1, 'maxLength': 2000, 'description': '逐字原文；数量必须带明确数量/单位，汇率须如1 USD = 7.2 CNY'})
SPEC = ToolSpec(routes=('web', 'price', 'calculate', '价格'), parameters=object_schema(['operation', 'operands'],
    operation={'type': 'string', 'enum': ['sum', 'multiply', 'convert']},
    operands={'type': 'array', 'items': OPERAND, 'minItems': 1, 'maxItems': 100},
    decimal_places={'type': 'integer', 'minimum': 0, 'maximum': 8}),
    permission='read', path_scope='authorized', max_output_chars=300000)
