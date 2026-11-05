"""Opt-in live image acceptance; uses an isolated session and only the given image."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from genesisai.app.cli import load_environment
from genesisai.core.media.images import prepare_path
from genesisai.core.prompt.context_budgeter import ContextBudgeter
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import load_settings
from genesisai.shared.messages import Message, ToolCall
from genesisai.shared.results import result_content
from genesisai.shared.security import Access


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True, help='Send the specified image to the configured model')
    parser.add_argument('--image', type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    image = args.image.resolve(strict=True)
    original = hashlib.sha256(image.read_bytes()).hexdigest()
    load_environment(workspace=project)
    model, _ = build_model(settings=load_settings())
    if model.image_capability.supported is not True:
        model.client.close()
        raise ValueError(model.image_capability.reason)
    root = project / 'temp' / ('live-image-' + uuid4().hex)
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    store = Session(root / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace), str(image)], output=str(workspace))
    access = Access([workspace, image], workspace, store.root, workspace_root=workspace)
    runtime = ToolRuntime(store, access, providers=[])
    runtime.supports_images = True
    from genesisai.shared.context_config import ContextConfig
    runtime.images.config = ContextConfig.parse(model.context_config)
    model.image_resolver = runtime.images.data_url
    model.bind_logging(runtime.logger, purpose='live_image_acceptance')
    report = {'provider': model.provider, 'model': model.model, 'source_image': str(image), 'checks': {}, 'answers': {}}
    options = {'max_tokens': 500, 'extra_body': {'enable_thinking': False}, 'timeout': 60}
    try:
        part = runtime.images.import_image(prepare_path(image, access, runtime.images.config))
        messages = [Message('user', [{'type': 'text', 'text': '请实际观察图片，用两句话描述主要角色的外形、颜色以及整张图片的排列方式。'}, part])]
        first = model.chat(messages, tools=[], **options)
        report['answers']['direct_image'] = first.content
        report['checks']['direct_image'] = bool(first.content) and first.finish_reason == 'stop'
        store.record.messages = [asdict(messages[0]), {'role': 'assistant', 'content': first.content}]
        store.commit()
        restored = Session(store.root, store.id)
        restored_runtime = ToolRuntime(restored, access, providers=[])
        restored_runtime.images.config = runtime.images.config
        model.image_resolver = restored_runtime.images.data_url
        history = ContextBudgeter._messages(restored.record.messages) + [Message('user', '继续看同一张图片：角色有哪些不同姿势或朝向？简短回答。')]
        second = list(model.stream_chat(history, tools=[], **options))[-1]
        report['answers']['restored_followup'] = second.content
        report['checks']['restored_followup'] = bool(second.content) and second.finish_reason == 'stop'
        tool = {'id': 'image_acceptance', 'name': 'read_image', 'arguments': json.dumps({'image_ref': part['image_ref']})}
        restored_runtime.supports_images = True
        result = restored_runtime.execute(tool)
        assert result['ok'], result
        tool_messages = [Message('user', '读取图片后说明角色的外形和颜色。'),
                         Message('assistant', tool_calls=[ToolCall(**tool)]),
                         Message('tool', result_content(result), tool_call_id=tool['id'])]
        third = model.chat(tool_messages, tools=[], **options)
        report['answers']['tool_image'] = third.content
        report['checks']['tool_image'] = bool(third.content) and third.finish_reason == 'stop'
        report['checks']['original_unchanged'] = original == hashlib.sha256(image.read_bytes()).hexdigest()
        report['checks']['no_inline_media_in_log'] = 'base64,' not in runtime.logger.journal.path.read_text(encoding='utf8')
        report['checks']['no_inline_media_in_session'] = 'base64,' not in json.dumps(store.snapshot())
        report['normalized_image'] = part
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        model.client.close()
        runtime.mcp.close()
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print('Report:', root / 'report.json', flush=True)
    return 0 if all(report['checks'].values()) and len(report['checks']) == 6 else 2


if __name__ == '__main__':
    raise SystemExit(main())
