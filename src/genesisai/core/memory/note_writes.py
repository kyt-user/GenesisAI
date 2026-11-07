"""Atomic fact entries with frozen, scope-checked document provenance."""
import hashlib
import json
import time

from genesisai.core.persistence.database import Database, encode
from genesisai.core.memory.invalidation import invalidate_sources
from genesisai.shared.ids import uid

KINDS = {'decision', 'constraint', 'experience', 'architecture', 'hypothesis'}
STATES = {'active', 'unverified', 'stale', 'superseded'}


def referenced_body(memory, value):
    if (not isinstance(value, dict) or set(value) - {'ref', 'revision', 'view', 'offset', 'length'}
            or not isinstance(value.get('ref'), str) or not value['ref']):
        raise ValueError('body_ref 需要精确引用，可选 revision/view/offset/length')
    offset, length = value.get('offset', 0), value.get('length')
    if type(offset) is not int or offset < 0 or (length is not None and (type(length) is not int or length < 1)):
        raise ValueError('body_ref 字符范围无效')
    view = value.get('view', 'record')
    first = memory.read(value['ref'], offset=offset, limit=1, revision=value.get('revision'), view=view)
    total = first['total_chars']
    end = total if length is None else offset + length
    if offset >= total or end > total:
        raise ValueError('body_ref 范围超出原文；不截断、不猜测')
    revision = first.get('revision')
    chunks, cursor = [], offset
    while cursor < end:
        page = memory.read(value['ref'], offset=cursor, limit=min(200000, end - cursor), revision=revision, view=view)
        if not page['text']:
            raise ValueError('引用正文未完整读取')
        chunks.append(page['text'])
        cursor += len(page['text'])
    body = ''.join(chunks)
    snapshot = {'ref': value['ref'], 'view': view, 'revision': revision, 'offset': offset,
                'length': len(body), 'sha256': hashlib.sha256(body.encode('utf-8')).hexdigest()}
    return body, snapshot


def prepare(memory, *, title, kind, body=None, body_ref=None, sources=None, ref=None,
            expected_revision=None, status='unverified', shared=False):
    if (not isinstance(title, str) or not title.strip() or len(title) > 200
            or not isinstance(kind, str) or kind not in KINDS or not isinstance(status, str) or status not in STATES
            or type(shared) is not bool):
        raise ValueError('记忆标题、类型、状态或作用域无效（标题最多200字符）')
    if (body is None) == (body_ref is None):
        raise ValueError('每个事实条目必须且只能提供 body 或 body_ref')
    if (ref is not None and (not isinstance(ref, str) or not ref or type(expected_revision) is not int or expected_revision < 1)
            or (ref is None and expected_revision is not None)):
        raise ValueError('修改知识必须提供引用和正整数版本；新建不接受 expected_revision')
    sources = [] if sources is None else sources
    if (not isinstance(sources, list) or any(not isinstance(s, str) or not s for s in sources)
            or len(sources) != len(set(sources))):
        raise ValueError('sources 必须是不重复的精确引用列表')
    sources = list(sources)
    snapshot = None
    if body_ref is not None:
        body, snapshot = referenced_body(memory, body_ref)
        if snapshot['ref'] not in sources:
            sources.append(snapshot['ref'])
    if not isinstance(body, str) or not body.strip() or not sources:
        raise ValueError('记忆需要非空正文和可验证来源')
    if ref in sources:
        raise ValueError('记忆不能引用自身')
    provenance = {source: memory.read(source, limit=1) for source in sources}
    if snapshot and provenance[snapshot['ref']].get('revision') != snapshot['revision']:
        raise ValueError('正文来源版本已变化，请重新读取当前版本')
    if status == 'active' and any(item.get('status') in {'stale', 'superseded'} or item.get('current') is False
                                  for item in provenance.values()):
        raise ValueError('失效来源不能支持 active 知识')
    title, body = memory.session.get_logger().journal.clean([title, body])
    return dict(title=title, body=body, kind=kind, sources=sources, ref=ref, expected_revision=expected_revision,
                status=status, shared=shared, body_reference=snapshot, provenance=provenance)


def persist(memory, db, entry):
    ref, sources, provenance = entry['ref'], entry['sources'], entry['provenance']
    old = db.execute('SELECT * FROM notes WHERE id=? AND project_id=? AND (workspace_id=? OR workspace_id IS NULL)',
                     (ref, memory.project_id, memory.workspace_id)).fetchone() if ref else None
    if ref and (not old or old['revision'] != entry['expected_revision']):
        raise ValueError('记忆版本已变化，请重新读取')
    for source, metadata in provenance.items():
        if metadata.get('revision') is not None:
            current = db.execute('SELECT revision,status FROM notes WHERE id=?', (source,)).fetchone()
            if not current or current['revision'] != metadata['revision']:
                raise ValueError('来源版本已变化，请重新查阅')
        elif metadata.get('kind') == 'code':
            snapshot = db.execute('SELECT 1 FROM file_snapshots WHERE id=? AND project_id=? AND workspace_id=?',
                                  (source, memory.project_id, memory.workspace_id)).fetchone()
            if not snapshot:
                raise ValueError('来源文件快照已不存在')
            if entry['status'] == 'active':
                current = db.execute('SELECT 1 FROM files f WHERE workspace_id=? AND path=? AND digest=? '
                                     'AND NOT EXISTS (SELECT 1 FROM index_dirty d WHERE d.workspace_id=f.workspace_id AND d.path=f.path)',
                                     (memory.workspace_id, metadata['path'], metadata['sha256'])).fetchone()
                if not current:
                    raise ValueError('来源文件已变化，请重新查阅')
        elif not db.execute('SELECT 1 FROM events e JOIN sessions s ON e.session_id=s.id '
                            'WHERE e.id=? AND s.project_id=? AND s.workspace_id=?',
                            (source, memory.project_id, memory.workspace_id)).fetchone():
            raise ValueError('历史来源已不存在或不属于当前工作区')
    queue, visited = list(sources), set()
    while queue:
        source = queue.pop()
        if source == ref:
            raise ValueError('记忆来源存在循环')
        if source in visited:
            continue
        visited.add(source)
        parent = db.execute('SELECT sources FROM notes WHERE id=?', (source,)).fetchone()
        if parent:
            queue.extend(json.loads(parent['sources']))
    identity = ref or uid('note')
    workspace = old['workspace_id'] if old else (None if entry['shared'] else memory.workspace_id)
    record = dict(id=identity, project_id=memory.project_id, workspace_id=workspace,
                  kind=entry['kind'], title=entry['title'], body=entry['body'], status=entry['status'],
                  revision=old['revision'] + 1 if old else 1, sources=sources,
                  source_versions={source: item.get('revision') for source, item in provenance.items()},
                  updated=time.time())
    if entry['body_reference'] is not None:
        record['body_reference'] = entry['body_reference']
    if old:
        invalidate_sources(db, memory.project_id, [identity])
    db.execute('INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
               'kind=excluded.kind,title=excluded.title,body=excluded.body,status=excluded.status,'
               'revision=excluded.revision,sources=excluded.sources,updated=excluded.updated',
               tuple(record[k] for k in ('id', 'project_id', 'workspace_id', 'kind', 'title', 'body', 'status', 'revision'))
               + (encode(sources), record['updated']))
    db.execute('INSERT INTO note_versions VALUES(?,?,?)', (identity, record['revision'], encode(record)))
    if record['status'] in {'active', 'unverified'}:
        Database.index(db, identity, memory.project_id, workspace, 'note', record['title'], record['body'])
    else:
        db.execute('DELETE FROM search_documents WHERE id=?', (identity,))
        db.execute('DELETE FROM search_fts WHERE id=?', (identity,))
    return {key: value for key, value in record.items() if key != 'body'} | {
        'body_chars': len(record['body']), 'read_more': {'ref': identity, 'revision': record['revision']}}


def prepare_batch(memory, entries):
    if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError('entries 必须为非空事实条目数组')
    prepared = [prepare(memory, **entry) for entry in entries]
    updates = [entry['ref'] for entry in prepared if entry['ref']]
    if len(updates) != len(set(updates)):
        raise ValueError('同一批次不能重复修改同一记忆')
    return prepared


def commit(memory, prepared):
    with memory.database.transaction() as db:
        records = [persist(memory, db, entry) for entry in prepared]
    return {'entries': records}


def write(memory, entries):
    return commit(memory, prepare_batch(memory, entries))
