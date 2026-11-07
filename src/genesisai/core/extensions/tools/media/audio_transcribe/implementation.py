"""Explicit local Whisper model, bounded audio chunks, resumable text segments."""
import os
from pathlib import Path

from genesisai.core.extensions.tools.media.helpers import input_options, run, source
from genesisai.core.extensions.tools.office.readers import page
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError


def model_directory():
    value = os.environ.get('GENESISAI_WHISPER_MODEL', '')
    path = Path(value)
    if not value or not path.is_absolute() or not all((path / item).is_file() for item in
            ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')):
        raise ToolError('missing_model', '设置 GENESISAI_WHISPER_MODEL 为完整本地 faster-whisper 模型绝对目录；本工具不下载模型')
    return path.resolve()


class Implementation:
    def prepare(self, context, args): return None

    def execute(self, context, args):
        import numpy as np
        from faster_whisper import WhisperModel
        model_path = model_directory()
        path, digest, ref = source(context, args['path'], args.get('expected_sha256'))
        start, duration = args.get('start_seconds', 0), args.get('duration_seconds', 60)
        raw = run('ffmpeg', ['-nostdin', '-v', 'error', '-ss', str(start), *input_options(path),
            '-map', '0:a:0', '-t', str(duration), '-vn', '-ac', '1', '-ar', '16000',
            '-f', 'f32le', '-acodec', 'pcm_f32le', 'pipe:1'])
        samples = np.frombuffer(raw, dtype='<f4')
        actual = len(samples) / 16000
        if sha(path) != digest: raise ToolError('media_changed', '解码期间媒体源已变化')
        if not len(samples):
            return {'items': [], 'next': None, 'source_ref': ref, 'sha256': digest,
                    'coverage': {'start_seconds': start, 'end_seconds': start, 'end_reached': True},
                    'network_used': False}
        model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=2,
                             local_files_only=True)
        segments, info = model.transcribe(samples, language=args.get('language'), beam_size=5, temperature=0.0,
                                          vad_filter=True, condition_on_previous_text=False)
        units = ({'kind': 'speech_segment', 'text': item.text, 'start_seconds': start + item.start,
                  'end_seconds': start + item.end, 'avg_logprob': item.avg_logprob,
                  'no_speech_probability': item.no_speech_prob} for item in segments)
        result = page(units, offset=args.get('segment_offset', 0), text_offset=args.get('text_offset', 0),
                      max_chars=args.get('max_chars', 16000), limit=1000)
        if result['next']:
            continuation = result['next']
            result['next'] = {**args, 'segment_offset': continuation['offset'],
                              'text_offset': continuation['text_offset'], 'expected_sha256': digest}
        elif actual >= duration - .01:
            result['next'] = {**args, 'start_seconds': start + actual, 'segment_offset': 0,
                              'text_offset': 0, 'expected_sha256': digest}
        if sha(path) != digest: raise ToolError('media_changed', '转写期间媒体源已变化')
        result['coverage'].update(start_seconds=start, end_seconds=start + actual,
                                  end_reached=result['next'] is None, recognition_is_uncertain=True)
        result.update(source_ref=ref, sha256=digest, engine='faster_whisper_cpu_int8',
                      model_sha256=sha(model_path / 'model.bin'), language=info.language,
                      language_probability=info.language_probability, network_used=False,
                      limitations=['机器转写需听原音核对；无说话人分离；分块边界可能拆分词句'])
        return result
