from genesisai.core.tools.base import ToolSpec, object_schema

SPEC = ToolSpec(permission='read', path_scope='workspace', execution='job', routes=('coding', 'testing', 'local_files'),
    max_output_chars=200000, parameters=object_schema([], cwd={'type': 'string', 'minLength': 1, 'maxLength': 2000},
        section={'type': 'string', 'enum': ['summary', 'packages', 'manifests', 'test_files', 'dependencies', 'html_files', 'source_roots', 'scan_errors', 'skipped_links']},
        offset={'type': 'integer', 'minimum': 0}, limit={'type': 'integer', 'minimum': 1, 'maximum': 1000}))
