"""Keep each command and its descendants inside one owned process group."""

from contextlib import contextmanager
from contextvars import ContextVar
import os
import re
import signal
import subprocess


# Supervised workers report nested POSIX groups, since setsid detaches those
# commands from the outer tool group. Windows jobs already include nested jobs.
group_listener = ContextVar("process_group_listener", default=None)


_BASE_ENVIRONMENT = frozenset(name.casefold() for name in (
    'PATH', 'PATHEXT', 'SystemRoot', 'WINDIR', 'SystemDrive', 'COMSPEC',
    'HOME', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA',
    'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ',
    'TERM', 'COLORTERM', 'NO_COLOR', 'COLUMNS', 'LINES', 'OS',
    'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS', 'ProgramFiles', 'ProgramFiles(x86)',
    'CommonProgramFiles', 'CommonProgramFiles(x86)', 'ProgramData',
    # Explicit non-secret configuration used by trusted document/media workers.
    'GENESISAI_DOCUMENT_MAX_EXPANDED_BYTES', 'GENESISAI_DOCUMENT_MAX_ENTRIES',
    'GENESISAI_DOCUMENT_MAX_COMPRESSION_RATIO', 'GENESISAI_OCR_MAX_PAGE_PIXELS',
    'GENESISAI_WHISPER_MODEL',
))
_ENVIRONMENT_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_PRIVATE_ENVIRONMENT = frozenset({'genesisai_result_path'})


def validate_environment_mapping(env_from):
    from genesisai.shared.security import ToolError
    if (not isinstance(env_from, dict) or any(not isinstance(key, str) or not isinstance(value, str)
            or not _ENVIRONMENT_NAME.fullmatch(key) or not _ENVIRONMENT_NAME.fullmatch(value)
            or key.casefold() in _PRIVATE_ENVIRONMENT
            for key, value in env_from.items())
            or (os.name == 'nt' and len({name.casefold() for name in env_from}) != len(env_from))):
        raise ToolError('validation_error', 'env_from 必须为互不冲突的子进程变量名到宿主变量名映射')
    return dict(env_from)


def mapped_environment(env_from, *, source=None):
    from genesisai.shared.security import ToolError
    source = os.environ if source is None else source
    mapping = validate_environment_mapping(env_from)
    lookup = {key.casefold(): value for key, value in source.items()} if os.name == 'nt' else source
    values = {}
    for target, origin in mapping.items():
        key = origin.casefold() if os.name == 'nt' else origin
        if key not in lookup:
            raise ToolError('missing_environment', '宿主环境变量未配置：' + origin)
        value = lookup[key]
        if not isinstance(value, str) or '\0' in value:
            raise ToolError('validation_error', '环境变量值无效：' + origin)
        values[target] = value
    return values


def environment_preview(env_from):
    import hashlib
    mapping = validate_environment_mapping(env_from)
    values = mapped_environment(mapping)
    return {'env_from': mapping, 'environment_digests': {
        target: hashlib.sha256(value.encode('utf-8')).hexdigest() for target, value in values.items()}}


def safe_environment(env_from=None, *, source=None):
    """Minimal process environment plus explicitly authorized name mappings."""
    source = os.environ if source is None else source
    values = {key: value for key, value in source.items() if key.casefold() in _BASE_ENVIRONMENT}
    values.update(mapped_environment({} if env_from is None else env_from, source=source))
    return values


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_on_job_close(kernel, job):
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
            ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
            ('max_working_set', ctypes.c_size_t), ('process_limit', wintypes.DWORD),
            ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('basic', BasicLimits), ('io_counters', ctypes.c_uint64 * 6),
                    ('memory_limits_and_peaks', ctypes.c_size_t * 4)]

    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def owned_process(command, **options):
    if os.name != 'nt':
        with subprocess.Popen(command, start_new_session=True, **options) as process:
            listener = group_listener.get()
            try:
                if listener:
                    listener(process.pid, True)
                yield process
            finally:
                kill_group(process.pid)
                if listener:
                    listener(process.pid, False)
        return

    import ctypes
    from ctypes.wintypes import HANDLE

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.restype = HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        _kill_on_job_close(kernel, job)
        # Attach before executing any child code, so even a fast-spawning
        # launcher cannot leave a grandchild outside our job.
        with subprocess.Popen(command, creationflags=0x4, **options) as process:
            handle = HANDLE(int(process._handle))
            try:
                if not kernel.AssignProcessToJobObject(HANDLE(job), handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                status = ctypes.WinDLL('ntdll').NtResumeProcess(handle)
                if status != 0:
                    raise OSError(f'NtResumeProcess failed: {status:#x}')
                yield process
            finally:
                try:
                    if not kernel.TerminateJobObject(HANDLE(job), 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
    finally:
        kernel.CloseHandle(HANDLE(job))
