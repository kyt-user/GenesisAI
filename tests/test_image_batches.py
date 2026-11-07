"""Original-resolution image views and resource-budgeted pixel batches."""
import base64
import copy
from dataclasses import asdict, replace
import hashlib
import io
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from genesisai.agent.loop import Runner
from genesisai.app.attachments import Attachments
from genesisai.core.media.images import ImageStore, prepare_image
from genesisai.core.persistence.session import Session
from genesisai.core.prompt.compaction import estimate_tokens, fingerprint, project_images, restore_images
from genesisai.core.prompt.composer import PromptComposer
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.shared.context_config import ContextConfig
from genesisai.shared.messages import Response, UserInput, content_text, image_parts, image_tokens
from genesisai.shared.security import ToolError
from test_acceptance import call, response
from test_image_assets import image_runtime
from test_multimodal_runtime import visual_model


def picture(size=(32, 32), color='red', **save_options):
    stream = io.BytesIO()
    Image.new('RGB', size, color).save(stream, format='PNG', **save_options)
    return stream.getvalue()


def parts(runtime, count=6, size=(32, 32)):
    return [runtime.images.import_image(prepare_image(picture(size, (i, 40, 90)), f'{i}.png', runtime.images.config))
            for i in range(count)]


def pixels(messages):
    return [p for m in messages for p in image_parts(m.content)]


def test_more_than_four_images_reach_model_and_pending_budget_is_separate(tmp_path):
    store, runtime = image_runtime(tmp_path)
    config = replace(runtime.images.config, max_images=2, max_request_image_bytes=1)
    draft = Attachments()
    for i in range(6):
        path = runtime.access.workspace / f'{i}.png'
        path.write_bytes(picture(color=(i, 40, 90)))
        draft.add(path, runtime.access, config)
    assert len(draft.request('').images) == 6
    model = visual_model(Response(content='seen'))
    try:
        assert Runner(model, runtime).start(draft.request('describe all'))['status'] == 'completed'
        assert len(pixels(model.contexts[0])) == 6
        assert len(image_parts(store.record.messages[0]['content'])) == 6
        with pytest.raises(ValueError, match='内存配额'):
            draft.add(path, runtime.access, replace(config, max_pending_image_bytes=1))
    finally:
        runtime.close()


def test_omitted_reference_read_is_prioritized_over_original_batch(tmp_path):
    store, runtime = image_runtime(tmp_path)
    attachments = tuple(prepare_image(picture(color=(i, 40, 90)), f'{i}.png', runtime.images.config) for i in range(6))
    requested = {}
    def request_more(messages):
        user = next(m for m in reversed(messages) if m.role == 'user')
        assert len(image_parts(user.content)) == 2
        remaining = user.metadata['image_projection']
        assert len(remaining) == 4
        requested['ref'] = remaining[-1]['image_ref']
        assert all(p['image_ref'] in content_text(user.content) for p in remaining)
        return response(call('read_image', {'image_ref': requested['ref']}, 'read_next'))
    def inspect(messages):
        tool = next(m for m in reversed(messages) if m.role == 'tool')
        assert image_parts(tool.content)[0]['image_ref'] == requested['ref']
        assert len(pixels(messages)) == 2
        return Response(content='next image seen')
    model = visual_model(request_more, inspect)
    model.context_config = {'max_images': 2}
    try:
        result = Runner(model, runtime).start(UserInput('inspect images', attachments))
        assert result['status'] == 'completed', result
        assert len(image_parts(store.record.messages[0]['content'])) == 6
    finally:
        runtime.close()


def test_wire_bytes_and_visual_tokens_project_without_losing_refs(tmp_path):
    _, runtime = image_runtime(tmp_path)
    images = parts(runtime)
    raw = [{'role': 'user', 'content': images}]
    try:
        for kwargs in ({'max_bytes': sum(p['byte_size'] for p in images[:2])},
                       {'max_bytes': 100000, 'max_tokens': sum(image_tokens(p) for p in images[:2])}):
            view = project_images(raw, None, **kwargs)
            assert len(image_parts(view[0]['content'])) == 2
            assert all(p['image_ref'] in content_text(view[0]['content']) for p in images[2:])
            assert restore_images(view, raw)[0]['content'] == images
        assert raw[0]['content'] == images
    finally:
        runtime.close()


def test_current_tool_batch_survives_compaction_and_larger_budget_restores_images(tmp_path):
    store, runtime = image_runtime(tmp_path)
    images = parts(runtime, size=(1536, 1536))
    store.record.messages = [
        {'role': 'user', 'content': 'inspect the full batch'},
        {'role': 'assistant', 'content': 'old context ' * 5000},
        {'role': 'assistant', 'tool_calls': [call('read_image', {'path': 'test.png'}, 'batch')]},
        {'role': 'tool', 'tool_call_id': 'batch', 'content': images},
    ]
    raw = copy.deepcopy(store.record.messages)
    budget = ContextBudgeter(store, PromptComposer(), config={
        'max_input_tokens': 18000, 'history_tokens': 1000, 'preserve_recent_tokens': 1000, 'strategy': 'basic'})
    try:
        first = budget.prepare(budget.build('local_files'), [])
        assert 0 < len(pixels(first)) < 6
        assert store.record.context_checkpoint
        assert store.record.messages == raw
        assert budget.request_tokens(first, []) <= budget.config.input_limit
        assert len(image_parts(next(m.content for m in first if m.role == 'tool'))) == len(pixels(first))
        reopened = Session(store.root, store.id)
        larger = ContextBudgeter(reopened, PromptComposer(), config={'max_input_tokens': 100000, 'strategy': 'basic'})
        second = larger.prepare(larger.build('local_files'), [])
        assert len(pixels(second)) == 6
        assert reopened.record.messages == raw
    finally:
        runtime.close()


def test_combined_text_and_image_projection_recovers_original_slots(tmp_path):
    store, runtime = image_runtime(tmp_path)
    images = parts(runtime)
    content = [{'type': 'text', 'text': 'large text ' * 5000}, images[0],
               {'type': 'text', 'text': 'second block ' * 5000}, *images[1:]]
    store.record.messages = [{'role': 'user', 'content': content}]
    small = ContextBudgeter(store, PromptComposer(), config={'max_input_tokens': 12000, 'max_images': 2, 'strategy': 'basic'})
    try:
        first = small.prepare(small.build('direct_answer'), [])
        user = next(m for m in reversed(first) if m.role == 'user')
        assert user.metadata['input_projection'] and user.metadata['image_projection']
        store.record.context_checkpoint = {'source_count': 1, 'source_hash': fingerprint(store.record.messages),
                                           'messages': [asdict(user)]}
        store.commit()
        reopened = Session(store.root, store.id)
        large = ContextBudgeter(reopened, PromptComposer(), config={'max_input_tokens': 100000})
        second = large.prepare(large.build('direct_answer'), [])
        assert next(m.content for m in reversed(second) if m.role == 'user') == content
    finally:
        runtime.close()


def test_original_region_is_readable_after_source_changes_and_session_restore(tmp_path):
    store, runtime = image_runtime(tmp_path)
    source = Image.new('RGB', (3000, 1800), 'white')
    source.putpixel((2200, 1200), (255, 0, 0))
    path = runtime.access.workspace / 'detail.png'
    source.save(path)
    original = path.read_bytes()
    first = runtime.images.read(path=path)
    ref = first['images'][0]['image_ref']
    assert first['images'][0]['width'] == 1536
    assert first['original']['width'] == 3000
    assert first['original']['sha256'] == hashlib.sha256(original).hexdigest()
    path.write_bytes(b'original local file changed')
    other = Session(store.root, workspace=runtime.access.workspace)
    restored = ImageStore(other, runtime.access)
    crop = {'x': 2199, 'y': 1199, 'width': 3, 'height': 3}
    try:
        result = restored.read(image_ref=ref, crop=crop, max_side=6000)
        assert result['region'] == [2199, 1199, 3, 3]
        view = result['images'][0]
        assert (view['width'], view['height']) == (3, 3)
        payload = base64.b64decode(restored.data_url(view).split(',', 1)[1])
        with Image.open(io.BytesIO(payload)) as decoded:
            assert decoded.getpixel((1, 1)) == (255, 0, 0)
        full = restored.read(image_ref=view['image_ref'], max_side=3000)
        assert (full['images'][0]['width'], full['images'][0]['height']) == (3000, 1800)
        assert full['region'] is None
        assert restored.data_url(full['images'][0])
    finally:
        runtime.close()


def test_exif_coordinates_and_invalid_regions():
    original = Image.new('RGB', (30, 10), 'blue')
    original.putpixel((0, 0), (255, 0, 0))
    exif = Image.Exif()
    exif[274] = 6
    stream = io.BytesIO()
    original.save(stream, format='PNG', exif=exif)
    raw = stream.getvalue()
    image = prepare_image(raw, 'oriented.png', ContextConfig(), crop={'x': 9, 'y': 0, 'width': 1, 'height': 1})
    assert (image.original_width, image.original_height) == (10, 30)
    with Image.open(io.BytesIO(image.data)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 0, 0)
    for crop in ({'x': 10, 'y': 0, 'width': 1, 'height': 1}, {'x': -1, 'y': 0, 'width': 1, 'height': 1},
                 {'x': True, 'y': 0, 'width': 1, 'height': 1}, {'x': 0, 'y': 0, 'width': 0, 'height': 1}):
        with pytest.raises(ValueError, match='crop'):
            prepare_image(raw, 'test.png', ContextConfig(), crop=crop)
    for side in (True, 0, 1.5):
        with pytest.raises(ValueError, match='max_side'):
            prepare_image(raw, 'test.png', ContextConfig(), max_side=side)


def test_original_integrity_and_journal_redaction(tmp_path):
    store, runtime = image_runtime(tmp_path)
    original = picture((1800, 1000))
    image = prepare_image(original, 'test.png', runtime.images.config)
    part = runtime.images.import_image(image)
    from genesisai.core.state.io_journal import IOJournal
    clean = IOJournal(store).clean(UserInput(images=(image,)))
    assert clean['images'][0]['original'] == {'sha256': hashlib.sha256(original).hexdigest(), 'byte_size': len(original)}
    assert clean['images'][0]['data']['byte_size'] == len(image.data)
    snapshot = runtime.images.root / 'originals' / hashlib.sha256(original).hexdigest()
    snapshot.write_bytes(b'corrupted fixture')
    try:
        with pytest.raises(ValueError, match='原图快照损坏'):
            runtime.images.read(image_ref=part['image_ref'], max_side=1800)
        with pytest.raises(ValueError, match='未覆盖'):
            runtime.images.import_image(image)
        assert snapshot.read_bytes() == b'corrupted fixture'
    finally:
        runtime.close()


def test_mcp_images_use_pending_quota_not_wire_count(tmp_path):
    _, runtime = image_runtime(tmp_path)
    model = visual_model()
    model.context_config = {'max_images': 2, 'max_request_image_bytes': 1}
    Runner(model, runtime)
    raw = {'content': [{'type': 'image', 'mimeType': 'image/png',
                        'data': base64.b64encode(picture(color=(i, 40, 90))).decode()} for i in range(6)]}
    runtime.mcp.transports['fixture'] = SimpleNamespace(request=lambda *a: raw, close=lambda: None)
    try:
        result = runtime.mcp.call('fixture', 'screen', {})
        assert len(result['images']) == len(result['source_refs']) == 6
        runtime.images.config = replace(runtime.images.config, max_pending_image_bytes=1)
        with pytest.raises(ToolError, match='输入资源配额'):
            runtime.mcp.call('fixture', 'screen', {})
    finally:
        runtime.close()
