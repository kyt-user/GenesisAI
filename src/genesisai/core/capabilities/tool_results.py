"""Publish exact external results once and read immutable, scoped body pages."""
import hashlib
import json
from pathlib import Path

from genesisai.core.capabilities import artifacts
from genesisai.core.capabilities.assets import file_identity
from genesisai.shared.security import ToolError

INLINE_BYTES = 65536


def _key(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def publish(service, outcome, *, job_id=None, logs_only=False):
    owner = outcome['owner']
    item = service.store.get(owner['id'], owner['revision'])
    if item['digest'] != owner['digest'] or item['package']['kind'] != 'tool':
        raise ToolError('capability_integrity', '结果与冻结能力修订不匹配')
    runtime, session = service.runtime, service.runtime.store
    ownership = None
    if job_id is not None:
        info = runtime.jobs.status(job_id)
        if info['kind'] != 'external_tool' or info['status'] in {'queued', 'running', 'stopping'} or (not logs_only and info['status'] != 'succeeded'):
            raise ToolError('invalid_result', '只能收取本会话成功完成的外挂工具作业')
        runtime.access.read_reference(str(session.workspace))
        previous = [source for source in session.record.sources.values()
                    if source.get('kind') == 'capability_result' and source.get('job_id') == job_id
                    and source.get('owner_session_id') == session.id and source.get('stream') == ('stdout' if logs_only else 'result')]
        if not previous:
            from genesisai.core.tools.authorization import verify_job_authorization
            verify_job_authorization(info)
        ownership = {'owner_session_id': session.id, 'workspace_id': session.record.workspace_id,
                     'workspace': str(session.workspace), 'job_id': job_id}
        allowed_root = runtime.jobs.root / job_id
    else:
        allowed_root = Path(outcome['directory'])
    references, published = {}, {}
    for stream in (('stdout', 'stderr') if logs_only else ('stdout', 'stderr', 'result')):
        entry = outcome['files'][stream]
        log_metadata = {}
        if stream in {'stdout', 'stderr'}:
            if (entry.get('redacted') is not True or type(entry.get('original_bytes')) is not int
                    or entry['original_bytes'] < 0):
                raise ToolError('capability_integrity', '日志缺少执行时脱敏证明，拒绝发布原始日志')
            log_metadata = {key: entry[key] for key in ('redacted', 'original_bytes')}
        expected = {key: entry[key] for key in ('sha256', 'size')}
        identity = 'artifact_' + _key([owner, session.id if job_id else None, job_id or outcome['directory'], stream])[:32]
        try:
            asset = artifacts.read(service.store, {'id': identity}, session=session, access=runtime.access)
        except ToolError as exc:
            if exc.error_type != 'unknown_artifact':
                raise
            path = Path(entry['path'])
            if not path.resolve(strict=True).is_relative_to(Path(allowed_root).resolve(strict=True)):
                raise ToolError('capability_integrity', '结果文件不属于当前作业')
            if file_identity(path) != expected:
                raise ToolError('capability_integrity', '收取前结果或日志已变化')
            asset = artifacts.publish(service.store, item, path, stream + ('.json' if stream == 'result' else '.log'),
                                      identity=identity, ownership={**(ownership or {}), **log_metadata})
        if (any(asset[key] != expected[key] for key in expected)
                or any(asset.get(key) != value for key, value in log_metadata.items())):
            raise ToolError('capability_integrity', '同一作业的不可变结果引用发生冲突')
        published[stream] = asset
        if job_id is None:
            references[stream] = asset['id']
        else:
            reference = session.source(kind='capability_result', artifact_id=asset['id'],
                capability_id=item['id'], revision=item['revision'], digest=item['digest'], stream=stream,
                sha256=asset['sha256'], size=asset['size'], **ownership, **log_metadata)
            references[stream] = reference
            snapshot = service.store.assets.resolve(expected)
            existing_refs = {ref for artifact in session.record.artifacts.values()
                             if artifact.get('path') == str(snapshot.resolve()) and artifact.get('sha256') == expected['sha256']
                             for ref in artifact.get('source_refs', [])}
            if reference not in existing_refs:
                session.artifact(snapshot, source_refs=sorted(existing_refs | {reference}))
    if logs_only:
        return {'logs': references}
    result = published['result']
    included = result['size'] <= INLINE_BYTES
    envelope = {'result_ref': references['result'], 'value_included': included,
                'logs': {name: references[name] for name in ('stdout', 'stderr')},
                'result_bytes': result['size'], 'sha256': result['sha256'], 'media_type': 'application/json'}
    if included:
        path = service.store.assets.resolve({key: result[key] for key in ('sha256', 'size')})
        envelope['value'] = json.loads(path.read_text(encoding='utf-8'))
    if job_id is not None:
        refs = session.record.run_runtime['evidence_refs']
        if references['result'] not in refs:
            refs.append(references['result'])
        session.commit()
    return envelope


def value(service, result):
    if result['value_included']:
        return result['value']
    metadata = artifacts.read(service.store, {'id': result['result_ref']})
    path = service.store.assets.resolve({key: metadata[key] for key in ('sha256', 'size')})
    return json.loads(path.read_text(encoding='utf-8'))


def read(service, ref, *, offset=0, limit=200000, page_tokens=None):
    session, access = service.runtime.store, service.runtime.access
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200000:
        raise ToolError('validation_error', '正文分页参数无效')
    if page_tokens is not None:
        if type(page_tokens) is not int or page_tokens < 512:
            raise ToolError('validation_error', '正文分页预算不足')
        limit = min(limit, max(1, (page_tokens - 512) // 4))
    if ref.startswith('artifact_'):
        artifact_id = ref
    else:
        source = session.record.sources.get(ref)
        if (not isinstance(source, dict) or source.get('kind') != 'capability_result'
                or source.get('owner_session_id') != session.id or source.get('workspace_id') != session.record.workspace_id
                or source.get('stale') or ref != 'src_' + _key(source)[:16]):
            raise ToolError('permission_denied', '结果来源不属于当前会话或工作区')
        if source.get('stream') in {'stdout', 'stderr'} and (source.get('redacted') is not True
                or type(source.get('original_bytes')) is not int or source['original_bytes'] < 0):
            raise ToolError('capability_integrity', '日志来源缺少执行时脱敏证明')
        artifact_id = source['artifact_id']
    result = artifacts.read(service.store, {'id': artifact_id, 'mode': 'text', 'offset': offset, 'limit': limit},
                            session=session, access=access)
    if not ref.startswith('artifact_') and any(result[key] != source[key] for key in ('sha256', 'size', 'owner_session_id', 'workspace_id', 'job_id')):
        raise ToolError('capability_integrity', '结果正文与来源元数据不一致')
    if not ref.startswith('artifact_') and source.get('stream') in {'stdout', 'stderr'}:
        if any(result.get(key) != source[key] for key in ('redacted', 'original_bytes')):
            raise ToolError('capability_integrity', '日志脱敏元数据与来源不一致')
    return {**result, 'ref': ref}


def logs(service, job_id):
    service.runtime.jobs.status(job_id)  # Enforce the current Session's job namespace first.
    directory = service.runtime.jobs.root / job_id / 'logs'
    manifests = list(directory.glob('tool_*/channels.json'))
    if len(manifests) != 1:
        return {'logs': {}, 'logs_available': False}
    outcome = json.loads(manifests[0].read_text(encoding='utf-8'))
    return publish(service, outcome, job_id=job_id, logs_only=True)
