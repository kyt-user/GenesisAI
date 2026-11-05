import base64
from dataclasses import replace
import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from genesisai.agent.loop import Runner
from genesisai.app.attachments import Attachments
from genesisai.app.session_picker import session_entries
from genesisai.app.terminal_view import CliView
from genesisai.core.media.images import prepare_path
from genesisai.core.prompt.compaction import image_parts, project, project_images, estimate_tokens, execution_facts
from genesisai.core.persistence.session import Session
from genesisai.model.capabilities import ImageCapability
from genesisai.shared.messages import Response, UserInput, content_text
from genesisai.shared.security import ToolError
from test_acceptance import FakeModel, call, response
from test_image_assets import SPRITE, image_runtime


def visual_model(*responses):
    model = FakeModel(*responses)
    model.image_capability = ImageCapability(True, 'fixture')
    return model


def test_image_only_turn_followup_restore_history_and_source_citation(tmp_path):
    store, runtime = image_runtime(tmp_path)
    image = prepare_path(SPRITE, runtime.access, runtime.images.config)
    def describe(messages):
        part = image_parts(messages[-1].content)[0]
        assert runtime.images.data_url(part).startswith('data:image/png;base64,')
        return Response(content='图像说明 ' + part['image_ref'])
    model = visual_model(describe, Response(content='followup'))
    runner = Runner(model, runtime)
    assert runner.start(UserInput(images=(image,)))['status'] == 'completed'
    assert runner.start('同一张图继续说明')['status'] == 'completed'
    assert any(image_parts(m.content) for m in model.contexts[-1])
    restored = Session(store.root, store.id)
    from genesisai.core.tools.tool_runtime import ToolRuntime
    restored_runtime = ToolRuntime(restored, runtime.access, providers=[])
    again = visual_model(Response(content='restored'))
    assert Runner(again, restored_runtime).start('继续')['status'] == 'completed'
    assert any(image_parts(m.content) for m in again.contexts[-1])
    screen = io.StringIO()
    console = Console(file=screen)
    entries = session_entries(console, store.root)
    assert len(entries) == 1 and SPRITE.name in entries[0][1]
    CliView(console).render_history(restored.snapshot())
    assert SPRITE.name in screen.getvalue()
    assert 'data:image' not in json.dumps(store.snapshot())


def test_local_tool_pixels_reach_next_model_turn_and_can_reload_after_compaction(tmp_path):
    store, runtime = image_runtime(tmp_path)
    def check(messages):
        tool = next(m for m in reversed(messages) if m.role == 'tool')
        part = image_parts(tool.content)[0]
        assert runtime.images.data_url(part)
        return Response(content='read ' + part['image_ref'])
    model = visual_model(response(call('read_image', {'path': str(SPRITE)}, 'read')), check)
    assert Runner(model, runtime).start('查看本地图片')['status'] == 'completed'
    raw = store.record.messages
    tool = next(m for m in raw if m['role'] == 'tool')
    ref = image_parts(tool['content'])[0]['image_ref']
    assert execution_facts(raw)[0]['ok'] is True
    raw.append({'role': 'user', 'content': '请继续'})
    projected, info = project(raw, 8000, 2000)
    assert info['folded']
    assert not any(image_parts(m.get('content')) for m in projected)
    assert ref in json.dumps(projected)
    assert runtime.images.read(image_ref=ref)['images'][0]['image_ref'] == ref
    assert estimate_tokens(raw) > estimate_tokens(projected)


def test_request_image_count_is_bounded_without_mutating_full_history(tmp_path):
    _, runtime = image_runtime(tmp_path)
    part = runtime.images.read(path=SPRITE)['images'][0]
    history = []
    for i in range(6):
        history.extend([{'role': 'user', 'content': [{'type': 'text', 'text': str(i)}, part]}, {'role': 'assistant', 'content': 'seen'}])
    projection = project_images(history, 2, 10000000)
    assert sum(len(image_parts(m.get('content'))) for m in projection) == 2
    assert sum(len(image_parts(m.get('content'))) for m in history) == 6
    with pytest.raises(ValueError, match='必要图片'):
        project_images([{'role': 'user', 'content': [part, part, part]}], 2, 10000000)


def test_mcp_mixed_image_text_is_not_discarded_and_unsupported_types_fail(tmp_path):
    _, runtime = image_runtime(tmp_path)
    Runner(visual_model(), runtime)
    raw = {'content': [{'type': 'text', 'text': 'screen'}, {'type': 'image', 'mimeType': 'image/webp',
                                                        'data': base64.b64encode(SPRITE.read_bytes()).decode()}]}
    runtime.mcp.transports['fixture'] = SimpleNamespace(request=lambda *a: raw)
    result = runtime.mcp.call('fixture', 'screen', {})
    assert result['text'] == 'screen' and len(result['images']) == 1
    from genesisai.shared.results import result_content
    content = result_content({'ok': True, 'data': result})
    assert image_parts(content) == result['images'] and 'screen' in content_text(content)
    raw['content'] = [{'type': 'audio', 'data': 'fixture'}]
    with pytest.raises(ToolError, match='未支持'):
        runtime.mcp.call('fixture', 'screen', {})


def test_frozen_attachment_survives_queue_draft_changes_and_registration_failure(tmp_path, monkeypatch):
    store, runtime = image_runtime(tmp_path)
    local = runtime.access.workspace / 'queued.webp'
    local.write_bytes(SPRITE.read_bytes())
    draft = Attachments()
    first = draft.add(local, runtime.access, runtime.images.config)
    request = draft.request('queued')
    local.write_bytes(b'changed after enqueue')
    second = draft.add(SPRITE, runtime.access, runtime.images.config)
    draft.consume(request.images)
    assert draft.request('').images == (second,)
    assert request.images == (first,)
    runner = Runner(visual_model(Response(content='done')), runtime)
    save = store.commit
    def fail_registration():
        if store.record.messages:
            raise OSError('registration fixture')
        save()
    monkeypatch.setattr(store, 'commit', fail_registration)
    with pytest.raises(OSError):
        runner.start(request)
    assert not store.record.messages
    monkeypatch.setattr(store, 'commit', save)
    assert runner.start(request)['status'] == 'completed'


def test_unsupported_model_retains_input_and_does_not_register_turn(tmp_path):
    store, runtime = image_runtime(tmp_path)
    request = UserInput(images=(prepare_path(SPRITE, runtime.access, runtime.images.config),))
    with pytest.raises(ValueError, match='未确认'):
        Runner(FakeModel(), runtime).start(request)
    assert not store.record.messages and (not store.record.sources)


def test_public_input_rejects_mutable_or_untyped_attachment_collection():
    with pytest.raises(ValueError, match='不可变'):
        UserInput(images=[])
    with pytest.raises(ValueError, match='不可变'):
        UserInput(images=('unvalidated-path.webp',))
