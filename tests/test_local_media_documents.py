"""Real local OCR/FFmpeg and password input boundaries using generated files."""
import json
import shutil

import pytest

from genesisai.app.document_commands import execute_pdf_unlock
from genesisai.core.extensions.tools.media import helpers as media
from genesisai.shared.security import ToolError
from test_upgrade_p2_p6 import runtime_for, execute


@pytest.fixture
def runtime(tmp_path):
    workspace, output, store, runtime = runtime_for(tmp_path)
    try: yield workspace, output, store, runtime
    finally: runtime.close()


def test_pdf_unlock_preserves_source_and_keeps_password_out_of_records(runtime):
    from pypdf import PdfReader, PdfWriter
    workspace, output, store, runtime = runtime
    source = workspace / 'encrypted.pdf'
    writer = PdfWriter(); writer.add_blank_page(300, 400); writer.encrypt('SECRET_ONLY_TERMINAL_123')
    writer.write(source); writer.close()
    original = source.read_bytes()
    with pytest.raises(ToolError, match='密码错误'):
        execute_pdf_unlock(runtime, '/pdf-unlock encrypted.pdf unlocked.pdf', password_reader=lambda: 'wrong')
    assert not (output / 'unlocked.pdf').exists()
    result = execute_pdf_unlock(runtime, '/pdf-unlock encrypted.pdf unlocked.pdf', password_reader=lambda: 'SECRET_ONLY_TERMINAL_123')
    assert result['decrypted'] and len(PdfReader(result['path']).pages) == 1
    assert not PdfReader(result['path']).is_encrypted
    assert source.read_bytes() == original
    from dataclasses import asdict
    assert 'SECRET_ONLY_TERMINAL_123' not in json.dumps(asdict(store.record), ensure_ascii=False, default=str)
    with pytest.raises(ToolError, match='已存在'):
        execute_pdf_unlock(runtime, '/pdf-unlock encrypted.pdf unlocked.pdf', password_reader=lambda: pytest.fail('must reject before secret prompt'))
    with pytest.raises(ValueError, match='隐藏输入'):
        execute_pdf_unlock(runtime, '/pdf-unlock encrypted.pdf next.pdf secret')


def test_actual_scan_ocr_paging_without_network(runtime, monkeypatch):
    pytest.importorskip('rapidocr'); pytest.importorskip('pypdfium2')
    import requests
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen.canvas import Canvas
    from genesisai.core.extensions.tools.office.pdf_ocr.implementation import Implementation
    workspace, _, _, runtime = runtime
    bitmap = Image.new('RGB', (1500, 300), 'white')
    font_path = 'C:/Windows/Fonts/arial.ttf'
    font = ImageFont.truetype(font_path, 64) if __import__('pathlib').Path(font_path).exists() else ImageFont.load_default(size=64)
    ImageDraw.Draw(bitmap).text((60, 80), 'SCANNED PAGE ALPHA 7319', font=font, fill='black')
    image_path = workspace / 'scan.png'; bitmap.save(image_path); bitmap.close()
    path = workspace / 'scan.pdf'
    canvas = Canvas(str(path), pagesize=(750, 150))
    for _ in range(2): canvas.drawImage(str(image_path), 0, 0, width=750, height=150); canvas.showPage()
    canvas.save()
    monkeypatch.setattr(requests.sessions.Session, 'request', lambda *a, **k: pytest.fail('OCR must not access network'))
    first = Implementation().execute(runtime.context, {'path': str(path), 'limit': 1})
    assert 'ALPHA7319' in first['items'][0]['text'].replace(' ', '')
    assert first['next']['offset'] == 1 and not first['coverage']['end_reached']
    second = Implementation().execute(runtime.context, first['next'])
    assert second['items'][0]['number'] == 2 and second['coverage']['end_reached']
    assert second['network_used'] is False
    from genesisai.core.jobs import tool_operation
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .01)
    submitted = execute(runtime, 'pdf_ocr', {'path': str(path), 'offset': 1, 'limit': 1})
    assert submitted['ok'], submitted
    collected = execute(runtime, 'read_job', {'job_id': submitted['data']['job_id'], 'wait_seconds': 30}, 'collect_ocr')
    assert collected['ok'], collected
    payload = collected['data']['result']
    assert 'ALPHA7319' in payload['items'][0]['text'].replace(' ', '')
    assert payload['source_ref'] in runtime.store.record.sources


def test_actual_media_probe_frames_and_audio_segment(runtime):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): pytest.skip('local ffmpeg/ffprobe required')
    from PIL import Image
    workspace, output, _, runtime = runtime
    path = workspace / 'clip.mp4'
    media.run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=320x240:r=10',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '2', '-c:v', 'mpeg4', '-c:a', 'aac', str(path)])
    info = media.probe(runtime.context, {'path': str(path)})
    assert {s['codec_type'] for s in info['streams']} == {'audio', 'video'}
    result = media.frames(runtime.context, {'path': str(path), 'output_prefix': 'frame', 'count': 2, 'interval_seconds': .5})
    assert len(result['frames']) == 2 and result['next_seconds'] == 1
    with Image.open(result['frames'][1]['path']) as frame:
        r, g, b = frame.convert('RGB').getpixel((10, 10))
        assert r > 200 and g < 30 and b < 30
    audio = media.audio(runtime.context, {'path': str(path), 'output': 'audio.wav', 'start_seconds': .5, 'duration_seconds': 1})
    assert audio['sample_rate'] == 16000 and .9 < audio['duration_seconds'] < 1.1
    assert audio['transcription'] == 'not_performed'
    with pytest.raises(ToolError, match='已存在'):
        media.audio(runtime.context, {'path': str(path), 'output': 'audio.wav'})
    outside = workspace.parent / 'private.mp4'; outside.write_bytes(path.read_bytes())
    # workspace runtime in these fixtures uses host access; revoke it for this boundary assertion.
    runtime.access.host_access = False
    with pytest.raises(ToolError, match='授权范围'):
        media.probe(runtime.context, {'path': str(outside)})


def test_media_missing_engine_and_playlists_are_explicit(runtime, monkeypatch):
    workspace, _, _, runtime = runtime
    with monkeypatch.context() as scoped:
        scoped.setenv('PATH', '.')
        with pytest.raises(ToolError, match='本地 ffprobe'): media.program('ffprobe')
    if not shutil.which('ffprobe'): pytest.skip('local ffprobe required')
    playlist = workspace / 'bad.mp4'; playlist.write_text('#EXTM3U\nhttps://example.invalid/private.mp4', encoding='utf-8')
    with pytest.raises(ToolError, match='播放列表'):
        media.probe(runtime.context, {'path': str(playlist)})


def test_media_tool_job_collects_real_artifact_and_source(runtime, monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): pytest.skip('local ffmpeg/ffprobe required')
    from genesisai.core.jobs import tool_operation
    workspace, _, store, runtime = runtime
    path = workspace / 'audio.wav'
    media.run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=16000', '-t', '1', str(path)])
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', .01)
    submitted = execute(runtime, 'media_audio', {'path': str(path), 'output': 'job.wav', 'duration_seconds': .5})
    assert submitted['ok'], submitted
    job = submitted['data']['job_id']
    result = execute(runtime, 'read_job', {'job_id': job, 'wait_seconds': 30}, 'collect_media')
    assert result['ok'], result
    payload = result['data']['result']
    assert payload['format'] == 'wav' and payload['source_ref'] in store.record.sources
    assert payload['artifact_refs'][0] in store.record.artifacts
    assert any(item['id'] == payload['change_id'] for item in store.record.changes)
