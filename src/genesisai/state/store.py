"""本地会话持久化；追踪记录只包含白名单中的元数据。"""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path


CURRENT_SESSION_SCHEMA_VERSION = 3


def uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def workspace_state_root(workspace):
    """将所有项目运行状态保存在用户选择的工作区内。"""
    return Path(workspace).resolve() / '.genesis'


def _new_session(session_id, workspace):
    from genesisai.agent.research import ResearchController
    return dict(
        schema_version=CURRENT_SESSION_SCHEMA_VERSION,
        id=session_id,
        workspace=str(Path(workspace).resolve()),
        status='idle',
        messages=[],
        calls={},
        sources={},
        artifacts={},
        pending=[],
        grants=[],
        output='',
        remote_allowed=False,
        run_id=None,
        rounds=0,
        tool_count=0,
        failures=0,
        deadline=0,
        usage={},
        history=[],
        changes=[],
        permission_modes={'network': 'ask', 'writes': 'ask', 'shell': 'ask'},
        tool_runtime=dict(active_tools=[], active_skills=[]),
        run_runtime=ResearchController.empty_state(),
    )


def _migrate_session_v0_to_v1(data):
    migrated = dict(data)
    migrated['schema_version'] = 1
    return migrated


def _migrate_session_v1_to_v2(data):
    migrated = dict(data)
    migrated['tool_runtime'] = {'active_tools': [], 'active_skills': []}
    migrated['schema_version'] = 2
    return migrated


def _migrate_session_v2_to_v3(data):
    migrated = dict(data)
    migrated.setdefault('workspace', '')
    migrated.setdefault('changes', [])
    migrated.setdefault('permission_modes', {'network': 'ask', 'writes': 'ask', 'shell': 'ask'})
    migrated['schema_version'] = 3
    return migrated


SESSION_MIGRATIONS = {
    0: _migrate_session_v0_to_v1,
    1: _migrate_session_v1_to_v2,
    2: _migrate_session_v2_to_v3,
}


def migrate_session(data, workspace=None):
    """将旧会话逐版本迁移到当前 Schema，不修改输入对象。"""
    if not isinstance(data, dict):
        raise ValueError('会话 JSON 根节点必须是对象')
    version = data.get('schema_version', 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError('会话 schema_version 必须是非负整数')
    if version > CURRENT_SESSION_SCHEMA_VERSION:
        raise ValueError(
            f'会话版本 {version} 高于当前支持版本 {CURRENT_SESSION_SCHEMA_VERSION}，请升级 GenesisAI'
        )

    migrated = dict(data)
    while version < CURRENT_SESSION_SCHEMA_VERSION:
        migration = SESSION_MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f'缺少会话版本 {version} 的迁移函数')
        migrated = migration(migrated)
        next_version = migrated.get('schema_version')
        if next_version != version + 1:
            raise ValueError(f'会话版本 {version} 迁移结果无效')
        version = next_version
    if version == 3 and not migrated.get('workspace') and workspace is not None:
        migrated['workspace'] = str(Path(workspace).resolve())
    if version == 3:
        migrated.setdefault('changes', [])
        migrated.setdefault('permission_modes', {'network': 'ask', 'writes': 'ask', 'shell': 'ask'})
        if isinstance(migrated.get('tool_runtime'), dict):
            migrated['tool_runtime'].setdefault('active_skills', [])
    if version == 3 and 'run_runtime' not in migrated:
        from genesisai.agent.research import ResearchController
        phase = 'done' if migrated.get('status') == 'completed' else 'explore'
        migrated['run_runtime'] = ResearchController.empty_state(phase=phase)
    elif version == 3 and isinstance(migrated.get('run_runtime'), dict):
        from genesisai.agent.research import ResearchController
        defaults = ResearchController.empty_state(
            migrated['run_runtime'].get('profile', 'direct_answer'),
            phase=migrated['run_runtime'].get('phase', 'explore'),
        )
        for key, value in defaults.items():
            migrated['run_runtime'].setdefault(key, value)
    return migrated


def validate_session(data, expected_id=None):
    """验证会话持久化数据的最小稳定结构。"""
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
        'remote_allowed': bool,
        'usage': dict,
        'history': list,
        'tool_runtime': dict,
        'run_runtime': dict,
        'changes': list,
        'permission_modes': dict,
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
    modes = data['permission_modes']
    if set(modes) != {'network', 'writes', 'shell'} or any(value not in {'ask', 'allow'} for value in modes.values()):
        raise ValueError('会话字段 permission_modes 无效')
    if data['run_id'] is not None and not isinstance(data['run_id'], str):
        raise ValueError('会话字段 run_id 类型无效')
    for name in ('rounds', 'tool_count', 'failures'):
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f'会话字段 {name} 类型无效')
    deadline = data['deadline']
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline < 0:
        raise ValueError('会话字段 deadline 类型无效')
    if data.get('schema_version') != CURRENT_SESSION_SCHEMA_VERSION:
        raise ValueError('会话 Schema 未迁移到当前版本')
    tool_runtime = data['tool_runtime']
    if set(tool_runtime) != {'active_tools', 'active_skills'} or not isinstance(tool_runtime['active_tools'], list) or not isinstance(tool_runtime['active_skills'], list):
        raise ValueError('会话字段 tool_runtime 无效')
    active_tools = tool_runtime['active_tools']
    if len(active_tools) > 8 or any(not isinstance(name, str) or not name for name in active_tools):
        raise ValueError('会话字段 active_tools 无效')
    if len(active_tools) != len(set(active_tools)):
        raise ValueError('会话字段 active_tools 包含重复工具')
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
    from genesisai.agent.state_machine import RUN_STATES
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
            self.data = migrate_session(raw, self.workspace)
            validate_session(self.data, self.id)
            if self.data['status'] == 'running':
                self.data['status'] = 'interrupted'
            for call in self.data['calls'].values():
                if call['state'] == 'started':
                    call['state'] = 'unknown'
        else:
            self.data = _new_session(self.id, self.workspace)
        self.save()

    def save(self):
        validate_session(self.data, self.id)
        atomic_json(self.path, self.data)

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

    def source(self, **data):
        identity = json.dumps(data, sort_keys=True, ensure_ascii=False)
        key = 'src_' + hashlib.sha256(identity.encode()).hexdigest()[:16]
        self.data['sources'][key] = data
        self.save()
        return key

    def artifact(self, path):
        path = Path(path).resolve()
        digest = sha(path)
        key = 'artifact_' + hashlib.sha256((str(path) + digest).encode()).hexdigest()[:16]
        self.data['artifacts'][key] = dict(path=str(path), sha256=digest, run_id=self.data['run_id'])
        self.save()
        return key

    def verify_artifacts(self):
        for data in self.data['artifacts'].values():
            path = Path(data['path']).resolve()
            if not path.is_relative_to(Path(self.data['output']).resolve()) or not path.is_file() or sha(path) != data['sha256']:
                raise ValueError('产物缺失、越界或已变更：' + path.name)

    def verify_sources(self):
        for data in self.data['sources'].values():
            if data['kind'] == 'file':
                path = Path(data['path']).resolve()
                if not any(path == Path(g) or path.is_relative_to(Path(g)) for g in self.data['grants']):
                    raise ValueError('历史资料已不在授权范围')
                if not path.is_file() or sha(path) != data['sha256']:
                    raise ValueError('历史资料已变更，请新建会话重新读取')
            elif data['kind'] == 'web':
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

