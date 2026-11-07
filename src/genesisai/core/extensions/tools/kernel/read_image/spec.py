from genesisai.core.tools.base import ToolSpec, object_schema


SPEC = ToolSpec(
    routes=('image',),
    parameters=object_schema([], path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
                             image_ref={'type': 'string', 'minLength': 1, 'maxLength': 100},
                             crop=object_schema(['x', 'y', 'width', 'height'],
                                 x={'type': 'integer', 'minimum': 0}, y={'type': 'integer', 'minimum': 0},
                                 width={'type': 'integer', 'minimum': 1}, height={'type': 'integer', 'minimum': 1}),
                             max_side={'type': 'integer', 'minimum': 1, 'description': '视图长边像素，默认用模型配置；不放大原图，仍受字节与解码预算约束'}),
    permission='read', path_scope='authorized', history_lookup=True,
)
