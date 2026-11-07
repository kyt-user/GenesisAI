"""Immutable trial outputs, with scoped references and bounded reads."""

import base64
import json
import mimetypes
from pathlib import Path
import time
from PIL import Image

from genesisai.core.capabilities.files import files_under
from genesisai.core.extensions.tools.filesystem import text_ranges
from genesisai.shared.filetypes import detect_file_type
from genesisai.shared.ids import uid
from genesisai.shared.security import ToolError


def describe(path, name):
    metadata = {'media_type': mimetypes.guess_type(name)[0] or 'application/octet-stream'}
    try:
        metadata['kind'] = detect_file_type(path)
        with path.open('rb') as stream:
            head = stream.read(16)
        if (metadata['media_type'].startswith('image/') or head.startswith((b'\x89PNG', b'\xff\xd8', b'GIF8', b'BM'))
                or (head.startswith(b'RIFF') and head[8:12] == b'WEBP')):
            with Image.open(path) as picture:
                details = {'kind': 'image', 'format': picture.format.lower(),
                           'width': picture.width, 'height': picture.height,
                           'media_type': Image.MIME.get(picture.format, metadata['media_type'])}
                picture.verify()
            metadata.update(details)
        elif metadata['kind'] == 'text':
            metadata.update(text_ranges.metadata(path))
    except (OSError, ValueError, ToolError, Image.DecompressionBombError) as exc:
        # An unsupported/corrupt document is still an output. Preserve the
        # bytes, but never claim that its type-specific validation succeeded.
        metadata['inspection_error'] = str(exc)
        metadata['kind'] = 'uninspected'
    return metadata


def publish(store, item, path, name, *, identity=None, ownership=None):
    identity = identity or uid('artifact')
    with store.transaction() as db:
        reference = store.assets.import_file(path)
        snapshot = store.assets.resolve(reference)
        metadata = {**describe(snapshot, name), **(ownership or {})}
        existing = db.execute('SELECT * FROM artifacts WHERE id=?', (identity,)).fetchone()
        if existing is not None:
            expected = (item['id'], item['revision'], item['digest'], name, reference['sha256'], reference['size'])
            actual = tuple(existing[key] for key in ('capability_id', 'revision', 'digest', 'name', 'sha256', 'size'))
            if actual != expected or json.loads(existing['metadata']) != metadata:
                raise ToolError('capability_integrity', '不可变产物引用内容冲突')
        else:
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)',
                       (identity, item['id'], item['revision'], item['digest'], name,
                        reference['sha256'], reference['size'], json.dumps(metadata), time.time()))
    return {'id': identity, **reference, **metadata}


def collect(store, item, workspace):
    return {path.relative_to(workspace).as_posix(): publish(store, item, path, path.relative_to(workspace).as_posix())
            for path in files_under(workspace)}


def read(store, args, *, session=None, access=None):
    with store.transaction() as db:
        row = db.execute("SELECT a.* FROM artifacts a JOIN drafts d ON d.id=a.capability_id "
                         "WHERE a.id=? AND (d.scope='user' OR d.project_id=?)",
                         (args['id'], store.project_id)).fetchone()
    if row is None:
        raise ToolError('unknown_artifact', '当前作用域不存在此产物引用')
    metadata = json.loads(row['metadata'])
    if 'owner_session_id' in metadata:
        if (session is None or access is None or session.id != metadata['owner_session_id']
                or session.record.workspace_id != metadata['workspace_id']):
            raise ToolError('permission_denied', '产物不属于当前会话或工作区')
        access.read_reference(metadata['workspace'])
    reference = {'sha256': row['sha256'], 'size': row['size']}
    path = store.assets.resolve(reference)
    result = {'id': row['id'], 'capability_id': row['capability_id'], 'revision': row['revision'],
              'digest': row['digest'], 'name': row['name'], **reference, **json.loads(row['metadata'])}
    mode = args.get('mode', 'metadata')
    if mode == 'metadata':
        return result
    name = Path(row['name']).name.casefold()
    if name == '.env' or name.startswith('.env.'):
        raise ToolError('permission_denied', '不向工具开放密钥文件正文')
    offset, limit = args.get('offset', 0), args.get('limit', 8000)
    if mode == 'text':
        body, info, next_offset = text_ranges.characters(path, offset, limit)
        return {**result, **info, 'text': body, 'offset': offset, 'next_offset': next_offset, 'offset_unit': 'character'}
    if mode != 'base64':
        raise ToolError('validation_error', '产物读取 mode 必须为 metadata/text/base64')
    with path.open('rb') as stream:
        stream.seek(offset)
        body = stream.read(limit)
    next_offset = offset + len(body) if offset + len(body) < reference['size'] else None
    return {**result, 'base64': base64.b64encode(body).decode('ascii'), 'offset': offset,
            'next_offset': next_offset, 'offset_unit': 'byte'}
