"""Durable file-tool execution with staged, explicitly collected session effects.

Opted-in tools receive file access, an append-only source/artifact/change store,
logging and a frozen page budget. Live host services require their own adapters.
"""
import base64
import json
import inspect
import os
import sys
import sysconfig
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import cloudpickle

from genesisai.core.jobs.operations import portable_adapter
from genesisai.core.jobs.storage import ACTIVE, load, save
from genesisai.core.jobs.submission import foreground_wait
from genesisai.core.persistence.session import Session
from genesisai.core.tools.base import HostBoundTool, PartialToolResult, ToolContext
from genesisai.core.tools.permissions import accepted_tool
from genesisai.shared.security import Access, ToolError, digest
from genesisai.shared.ids import sha
from genesisai.shared.session import SessionRecord
from genesisai.shared.time_limits import tool_deadline


FOREGROUND_SECONDS = 5
TOOL_ENVIRONMENT_KEYS = (
    'GENESISAI_DOCUMENT_MAX_EXPANDED_BYTES', 'GENESISAI_DOCUMENT_MAX_ENTRIES',
    'GENESISAI_DOCUMENT_MAX_COMPRESSION_RATIO', 'GENESISAI_OCR_MAX_PAGE_PIXELS',
    'GENESISAI_WHISPER_MODEL', 'PLAYWRIGHT_BROWSERS_PATH',
)


def snapshot_adapter(adapter):
    # Keep installed libraries importable by name, but freeze host-only callback
    # modules too (e.g. a Network resolver or MockTransport handler). Registering
    # only the adapter's class module misses those nested callbacks.
    libraries = [Path(sysconfig.get_path(key)).resolve() for key in ('stdlib', 'platstdlib', 'purelib', 'platlib')]
    modules, seen, pending = {}, set(), [adapter]
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        names = {getattr(value, '__module__', type(value).__module__)}
        names.update(cls.__module__ for cls in type(value).__mro__ if cls is not object)
        for name in names:
            module = sys.modules.get(name)
            path = getattr(module, '__file__', None)
            if path and not name.startswith('genesisai.') and not any(Path(path).resolve().is_relative_to(root) for root in libraries):
                modules[name] = module
        if inspect.ismodule(value) or isinstance(value, type):
            continue
        if inspect.ismethod(value):
            pending.extend((value.__func__, value.__self__))
        elif inspect.isfunction(value):
            pending.extend(value.__defaults__ or ())
            for cell in value.__closure__ or ():
                try:
                    pending.append(cell.cell_contents)
                except ValueError:
                    pass  # An uninitialized closure cell has no captured state.
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            pending.extend(value)
        elif hasattr(value, '__dict__'):
            pending.extend(vars(value).values())
    return portable_adapter(adapter, modules=list(modules.values()))


def submit_tool(context, descriptor, implementation, args, preview):
    if isinstance(implementation, HostBoundTool):
        raise ToolError('job_isolation', '宿主绑定工具需要独立作业适配器')
    runtime, store = context.runtime, context.store
    record = store.record
    snapshot = {key: getattr(record, key) for key in (
        'id', 'workspace', 'project_id', 'workspace_id', 'grants', 'output', 'run_id', 'permission_mode')}
    snapshot['root'] = str(store.root)
    snapshot['sources'] = deepcopy(record.sources)
    snapshot['artifacts'] = deepcopy(record.artifacts)
    data = {'name': descriptor.name, 'arguments': args, 'preview': preview,
            'authorization': context.authorization,
            'acceptance': accepted_tool.get(),
            'environment': {key: os.environ.get(key) for key in TOOL_ENVIRONMENT_KEYS},
            'origin_call': {'id': context.call_id, 'name': descriptor.name, 'arguments': json.dumps(args, ensure_ascii=False)},
            'redaction_secrets': list(runtime.logger.journal._secrets),
            'implementation': snapshot_adapter(implementation), 'side_effect': descriptor.spec.side_effect,
            'store': snapshot,
            'network': snapshot_adapter(context.network) if descriptor.spec.network else None,
            'providers': [snapshot_adapter(item) for item in context.providers] if descriptor.spec.network else [],
            'access': {'roots': list(map(str, context.access.roots)), 'output': str(context.access.output),
                       'state_root': str(context.access.state_root), 'workspace_root': str(context.access.workspace),
                       'host_access': context.access.host_access},
            'page_tokens': runtime.context_budgeter.read_page_tokens(),
            'observer': portable_adapter(runtime.observations.adapters[descriptor.name])
                        if descriptor.name in runtime.observations.adapters else None}
    info = runtime.jobs.start_operation('builtin_tool', data,
        timeout=descriptor.spec.execution_timeout_seconds,
        task_id=record.run_runtime.get('project_task_id'))
    wait_seconds = foreground_wait.get()
    info = runtime.jobs.wait(info['job_id'], FOREGROUND_SECONDS if wait_seconds is None else wait_seconds, cancel_on_interrupt=True)
    if info['status'] in ACTIVE:
        return {**info, 'tool': descriptor.name, 'complete': False,
                'read_result': {'tool': 'read_job', 'job_id': info['job_id']}}, []
    result = collect_tool(runtime, info['job_id'], context.call_id)
    if error := result.get('operation_error'):
        payload = PartialToolResult(error['code'], error['message'], **result['payload'])
        payload.error = error
        return payload, result['observations']
    return result['payload'], result['observations']


class ToolJobStore:
    """No session overwrite: only this invocation's effects are published."""
    source = Session.source
    artifact = Session.artifact
    _file_roots = Session._file_roots
    verify_sources = Session.verify_sources
    refresh_local_files = Session.refresh_local_files

    def __init__(self, snapshot, job_root, secrets):
        self.root = Path(snapshot['root'])
        self.id = snapshot['id']
        self.workspace = Path(snapshot['workspace'])
        self.record = SessionRecord(**{key: value for key, value in snapshot.items() if key != 'root'})
        self._baseline = deepcopy((self.record.sources, self.record.artifacts))
        self.job_root = job_root
        self._invalidated = set()
        from genesisai.core.state.logger import RunLogger
        self._logger = RunLogger(SimpleNamespace(root=job_root, id=self.id, record=self.record))
        for secret in secrets:
            self._logger.add_secret(secret)

    def get_logger(self):
        return self._logger

    def event(self, kind, **metadata):
        self._logger.record(kind, **metadata)

    def commit(self):
        save(self.job_root / 'effects.json', {
            'session_id': self.id,
            'sources': {key: value for key, value in self.record.sources.items() if self._baseline[0].get(key) != value},
            'artifacts': {key: value for key, value in self.record.artifacts.items() if self._baseline[1].get(key) != value},
            'changes': self.record.changes})
        changed = [item for item in self.record.changes if item['id'] not in self._invalidated]
        if changed:
            # A completed write must invalidate old evidence even while its
            # original CLI is detached and has not yet collected the result.
            from genesisai.core.persistence.database import Database
            from genesisai.core.memory.project_index import ProjectIndex
            from genesisai.core.memory.scope import CodeScope
            view = SimpleNamespace(root=self.root, record=self.record, workspace=self.workspace,
                                   database=Database(self.root))
            index = ProjectIndex(view, CodeScope(self.workspace, [*self.record.grants, self.record.output]))
            index.mark_changed([item['path'] for item in changed])
            self._invalidated.update(item['id'] for item in changed)


def execute_tool(data, job_root):
    from genesisai.core.tools.worker import tool_worker
    for key, value in data['environment'].items():
        if key not in TOOL_ENVIRONMENT_KEYS:
            raise ToolError('job_config', '内置工具包含未声明的环境设置')
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    store = ToolJobStore(data['store'], job_root, data['redaction_secrets'])
    access_data = dict(data['access'])
    host_access = access_data.pop('host_access')
    access = Access(**access_data)
    access.host_access = host_access
    runtime = SimpleNamespace(logger=store.get_logger(), context_budgeter=SimpleNamespace(
        read_page_tokens=lambda: data['page_tokens']))
    network = cloudpickle.loads(base64.b64decode(data['network'])) if data['network'] else None
    providers = [cloudpickle.loads(base64.b64decode(value)) for value in data['providers']]
    context = ToolContext(store, access, network, providers, runtime, hosted_network=False, hosted_providers=False,
                          authorization=data['authorization'], call_id=data['origin_call']['id'])
    implementation = cloudpickle.loads(base64.b64decode(data['implementation']))
    observer = cloudpickle.loads(base64.b64decode(data['observer'])) if data['observer'] else None
    started = False
    acceptance_token = accepted_tool.set(data['acceptance'])
    try:
        # Re-check the exact resources after queueing. Confirmation never grants
        # permission to overwrite a newer target or read a substituted source.
        state, _ = Session.inspect(store.root, store.id)
        if any(state[key] != data['store'][key] for key in ('grants', 'output', 'permission_mode')):
            raise ToolError('confirmation_invalid', '作业排队期间授权范围已变化')
        with tool_worker(context, implementation, observer, tool_deadline.get()) as worker:
            preview = worker.request('prepare', data['arguments'])
            expected = data['preview']
            if isinstance(expected, dict) and 'args' in expected:
                preview = {**preview, 'args': data['arguments']} if isinstance(preview, dict) else preview
            if digest(preview) != digest(expected):
                raise ToolError('confirmation_invalid', '作业排队期间参数或资源发生变化，请重新确认')
            if data['acceptance'] is not None and data['acceptance'] != {'name': data['name'], 'digest': digest(expected)}:
                raise ToolError('confirmation_invalid', '作业的具体确认与冻结资源不匹配')
            started = True
            payload, observations = worker.request('execute', data['arguments'])
        if not isinstance(payload, dict):
            raise ToolError('invalid_result', '工具结果必须是对象')
        store.commit()
        return {'tool': data['name'], 'payload': dict(payload), 'observations': observations,
                'access': data['access'],
                'partial_error': payload.error if isinstance(payload, PartialToolResult) else None}
    except Exception as exc:
        store.get_logger().error('tool_error', exc, call=data['origin_call'])
        if started and data['side_effect'] and getattr(exc, 'error_type', None) not in {
                'patch_conflict', 'confirmation_invalid', 'validation_error', 'target_exists', 'format_mismatch'}:
            raise ToolError('unknown_execution', f'文件作业可能已产生修改，请核对产物与 effects.json：{exc}') from exc
        if not isinstance(exc, ToolError):
            raise ToolError('execution_error', '工具执行失败：' + type(exc).__name__) from exc
        raise
    finally:
        accepted_tool.reset(acceptance_token)


def collect_tool(runtime, job_id, call_id):
    jobs, store = runtime.jobs, runtime.store
    info = jobs.status(job_id)
    if info['kind'] != 'builtin_tool':
        raise ToolError('job_operation', '此作业不是文件工具操作')
    try:
        result = jobs.operation_result(job_id, acknowledge=False)
    except ToolError as exc:
        if info['status'] == 'unknown' or info['status'] in ACTIVE or info['status'] == 'succeeded':
            raise
        return {'payload': {}, 'observations': [], 'operation_error': {
            'code': exc.error_type, 'message': str(exc), 'retryable': bool(exc.retryable)}}
    if result is None:
        return None
    current_access = {'roots': list(map(str, runtime.access.roots)), 'output': str(runtime.access.output),
                      'state_root': str(runtime.access.state_root), 'workspace_root': str(runtime.access.workspace),
                      'host_access': runtime.access.host_access}
    if result['access'] != current_access:
        raise ToolError('permission_denied', '作业执行时的授权范围与当前范围不同，结果暂不可收取；恢复原授权范围后重试 read_job')
    owner = store.record.job_receipts.get(job_id)
    delivered = bool(owner and store.record.calls.get(owner, {}).get('state') in {'succeeded', 'failed'})
    if delivered:
        # Reading an already delivered result never reapplies its staged effects.
        payload = result['payload']
        if error := result.get('partial_error'):
            payload = PartialToolResult(error['code'], error['message'], **payload)
            payload.error = error
        return {'payload': payload, 'observations': result['observations'] if owner == call_id else []}
    from genesisai.core.tools.authorization import verify_job_authorization
    verify_job_authorization(info)
    effects = load(jobs._path(job_id) / 'effects.json')
    if effects['session_id'] != store.id:
        raise ToolError('permission_denied', '作业结果不属于当前会话')
    for item in [*effects['sources'].values(), *effects['artifacts'].values()]:
        if item.get('path'):
            path = runtime.access.read_reference(item['path'])
            item['stale'] = not path.is_file() or sha(path) != item.get('sha256')
    for item in effects['changes']:
        runtime.access.read_reference(item['path'])
    # One host commit imports effects and acknowledges the result. Original
    # source/artifact identifiers and change IDs make repeated collection safe.
    original = deepcopy((store.record.sources, store.record.artifacts, store.record.changes,
                         store.record.run_runtime, store.record.job_receipts))
    owner = store.record.job_receipts.get(job_id)
    if owner and store.record.calls.get(owner, {}).get('state') not in {'succeeded', 'failed'}:
        owner = None  # An interrupted collector never delivered its tool result.
    observe = owner in (None, call_id)
    try:
        for key in ('sources', 'artifacts'):
            target = getattr(store.record, key)
            for identity, item in effects[key].items():
                target.setdefault(identity, item)
                if 'stale' in item:
                    target[identity]['stale'] = item['stale']
        seen = {item['id'] for item in store.record.changes}
        store.record.changes.extend(item for item in effects['changes'] if item['id'] not in seen)
        pending = store.record.run_runtime.get('pending_execution_jobs', [])
        store.record.run_runtime['pending_execution_jobs'] = [item for item in pending if item != job_id]
        if observe:
            store.record.job_receipts[job_id] = call_id
        store.commit()
    except BaseException:
        (store.record.sources, store.record.artifacts, store.record.changes,
         store.record.run_runtime, store.record.job_receipts) = original
        raise
    observations = list(result['observations'])
    observed = {item.get('path') for item in observations if item['kind'] == 'resource_changed'}
    observations.extend({'kind': 'resource_changed', 'path': item['path'], 'sha256': item['after_sha256'],
                         'passed': None, 'detail': job_id}
                        for item in effects['changes'] if item['path'] not in observed)
    payload = result['payload']
    if error := result.get('partial_error'):
        payload = PartialToolResult(error['code'], error['message'], **payload)
        payload.error = error
    return {'payload': payload, 'observations': observations if observe else []}
