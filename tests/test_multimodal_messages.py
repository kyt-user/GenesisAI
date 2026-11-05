import base64
import io
import json

import httpx
from openai import OpenAI
from PIL import Image
import pytest

from genesisai.model.capabilities import image_capability
from genesisai.model.providers.qwen import QwenClient
from genesisai.shared.messages import Message, ToolCall
from test_image_assets import SPRITE, image_runtime


def client_for(runtime, handler):
    model = QwenClient('qwen3-vl-plus', api_key='fixture-key', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1')
    model.client.close()
    model.client = OpenAI(api_key='fixture-key', base_url='https://dashscope.aliyuncs.com/compatible-mode/v1',
                          http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    model.image_resolver = runtime.images.data_url
    return model


def completion(body, content='seen'):
    if body.get('stream'):
        chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 0, 'model': body['model'],
                 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                              text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')
    return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': body['model'],
                                   'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})


def test_sync_and_stream_deliver_real_pixels_and_log_only_refs(tmp_path):
    store, runtime = image_runtime(tmp_path)
    part = runtime.images.read(path=SPRITE)['images'][0]
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return completion(body)
    model = client_for(runtime, handle)
    model.bind_logging(runtime.logger)
    messages = [Message('user', [{'type': 'text', 'text': 'describe'}, part])]
    try:
        assert model.chat(messages).content == 'seen'
        assert list(model.stream_chat(messages))[-1].content == 'seen'
    finally:
        model.client.close()
    assert requests[0]['messages'] == requests[1]['messages']
    url = requests[0]['messages'][0]['content'][1]['image_url']['url']
    with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
        assert image.size == (1260, 1536)
    assert 'base64' not in runtime.logger.journal.path.read_text(encoding='utf8')


def test_tool_images_follow_all_parallel_results_and_keep_call_identity(tmp_path):
    _, runtime = image_runtime(tmp_path)
    part = runtime.images.read(path=SPRITE)['images'][0]
    model = client_for(runtime, lambda r: completion(json.loads(r.content)))
    messages = [Message('user', 'look'), Message('assistant', tool_calls=[ToolCall('a', 'read_image', '{}'), ToolCall('b', 'list_files', '{}')]),
                Message('tool', [{'type': 'text', 'text': '{"ok":true}'}, part], tool_call_id='a'),
                Message('tool', '{}', tool_call_id='b')]
    try:
        wire = model._request_messages(messages)
        assert [m['role'] for m in wire] == ['user', 'assistant', 'tool', 'tool', 'user']
        assert wire[2]['tool_call_id'] == 'a' and isinstance(wire[2]['content'], str)
        assert part['image_ref'] in wire[-1]['content'][1]['text'] and 'a' in wire[-1]['content'][1]['text']
        assert '不是用户新指令' in wire[-1]['content'][0]['text']
        with pytest.raises(ValueError, match='未全部'):
            model._request_messages(messages[:-1])
    finally:
        model.client.close()


def test_unknown_model_and_raw_video_are_rejected_before_http(tmp_path):
    _, runtime = image_runtime(tmp_path)
    part = runtime.images.read(path=SPRITE)['images'][0]
    model = client_for(runtime, lambda r: pytest.fail('must not send HTTP'))
    try:
        model.image_capability = image_capability('qwen', 'custom', str(model.client.base_url))
        with pytest.raises(ValueError, match='未确认'):
            model.chat([Message('user', [part])])
        with pytest.raises(ValueError, match='仅支持'):
            model.chat([Message('user', [{'type': 'video', 'url': 'video.mp4'}])])
    finally:
        model.client.close()


def test_capabilities_are_bound_to_provider_model_and_endpoint():
    endpoint = 'https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1'
    assert image_capability('qwen_token_plan', 'qwen3.8-flash', endpoint).supported is True
    assert image_capability('qwen_token_plan', 'auto', endpoint).supported is None
    assert image_capability('qwen', 'qwen3-vl-plus', 'https://unverified.example/v1').supported is None
