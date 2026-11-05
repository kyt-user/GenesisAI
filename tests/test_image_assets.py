import json
"""Use the supplied pet sheet to exercise actual decoding and persisted pixels."""
import base64
from dataclasses import replace
import hashlib
import io
from pathlib import Path

from PIL import Image
import pytest

from genesisai.core.media.images import prepare_image, prepare_path
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.context_config import ContextConfig
from genesisai.shared.security import Access, ToolError


SPRITE = Path(__file__).resolve().parents[1] / 'src/genesisai/app/assets/pet/spritesheet.webp'


def image_runtime(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    roots = [workspace, SPRITE]
    store.record.apply(grants=[str(p) for p in roots], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(store, Access(roots, workspace, store.root, workspace_root=workspace), providers=[], permission_mode='user')
    return store, runtime


def test_supplied_webp_is_bounded_and_restores_without_original(tmp_path):
    original = hashlib.sha256(SPRITE.read_bytes()).hexdigest()
    store, runtime = image_runtime(tmp_path)
    attachment = prepare_path(SPRITE, runtime.access, runtime.images.config)
    assert (attachment.width, attachment.height) == (1260, 1536)
    part = runtime.images.import_image(attachment)
    url = runtime.images.data_url(part)
    payload = base64.b64decode(url.split(',', 1)[1])
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.format == 'PNG' and decoded.size == (1260, 1536)
    store.record.messages = [{'role': 'user', 'content': [part]}]
    store.commit()
    restored = Session(store.root, store.id)
    from genesisai.core.media.images import ImageStore
    assert ImageStore(restored, runtime.access).data_url(part) == url
    assert 'base64' not in json.dumps(store.snapshot())
    assert hashlib.sha256(SPRITE.read_bytes()).hexdigest() == original


def test_same_asset_is_reused_and_corruption_is_not_overwritten(tmp_path):
    store, runtime = image_runtime(tmp_path)
    image = prepare_path(SPRITE, runtime.access, runtime.images.config)
    part = runtime.images.import_image(image)
    assert runtime.images.import_image(image) == part
    path, = runtime.images.root.glob('*.png')
    path.write_bytes(b'corrupt fixture')
    with pytest.raises(ValueError, match='损坏'):
        runtime.images.data_url(part)
    with pytest.raises(ValueError, match='未覆盖'):
        runtime.images.import_image(image)
    assert path.read_bytes() == b'corrupt fixture'


@pytest.mark.parametrize('change,match', [({'max_image_bytes': 10}, '大小'), ({'max_image_pixels': 100}, '像素')])
def test_limits_are_enforced_before_decode(change, match):
    with pytest.raises(ValueError, match=match):
        prepare_image(SPRITE.read_bytes(), SPRITE.name, replace(ContextConfig(), **change))


def test_non_image_animation_and_mime_mismatch_are_rejected():
    config = ContextConfig()
    for payload in (b'video fixture', b'RIFF\0\0\0\0WEBPbroken'):
        with pytest.raises(ValueError):
            prepare_image(payload, 'test.mp4', config)
    with pytest.raises(ValueError, match='MIME'):
        prepare_image(SPRITE.read_bytes(), 'image.png', config, mime_type='image/png')
    output = io.BytesIO()
    Image.new('RGB', (3, 3), 'red').save(output, format='PNG', save_all=True,
                                      append_images=[Image.new('RGB', (3, 3), 'blue')], duration=100, loop=0)
    with pytest.raises(ValueError, match='动画'):
        prepare_image(output.getvalue(), 'animation.png', config)


def test_path_permission_and_forged_metadata(tmp_path):
    store, runtime = image_runtime(tmp_path)
    outside = tmp_path / 'outside.webp'
    outside.write_bytes(SPRITE.read_bytes())
    with pytest.raises(ToolError, match='授权'):
        prepare_path(outside, runtime.access, runtime.images.config)
    part = runtime.images.read(path=SPRITE)['images'][0]
    with pytest.raises(ValueError, match='元数据'):
        runtime.images.data_url({**part, 'width': 1})
    store.record.messages = [{'role': 'user', 'content': [{**part, 'byte_size': 1}]}]
    with pytest.raises(ValueError, match='元数据'):
        store.commit()
