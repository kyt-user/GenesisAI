"""本地会话持久化；追踪记录只包含白名单中的元数据。"""
import hashlib
import json
import os
import time
from pathlib import Path

from genesisai.shared.budgets import initial_run_runtime
from genesisai.shared.ids import sha, uid


CURRENT_SESSION_SCHEMA_VERSION = 3


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def default_workspace():
    """与启动目录、应用安装位置无关的默认工作区。"""
    return (Path.home() / 'GenesisAI' / 'workspace').resolve()


def application_state_root():
    """所有工作区的会话和运行历史统一保存在默认工作区。"""
    return default_workspace() / '.genesis'


def _new_session(session_id, workspace):
    return dict(
        schema_version=CURRENT_SESSION_SCHEMA_VERSION,
        id=session_id,
        workspace=str(Path(workspace).resolve()),
        status='idle',
        messages=[],
        context_checkpoint=None,
        context_estimate_factor=1.0,
        calls={},
        sources={},
        artifacts={},
        pending=[],
        grants=[],
        output='',
        run_id=None,
        rounds=0,
        tool_count=0,
        failures=0,
        deadline=0,
        usage={},
        history=[],
        changes=[],
        permission_mode='user',
        tool_runtime=dict(active_skills=[]),
        run_runtime=initial_run_runtime(),
    )


def validate_session(data, expected_id=None):
    """验证会话持久化数据的最小稳定结构。"""
    if not isinstance(data, dict):
        raise ValueError('会话 JSON 根节点必须是对象')
    version = data.get('schema_version')
    if type(version) is not int or version != CURRENT_SESSION_SCHEMA_VERSION:
        raise ValueError(f'会话 schema_version 必须为 {CURRENT_SESSION_SCHEMA_VERSION}；不支持历史格式，请新建会话')
    field_types = {
        'id': str,
        'workspace': str,
        'status': str,
        'messages': list,
        'calls': dict,
        'sources': dict,
        'artifacts': dict,
        'pending': list,
        'grants': list,
        'output': str,
        'usage': dict,
        'history': list,
        'tool_runtime': dict,
        'run_runtime': dict,
        'changes': list,
    }
    missing = [name for name in field_types if name not in data]
    missing.extend(
        name for name in ('run_id', 'rounds', 'tool_count', 'failures', 'deadline')
        if name not in data
    )
    if missing:
        raise ValueError('会话缺少字段：' + ', '.join(sorted(set(missing))))
    for name, expected in field_types.items():
        if not isinstance(data[name], expected):
            raise ValueError(f'会话字段 {name} 类型无效')
    if expected_id is not None and data['id'] != expected_id:
        raise ValueError('会话 ID 与文件名不一致')
    if not data['workspace']:
        raise ValueError('会话字段 workspace 无效')
    checkpoint = data.get('context_checkpoint')
    if checkpoint is not None:
        if (not isinstance(checkpoint, dict)
                or type(checkpoint.get('source_count')) is not int
                or checkpoint['source_count'] < 0
                or not isinstance(checkpoint.get('source_hash'), str)
                or not isinstance(checkpoint.get('messages'), list)):
            raise ValueError('会话 context_checkpoint 无效')
    factor = data.get('context_estimate_factor', 1.0)
    if type(factor) not in (int, float) or not 1 <= factor <= 4:
        raise ValueError('会话 context_estimate_factor 无效')
    if data.get('permission_mode', 'user') not in ('user', 'auto'):
        raise ValueError('会话字段 permission_mode 无效')
    if data['run_id'] is not None and not isinstance(data['run_id'], str):
        raise ValueError('会话字段 run_id 类型无效')
    for name in ('rounds', 'tool_count', 'failures'):
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'会话字段 {name} 类型无效')
    deadline = data['deadline']
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline < 0:
        raise ValueError('会话字段 deadline 类型无效')
    tool_runtime = data['tool_runtime']
    if set(tool_runtime) != {'active_skills'} or not isinstance(tool_runtime['active_skills'], list):
        raise ValueError('会话字段 tool_runtime 无效')
    if len(tool_runtime['active_skills']) > 2 or any(not isinstance(name, str) for name in tool_runtime['active_skills']):
        raise ValueError('会话字段 active_skills 无效')
    run_runtime = data['run_runtime']
    required_runtime = {
        'profile', 'phase', 'budget', 'usage', 'candidates', 'candidate_count',
        'fetched_count', 'content_hashes', 'evidence', 'evidence_refs',
        'failed_resources', 'recovery_counts', 'counted_calls',
        'completed_queries', 'progress_mark', 'stalled_rounds', 'stop_reason',
        'partial_response', 'last_tool_name', 'last_query_relevant', 'reuse_evidence', 'allow_new_network', 'prompt_names', 'prompt_hashes', 'started_at',
        'lifecycle', 'state_history', 'protocols', 'context_summary', 'context_report',
    }
    missing_runtime = required_runtime - set(run_runtime)
    if missing_runtime:
        raise ValueError('会话字段 run_runtime 缺少：' + ', '.join(sorted(missing_runtime)))
    if run_runtime['profile'] not in {'direct_answer', 'web_quick', 'web_normal', 'web_deep', 'local_files', 'creative'}:
        raise ValueError('会话字段 run_runtime.profile 无效')
    if run_runtime['phase'] not in {'explore', 'answer', 'recover', 'done'}:
        raise ValueError('会话字段 run_runtime.phase 无效')
    from genesisai.shared.run_states import RUN_STATES
    if run_runtime['lifecycle'] not in RUN_STATES:
        raise ValueError('会话字段 run_runtime.lifecycle 无效')
    for name in ('budget', 'usage', 'candidates', 'evidence', 'failed_resources', 'recovery_counts', 'prompt_hashes', 'context_report'):
        if not isinstance(run_runtime[name], dict):
            raise ValueError(f'会话字段 run_runtime.{name} 类型无效')
    for name in ('content_hashes', 'evidence_refs', 'counted_calls', 'completed_queries', 'progress_mark', 'prompt_names', 'state_history', 'protocols'):
        if not isinstance(run_runtime[name], list):
            raise ValueError(f'会话字段 run_runtime.{name} 类型无效')
    for name in ('candidate_count', 'fetched_count', 'stalled_rounds'):
        value = run_runtime[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'会话字段 run_runtime.{name} 类型无效')
    if run_runtime['stop_reason'] is not None and not isinstance(run_runtime['stop_reason'], str):
        raise ValueError('会话字段 run_runtime.stop_reason 类型无效')
    if not isinstance(run_runtime['partial_response'], str):
        raise ValueError('会话字段 run_runtime.partial_response 类型无效')
    if run_runtime['last_tool_name'] is not None and not isinstance(run_runtime['last_tool_name'], str):
        raise ValueError('会话字段 run_runtime.last_tool_name 类型无效')
    if not isinstance(run_runtime['last_query_relevant'], bool):
        raise ValueError('会话字段 run_runtime.last_query_relevant 类型无效')
    for name in ('reuse_evidence', 'allow_new_network'):
        if not isinstance(run_runtime[name], bool):
            raise ValueError(f'会话字段 run_runtime.{name} 类型无效')
    if run_runtime['context_summary'] is not None and not isinstance(run_runtime['context_summary'], dict):
        raise ValueError('会话字段 run_runtime.context_summary 无效')


class HostLock:
    def __init__(self, workspace):
        self.path = Path(workspace) / '.host.lock'

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError('同一 workspace 已有活动宿主') from exc
        return self

    def __exit__(self, *args):
        self.stream.close()


class Store:
    def __init__(self, state_root, session_id=None, *, workspace=None):
        self.root = Path(state_root).resolve()
        self.workspace = Path(workspace or state_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if session_id and (not session_id.startswith('session_') or not session_id.replace('_', '').isalnum()):
            raise ValueError('无效会话 ID')
        self.id = session_id or uid('session')
        self.path = self.root / 'sessions' / f'{self.id}.json'
        if session_id:
            try:
                raw = json.loads(self.path.read_text(encoding='utf-8'))
            except json.JSONDecodeError as exc:
                raise ValueError('会话 JSON 损坏，原文件未修改') from exc
            self.data = raw
            validate_session(self.data, self.id)
            saved_workspace = Path(self.data['workspace']).resolve()
            if workspace is not None and self.workspace != saved_workspace:
                raise ValueError('恢复会话的工作区与指定目录不一致，请使用原工作区或新建会话')
            self.workspace = saved_workspace
            interrupted = self.data['status'] == 'running'
            if interrupted:
                self.data['status'] = 'interrupted'
            for call in self.data['calls'].values():
                if call['state'] == 'started':
                    call['state'] = 'unknown'
                    interrupted = True
            if interrupted:
                self.save()
        else:
            self.data = _new_session(self.id, self.workspace)
            self.save()

    def save(self):
        validate_session(self.data, self.id)
        atomic_json(self.path, self.data)

    def get_logger(self):
        """会话级日志端口，也供不组装完整 ToolRuntime 的恢复入口使用。"""
        if not hasattr(self, '_io_logger'):
            from genesisai.core.state.logger import RunLogger
            self._io_logger = RunLogger(self)
        return self._io_logger

    def event(self, kind, **metadata):
        allowed = {
            'call_id', 'tool_call_id', 'model_call_id', 'tool', 'ok', 'code',
            'status', 'elapsed_ms', 'rounds', 'tool_count', 'usage', 'profile',
            'phase', 'finish_reason', 'stop_reason', 'evidence_count',
            'candidate_count', 'retry_count', 'prompt_names', 'prompt_hashes',
            'diagnostic',
            'provider', 'new_candidates', 'repeated_candidates',
            'provider_errors', 'context_report',
        }
        payload = dict(time=time.time(), session_id=self.id, run_id=self.data['run_id'], event=kind)
        payload.update({k: v for k, v in metadata.items() if k in allowed})
        path = self.root / 'traces' / f'{self.id}.jsonl'
        path.parent.mkdir(exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + '\n')

    def _file_roots(self):
        return [Path(p).resolve() for p in [*self.data['grants'], self.data['output']] if p]

    def source(self, **data):
        identity = json.dumps(data, sort_keys=True, ensure_ascii=False)
        key = 'src_' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        if data.get('kind') == 'file' and self.data.get('permission_mode') == 'auto':
            path = Path(data['path']).resolve()
            if not any(path.is_relative_to(root) for root in self._file_roots()):
                data['host_file'] = True
        self.data['sources'][key] = data
        self.save()
        return key

    def artifact(self, path):
        path = Path(path).resolve()
        digest = sha(path)
        key = 'artifact_' + hashlib.sha256((str(path) + digest).encode()).hexdigest()[:16]
        item = dict(path=str(path), sha256=digest, run_id=self.data['run_id'])
        if self.data.get('permission_mode') == 'auto' and not path.is_relative_to(Path(self.data['output']).resolve()):
            item['host_file'] = True
        self.data['artifacts'][key] = item
        self.save()
        return key

    def verify_artifacts(self, *, current_run_only=False):
        latest = {data['path']: data for data in self.data['artifacts'].values()}
        changes = {item['path']: item for item in self.data['changes']}
        for data in latest.values():
            change = changes.get(data['path'])
            last_run = change.get('run_id') if change else data.get('run_id')
            if current_run_only and last_run != self.data['run_id']:
                continue
            if data.get('host_file') and self.data.get('permission_mode') != 'auto':
                continue
            path = Path(data['path']).resolve()
            if not data.get('host_file') and not path.is_relative_to(Path(self.data['output']).resolve()):
                raise ValueError('产物越界：' + path.name)
            if change and change['operation'] in {'delete', 'move'}:
                continue  # 已记录的合法删除/移动使原路径退出当前产物集合，历史仍保留。
            expected = (change or {}).get('after_sha256') or data['sha256']
            if not path.is_file() or sha(path) != expected:
                raise ValueError('产物缺失、越界或已变更：' + path.name)

    def verify_sources(self):
        self.refresh_local_files()
        for data in self.data['sources'].values():
            if data['kind'] == 'web':
                ref = next((key for key, value in self.data['sources'].items() if value is data), None)
                path = self.root / 'sources' / f'{ref}.json'
                if not path.is_file():
                    raise ValueError('历史网络资料缺失，请重新执行检索')
                try:
                    page = json.loads(path.read_text(encoding='utf-8'))
                    from genesisai.shared.security import digest
                    text_digest = digest(page['text'])
                except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise ValueError('历史网络资料损坏，请重新执行检索') from exc
                if text_digest != data.get('sha256'):
                    raise ValueError('历史网络资料已变更，请重新执行检索')

    def refresh_local_files(self):
        """标记已变化的本地快照；保留原始哈希和历史，不阻断继续读取。"""
        changed = []
        digests = {}
        roots = self._file_roots()
        for collection in ('sources', 'artifacts'):
            for ref, data in self.data[collection].items():
                if collection == 'sources' and data['kind'] != 'file':
                    continue
                if data.get('host_file') and self.data.get('permission_mode') != 'auto':
                    continue
                path = Path(data['path']).resolve()
                if not data.get('host_file') and not any(path.is_relative_to(root) for root in roots):
                    raise ValueError('历史文件已不在授权范围：' + path.name)
                if path not in digests:
                    digests[path] = sha(path) if path.is_file() else None
                stale = digests[path] != data['sha256']
                if stale != data.get('stale', False):
                    data['stale'] = stale
                    changed.append(dict(ref=ref, path=str(path), stale=stale))
        if changed:
            self.save()
            self.get_logger().record('local_files_changed', files=changed)

