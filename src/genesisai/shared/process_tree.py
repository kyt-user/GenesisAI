"""Keep each command and its descendants inside one owned process group."""

from contextlib import contextmanager
from contextvars import ContextVar
import os
import signal
import subprocess


# Supervised workers report nested POSIX groups, since setsid detaches those
# commands from the outer tool group. Windows jobs already include nested jobs.
group_listener = ContextVar("process_group_listener", default=None)


def safe_environment():
    blocked = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL')
    return {key: value for key, value in os.environ.items() if not any(word in key.upper() for word in blocked)}


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
