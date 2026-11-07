import json
import math
from pathlib import Path

from genesisai.core.tools.base import HostBoundTool, ToolDescriptor, ToolSpec, object_schema
from .service import LanguageService


class LanguageQuery(HostBoundTool):
    def prepare(self, context, args):
        self.preview = self.host_service.prepare(context.access, args)
        return self.preview

    def execute(self, context, args):
        return self.host_service.execute(context.access, args, self.preview)


class LanguageCancel(HostBoundTool):
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        return self.host_service.cancel(context.access, args['query_id'])


def register_lsp(project_dir, registry, loader, *, state_root):
    path = Path(project_dir) / 'lsp.json'
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or set(data) - {'servers', 'max_concurrent'} or not isinstance(data.get('servers'), dict):
            raise ValueError('配置需要 servers 对象，只接受 max_concurrent 作为额外设置')
        concurrent = data.get('max_concurrent', 4)
        if type(concurrent) is not int or concurrent < 1:
            raise ValueError('max_concurrent 必须为正整数')
        servers = data['servers']
        for suffix, server in servers.items():
            if (not isinstance(server, dict) or set(server) - {'command', 'language', 'workspace_roots',
                    'startup_timeout_seconds', 'query_timeout_seconds'}
                    or not suffix.startswith('.') or suffix != suffix.lower()
                    or not isinstance(server.get('command'), list) or not server['command']
                    or any(not isinstance(part, str) or not part for part in server['command'])
                    or not isinstance(server.get('language'), str) or not server['language']):
                raise ValueError('每个后缀须配置 language 和非空 command 数组；不接受未知字段')
            if 'workspace_roots' in server and (not isinstance(server['workspace_roots'], list) or not server['workspace_roots']
                    or any(not isinstance(root, str) or not root for root in server['workspace_roots'])):
                raise ValueError('workspace_roots 必须是非空目录数组')
            for key in ('startup_timeout_seconds', 'query_timeout_seconds'):
                value = server.setdefault(key, 120)
                if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
                    raise ValueError(key + ' 必须是正数')
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ValueError(f'{path} 配置无效：{exc}') from exc
    if not servers:
        return None
    service = LanguageService(servers, project_dir, state_root=state_root, max_concurrent=concurrent)
    spec = ToolSpec(permission='read', launches_service=True, path_scope='authorized', routes=('coding', 'debugging'),
        timeout_seconds=30,
        max_output_chars=1500000,
        parameters=object_schema(['operation', 'path'], operation={'type': 'string', 'enum': ['definition', 'references', 'diagnostics', 'capabilities']},
            path={'type': 'string', 'minLength': 1, 'maxLength': 2000},
            line={'type': 'integer', 'minimum': 1}, column={'type': 'integer', 'minimum': 1}))
    registry.register(ToolDescriptor('lsp_query', True, 'lsp',
        '异步语言查询，立即返回 query_id；用 read_lsp_result 观察结果和分页，cancel_lsp_query取消。定义/引用需1起始UTF-16行列；diagnostics需服务器拉取诊断能力。',
        Path(project_dir), spec, source='external', scope='project', provider='lsp'))
    loader.register('lsp_query', LanguageQuery(service))
    page_spec = ToolSpec(permission='read', path_scope='authorized', history_lookup=True,
        routes=('coding', 'debugging'), max_output_chars=1500000,
        parameters=object_schema(['query_id'], query_id={'type': 'string', 'minLength': 1, 'maxLength': 100},
            offset={'type': 'integer', 'minimum': 0}, limit={'type': 'integer', 'minimum': 1, 'maximum': 1000}))
    registry.register(ToolDescriptor('read_lsp_result', True, 'lsp',
        '读取语言查询状态/结果，按 next_offset 分页；不会启动外部服务，源文件/目标变化或授权撤销时拒绝旧结果。',
        Path(project_dir), page_spec, source='external', scope='project', provider='lsp'))
    loader.register('read_lsp_result', LanguageQuery(service))
    cancel_spec = ToolSpec(permission='core', routes=('coding', 'debugging'),
        parameters=object_schema(['query_id'], query_id={'type': 'string', 'minLength': 1, 'maxLength': 100}))
    registry.register(ToolDescriptor('cancel_lsp_query', True, 'lsp', '取消当前工作区语言查询；不重发查询。',
        Path(project_dir), cancel_spec, source='external', scope='project', provider='lsp'))
    loader.register('cancel_lsp_query', LanguageCancel(service))
    return service
