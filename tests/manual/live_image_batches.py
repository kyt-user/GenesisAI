"""Opt-in Qwen Token Plan acceptance: six images, tool crop, restored original."""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import secrets
import time

from PIL import Image, ImageDraw, ImageFont

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.media.images import prepare_path
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.shared.ids import uid
from genesisai.shared.messages import UserInput, image_parts
from genesisai.shared.security import Access


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', required=True)
    parser.add_argument('--model', default='qwen3.8-flash')
    args = parser.parse_args()
    load_environment()
    saved = load_settings()
    if saved is None or saved.provider != 'qwen_token_plan':
        saved = ProviderSettings(provider='qwen_token_plan', model=args.model)
    settings = replace(saved, model=args.model, generation={'max_tokens': 2048},
                       context={'max_images': 2, 'image_max_side': 128},
                       request_timeouts={'connect': 30, 'first_token': 120, 'idle': 120, 'total': 240})
    model, _ = build_model(settings=settings)
    if model.provider != 'qwen_token_plan' or model.image_capability.supported is not True:
        raise ValueError('本验收必须使用已确认支持视觉的 Qwen Token Plan 模型')
    root = Path(__file__).resolve().parents[2] / 'temp' / uid('live_image_batch')
    workspace = root / 'workspace'
    workspace.mkdir(parents=True)
    session = Session(root / 'state', workspace=workspace)
    session.record.apply(grants=[str(workspace)], output=str(workspace))
    session.commit()
    access = Access([workspace], workspace, session.root, workspace_root=workspace)
    runtime = ToolRuntime(session, access, providers=[], permission_mode='user')
    report = {'provider': model.provider, 'model': model.model, 'session_id': session.id,
              'started': time.time(), 'checks': {}, 'passed': False}
    restored_runtime = None
    try:
        code = str(secrets.randbelow(900000) + 100000)
        font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 38)
        paths = []
        for i in range(6):
            image = Image.new('RGB', (2400, 1400), 'white')
            draw = ImageDraw.Draw(image)
            draw.text((50, 50), str(i + 1), fill='black', font=font)
            if i == 5:
                draw.text((920, 620), code, fill='black', font=font)
            path = workspace / f'{i+1}.png'
            image.save(path)
            paths.append(path)
        source_hash = hashlib.sha256(paths[-1].read_bytes()).hexdigest()
        def progress(kind, payload):
            if kind == 'model':
                print('model_round', payload.get('round'), flush=True)
            elif kind == 'tool_result':
                print('tool_result', payload.get('ok'), (payload.get('error') or {}).get('code'), flush=True)
        runner = Runner(model, runtime, seconds=480, max_rounds=10, max_tools=12,
                        token_budget=60000, on_event=progress)
        attachments = tuple(prepare_path(path, access, runtime.images.config) for path in paths)
        request = UserInput('本次是图片读取验收。共有6张图片。请找到第6张图片（文件名6.png）的引用，'
            '实际调用 read_image，以 image_ref 读取原图区域 crop={"x":900,"y":600,"width":400,"height":140}，'
            'max_side=800。缩略图不可作为细节依据。读取后只回答区域里的6位数字；不要猜测、联网或修改文件。', attachments)
        outcome = runner.start(request)
        first_answer = outcome['answer']
        original_parts = image_parts(session.record.messages[0]['content'])
        sixth = original_parts[-1]
        checks = report['checks']
        checks['six_originals_retained'] = len(original_parts) == 6
        checks['completed'] = outcome['status'] == 'completed'
        checks['observed_random_digits'] = code in first_answer
        crop_calls = [entry for entry in session.record.calls.values()
                      if (entry.get('call') or {}).get('name') == 'read_image']
        checks['actual_crop_tool'] = any((entry.get('result') or {}).get('ok') is True
            and json.loads(entry['call']['arguments']).get('image_ref') == sixth['image_ref']
            and json.loads(entry['call']['arguments']).get('crop') == {'x': 900, 'y': 600, 'width': 400, 'height': 140}
            for entry in crop_calls)
        report['first'] = {'outcome': outcome, 'answer': first_answer, 'crop_calls': crop_calls}
        # Modify only this script's generated input. Recovery must use the snapshot.
        paths[-1].write_bytes(b'generated local source changed after import')
        restored = Session(session.root, session.id)
        restored_runtime = ToolRuntime(restored, access, providers=[], permission_mode='user')
        again = Runner(model, restored_runtime, seconds=480, max_rounds=8, max_tools=10,
                       token_budget=60000, on_event=progress)
        before_calls = set(restored.record.calls)
        followup = again.start('请再实际调用 read_image 核验原图快照 ' + sixth['image_ref'] +
            '，crop={"x":900,"y":600,"width":400,"height":140}, max_side=800。不要复述历史答案；读取像素后只回复6位数字。')
        answer = followup['answer']
        checks['restored_completed'] = followup['status'] == 'completed'
        checks['restored_digits'] = code in answer
        checks['restored_actual_read'] = any(entry.get('result', {}).get('ok') is True
            and (entry.get('call') or {}).get('name') == 'read_image'
            for identity, entry in restored.record.calls.items() if identity not in before_calls)
        snapshot = restored_runtime.images.root / 'originals' / source_hash
        checks['original_snapshot_unchanged'] = hashlib.sha256(snapshot.read_bytes()).hexdigest() == source_hash
        checks['no_inline_media_in_log'] = 'base64,' not in runtime.logger.journal.path.read_text(encoding='utf-8')
        checks['no_inline_media_in_session'] = 'base64,' not in json.dumps(restored.snapshot())
        reports = [json.loads(row['payload']) for row in restored.database.rows(
            'SELECT payload FROM context_manifests WHERE session_id=?', (session.id,))]
        checks['request_batches_at_most_two'] = bool(reports) and all(r['image_count'] <= 2 for r in reports)
        checks['omitted_refs_explicit'] = any(len(r.get('image_references', [])) >= 4 for r in reports)
        report['expected_digits'] = code
        report['restored'] = {'outcome': followup, 'answer': answer}
        report['passed'] = all(checks.values())
    except BaseException as exc:
        runtime.logger.error('live_image_batch_error', exc)
        report['error'] = runtime.logger.journal.clean(str(exc))
    finally:
        report['finished'] = time.time()
        path = root / 'report.json'
        path.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding='utf-8')
        if restored_runtime:
            restored_runtime.close()
        runtime.close()
        print(json.dumps({'report': str(path), 'passed': report['passed'], 'checks': report['checks'],
                          'error': report.get('error')}, ensure_ascii=False), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
