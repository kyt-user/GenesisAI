"""Local, fixed-argv media decoding; no remote URLs or playlist demuxers."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError
from genesisai.shared.process_tree import safe_environment

FORMATS = 'mov,matroska,webm,wav,mp3,flac,ogg,avi'


def program(name):
    # Only explicit absolute PATH entries; Windows shutil.which also searches
    # the current working directory, which could execute a project-supplied file.
    filename = name + ('.exe' if os.name == 'nt' else '')
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        directory = Path(entry.strip('"'))
        if directory.is_absolute():
            candidate = directory / filename
            if candidate.is_file() and (os.name == 'nt' or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    raise ToolError('missing_dependency', f'需要本地 {name} 程序并加入绝对 PATH；不会自动安装或调用云服务')


def run(name, args):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run([program(name), *args], stdin=subprocess.DEVNULL,
                            capture_output=True, env=safe_environment(), **options)
    if result.returncode:
        raise ToolError('media_decode_error', f'{name} 无法解码此本地文件；支持自包含 MP4/MOV、MKV/WebM、AVI、WAV、MP3、FLAC、OGG，不读取播放列表')
    return result.stdout


def input_options(path):
    return ['-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMATS, '-i', str(path)]


def source(context, name, expected=None):
    path = context.access.read(name)
    if not path.is_file(): raise ToolError('invalid_path', '媒体源必须是本地文件')
    digest = sha(path)
    if expected is not None and expected != digest:
        raise ToolError('media_changed', '媒体源已变化，请重新开始')
    ref = context.store.source(kind='file', path=str(path), sha256=digest, title=path.name)
    return path, digest, ref


def probe(context, args):
    path, digest, ref = source(context, args['path'], args.get('expected_sha256'))
    raw = run('ffprobe', ['-v', 'error', *input_options(path), '-show_entries',
        'format=format_name,duration,size:stream=index,codec_type,codec_name,width,height,sample_rate,channels,duration', '-of', 'json'])
    if sha(path) != digest: raise ToolError('media_changed', '读取期间媒体源已变化')
    return json.loads(raw) | {'source_ref': ref, 'sha256': digest, 'network_used': False,
                             'transcription': 'not_performed', 'visual_content': 'not_analyzed'}


def publish(context, name, suffix, writer, verify):
    from genesisai.shared.changes import record_change
    target = context.access.write(name)
    if target.suffix.lower() != suffix:
        raise ToolError('validation_error', '输出扩展名必须为 ' + suffix)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, suffix=suffix, prefix='.genesis-media-', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        writer(temporary)
        metadata = verify(temporary)
        if target.exists(): raise ToolError('target_exists', '目标已被占用，未覆盖')
        if os.name == 'nt': os.rename(temporary, target)
        else: os.link(temporary, target)
        digest = sha(target)
        change = record_change(context.store, operation='media_write', path=target, before_sha=None, after_sha=digest, backup=None)
        ref = context.store.artifact(target)
        return {'path': str(target), 'sha256': digest, 'artifact_refs': [ref], 'change_id': change['id'], **metadata}
    finally:
        temporary.unlink(missing_ok=True)


def frames(context, args):
    from PIL import Image
    path, digest, ref = source(context, args['path'], args.get('expected_sha256'))
    start, interval, count = args.get('start_seconds', 0), args.get('interval_seconds', 1), args.get('count', 1)
    def verify(path):
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return {'format': 'png', 'width': image.width, 'height': image.height}
    results = []
    for i in range(count):
        timestamp = start + i * interval
        def writer(target):
            run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-ss', str(timestamp), *input_options(path),
                '-map', '0:v:0', '-frames:v', '1', '-vf', 'scale=1920:1080:force_original_aspect_ratio=decrease', str(target)])
            if sha(path) != digest: raise ToolError('media_changed', '抽帧期间媒体源已变化')
        item = publish(context, f"{args['output_prefix']}-{i:04d}.png", '.png', writer, verify)
        results.append(item | {'seconds': timestamp})
    return {'frames': results, 'source_ref': ref, 'sha256': digest, 'next_seconds': start + count * interval,
            'network_used': False, 'visual_content': 'not_analyzed', 'hint': '通过 read_image 查看生成帧；抽样帧不代表已看完整视频'}


def audio(context, args):
    import wave
    path, digest, ref = source(context, args['path'], args.get('expected_sha256'))
    start, duration = args.get('start_seconds', 0), args.get('duration_seconds', 30)
    def writer(target):
        run('ffmpeg', ['-nostdin', '-v', 'error', '-y', '-ss', str(start), *input_options(path),
            '-map', '0:a:0', '-t', str(duration), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(target)])
        if sha(path) != digest: raise ToolError('media_changed', '提取期间媒体源已变化')
    def verify(path):
        with wave.open(str(path), 'rb') as handle:
            if not handle.getnframes(): raise ToolError('empty_media', '指定范围没有音频')
            return {'format': 'wav', 'channels': handle.getnchannels(), 'sample_rate': handle.getframerate(),
                    'duration_seconds': handle.getnframes() / handle.getframerate()}
    result = publish(context, args['output'], '.wav', writer, verify)
    return result | {'source_ref': ref, 'source_sha256': digest, 'start_seconds': start,
                     'next_seconds': start + result['duration_seconds'], 'network_used': False,
                     'transcription': 'not_performed'}
