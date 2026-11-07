import base64
import hashlib

from PIL import Image
import pytest

from genesisai.core.capabilities.artifacts import collect, read
from genesisai.core.capabilities.store import CapabilityStore
from genesisai.shared.security import ToolError


def setup_store(tmp_path, scope='project'):
    store = CapabilityStore(tmp_path / 'state', 'project-a')
    item = store.create({'kind': 'skill', 'name': 'artifact_fixture', 'description': 'fixture', 'scope': scope,
                         'definition': {}, 'files': {}, 'tests': []})
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    return store, item, workspace


def test_large_text_binary_and_image_outputs_have_immutable_readable_references(tmp_path):
    store, item, workspace = setup_store(tmp_path)
    body = '资料' * 300000 + 'TAIL'
    (workspace / 'large.txt').write_text(body, encoding='utf-8')
    binary = bytes(range(256)) * 2000
    (workspace / 'data.bin').write_bytes(binary)
    Image.new('RGB', (7, 11), 'red').save(workspace / 'image.dat', format='PNG')
    artifacts = collect(store, item, workspace)
    assert set(artifacts) == {'large.txt', 'data.bin', 'image.dat'}
    assert artifacts['large.txt']['size'] > 200000 and artifacts['large.txt']['total_chars'] > 500000
    assert artifacts['data.bin']['sha256'] == hashlib.sha256(binary).hexdigest()
    image = artifacts['image.dat']
    assert (image['kind'], image['format'], image['width'], image['height'], image['media_type']) == ('image', 'png', 7, 11, 'image/png')
    assert all('text' not in entry and 'base64' not in entry for entry in artifacts.values())
    # The recorded artifact survives changes to the working copy.
    (workspace / 'large.txt').write_text('changed', encoding='utf-8')
    page = read(store, {'id': artifacts['large.txt']['id'], 'mode': 'text', 'offset': 599998, 'limit': 6})
    assert page['text'] == '资料TAIL' and page['next_offset'] is None
    chunks, offset = [], 0
    while True:
        page = read(store, {'id': artifacts['data.bin']['id'], 'mode': 'base64', 'offset': offset, 'limit': 100000})
        chunks.append(base64.b64decode(page['base64']))
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert b''.join(chunks) == binary
    restored = CapabilityStore(store.root.parent, 'project-a')
    assert read(restored, {'id': image['id']})['sha256'] == image['sha256']


@pytest.mark.parametrize('scope', ['project', 'user'])
def test_artifact_reads_follow_capability_scope_and_verify_content(tmp_path, scope):
    store, item, workspace = setup_store(tmp_path, scope)
    (workspace / 'note.txt').write_text('hello', encoding='utf-8')
    artifact = collect(store, item, workspace)['note.txt']
    other = CapabilityStore(store.root.parent, 'project-b')
    if scope == 'project':
        with pytest.raises(ToolError, match='当前作用域'):
            read(other, {'id': artifact['id']})
    else:
        assert read(other, {'id': artifact['id'], 'mode': 'text'})['text'] == 'hello'
    (store.assets.root / artifact['sha256']).write_bytes(b'tampered')
    with pytest.raises(ToolError, match='哈希不匹配'):
        read(store, {'id': artifact['id']})


def test_corrupt_formats_and_quota_do_not_silently_drop_outputs(tmp_path):
    store, item, workspace = setup_store(tmp_path)
    (workspace / 'broken.png').write_bytes(b'invalid png')
    artifact = collect(store, item, workspace)['broken.png']
    assert artifact['kind'] == 'uninspected' and artifact['inspection_error']
    assert base64.b64decode(read(store, {'id': artifact['id'], 'mode': 'base64'})['base64']) == b'invalid png'
    store.assets.limits['import_max_bytes'] = 5
    with pytest.raises(ToolError, match='导入配额'):
        collect(store, item, workspace)
    assert (workspace / 'broken.png').read_bytes() == b'invalid png'
    assert read(store, {'id': artifact['id']})['sha256'] == artifact['sha256']


def test_artifact_reader_does_not_open_environment_file_content(tmp_path):
    store, item, workspace = setup_store(tmp_path)
    (workspace / '.env').write_text('FIXTURE=value', encoding='utf-8')
    artifact = collect(store, item, workspace)['.env']
    with pytest.raises(ToolError, match='密钥文件'):
        read(store, {'id': artifact['id'], 'mode': 'text'})
