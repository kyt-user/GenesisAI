"""Cancellable Git enumeration with explicit, inspectable cache freshness."""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import subprocess
import time

from genesisai.core.paths import ApplicationPaths
from genesisai.shared.process_tree import owned_process, safe_environment
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError


DEFAULT_EXCLUDES = ('.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
                    'dist', 'build', '.genesis', '.genesisai')


@dataclass(frozen=True)
class IndexConfig:
    git_timeout_seconds: float | None = None
    cache_seconds: float = 15
    progress_batch: int = 128
    exclude_directories: tuple[str, ...] = DEFAULT_EXCLUDES

    @classmethod
    def load(cls, session):
        path = ApplicationPaths(session.root).project(session.record.project_id) / 'index.json'
        supplied = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(supplied, dict) or supplied.keys() - cls.__dataclass_fields__.keys():
            raise ValueError('index.json 包含未知设置或不是对象')
        values = cls(**supplied)
        for name in ('git_timeout_seconds', 'cache_seconds'):
            value = getattr(values, name)
            if value is None and name == 'git_timeout_seconds':
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (name == 'git_timeout_seconds' and value == 0):
                raise ValueError(f'index.{name} 必须为有效秒数')
        if type(values.progress_batch) is not int or values.progress_batch < 1:
            raise ValueError('index.progress_batch 必须为正整数')
        if (not isinstance(values.exclude_directories, (list, tuple))
                or any(not isinstance(item, str) or not item or '/' in item or '\\' in item
                       or item in {'.', '..'} for item in values.exclude_directories)):
            raise ValueError('index.exclude_directories 必须为目录名称数组')
        return values


def check_cancelled():
    if cancellation_requested():
        raise ToolError('cancelled', '项目索引扫描已取消；已完成的派生记录保留，未完成扫描不会删除剩余索引')


def stamp(path):
    try:
        info = path.stat()
        return [info.st_mtime_ns, info.st_ctime_ns, info.st_size]
    except FileNotFoundError:
        return None


class IndexScanner:
    def __init__(self, session):
        self.session = session
        self.cache = None
        self.status = {'phase': 'idle', 'cached': False}

    def checkpoint(self, progress=None, **details):
        self.status.update(details)
        if progress:
            progress(dict(self.status))
        check_cancelled()

    def git(self, args, config, *, data=None, accepted=(0,), progress=None):
        self.checkpoint(progress, phase='git', command=args[0])
        options = {}
        if os.name == 'nt':
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options['startupinfo'] = startup
        started = time.monotonic()
        with owned_process(['git', '-C', str(self.session.workspace), *args], env=safe_environment(),
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options) as process:
            pending = data
            while True:
                self.checkpoint(progress, elapsed_seconds=time.monotonic() - started)
                if config.git_timeout_seconds is not None and time.monotonic() - started >= config.git_timeout_seconds:
                    raise ToolError('index_timeout', f'Git {args[0]} 超过配置的 {config.git_timeout_seconds} 秒，扫描未完成')
                try:
                    stdout, stderr = process.communicate(input=pending, timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    pending = None
            if process.returncode not in accepted:
                raise ValueError(f'Git {args[0]} 失败（{process.returncode}）：' + stderr.decode('utf-8', errors='replace'))
        return process.returncode, stdout

    def marker(self):
        root = self.session.workspace
        return next((parent / '.git' for parent in (root, *root.parents) if (parent / '.git').exists()), None)

    def _watches(self, marker, paths, config, progress):
        root = self.session.workspace
        watched = {root, marker}
        gitdir = marker
        if marker.is_file():
            value = marker.read_text(encoding='utf-8').strip()
            if not value.startswith('gitdir: '):
                raise ValueError('Git工作树标记格式无效')
            gitdir = (marker.parent / value[8:]).resolve()
        common = gitdir
        if (gitdir / 'commondir').is_file():
            common = (gitdir / (gitdir / 'commondir').read_text(encoding='utf-8').strip()).resolve()
        watched.update({gitdir / 'index', gitdir / 'HEAD', gitdir / 'config', common / 'config', common / 'info/exclude'})
        _, exclude = self.git(['config', '--path', '--get', 'core.excludesFile'], config, accepted=(0, 1), progress=progress)
        if exclude.strip():
            watched.add((root / Path(exclude.decode('utf-8').strip()).expanduser()).resolve())
        for name in paths:
            parent = (root / name).parent
            while parent.is_relative_to(root):
                watched.update({parent, parent / '.gitignore'})
                if parent == root:
                    break
                parent = parent.parent
        parent = root
        while parent.is_relative_to(marker.parent):
            watched.add(parent / '.gitignore')
            if parent == marker.parent:
                break
            parent = parent.parent
        return {str(path): stamp(path) for path in watched}

    def paths(self, config, *, force=False, progress=None):
        self.status.update(cache_ttl_seconds=config.cache_seconds, git_timeout_seconds=config.git_timeout_seconds)
        marker = self.marker()
        if marker is None:
            self.cache = None
            self.checkpoint(progress, cached=False, cache_age_seconds=0)
            return None
        now = time.monotonic()
        if (not force and self.cache and self.cache['marker'] == str(marker)
                and now - self.cache['created'] < config.cache_seconds
                and all(stamp(Path(path)) == value for path, value in self.cache['watches'].items())):
            self.checkpoint(progress, phase='enumerated', cached=True, cache_age_seconds=now - self.cache['created'])
            return self.cache['paths']
        ignored, _ = self.git(['check-ignore', '-q', '.'], config, accepted=(0, 1), progress=progress)
        if ignored == 0:
            self.cache = None
            return None
        _, raw = self.git(['ls-files', '--cached', '--others', '--exclude-standard', '-z'], config, progress=progress)
        paths = sorted({name for name in raw.decode('utf-8').split('\0') if name})
        watches = self._watches(marker, paths, config, progress)
        self.cache = {'marker': str(marker), 'created': time.monotonic(), 'paths': paths, 'watches': watches}
        self.checkpoint(progress, phase='enumerated', cached=False, cache_age_seconds=0, candidates=len(paths))
        return paths

    def ignored(self, paths, config, *, progress=None):
        if not paths or self.marker() is None:
            return set()
        ignored_root, _ = self.git(['check-ignore', '-q', '.'], config, accepted=(0, 1), progress=progress)
        if ignored_root == 0:
            # A separately selected workspace inside an ignored test/build
            # folder is indexed as its own tree, not hidden by the parent repo.
            return set()
        _, raw = self.git(['check-ignore', '-z', '--stdin'], config, data=('\0'.join(paths) + '\0').encode('utf-8'),
                          accepted=(0, 1), progress=progress)
        return set(raw.decode('utf-8').split('\0'))
