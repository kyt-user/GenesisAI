"""Cancellation and progress for the currently executing interactive turn."""

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Lock


current_run = ContextVar('current_run', default=None)


class RunControl:
    def __init__(self, emit):
        self.emit = emit
        self.cancelled = Event()
        self._lock = Lock()
        self._interrupt = None

    def cancel(self):
        with self._lock:
            self.cancelled.set()
            if self._interrupt:
                self._interrupt()

    @contextmanager
    def interruptible(self, interrupt):
        with self._lock:
            self._interrupt = interrupt
            if self.cancelled.is_set():
                interrupt()
        try:
            yield
        finally:
            with self._lock:
                self._interrupt = None

    def execute(self, action):
        token = current_run.set(self)
        try:
            return action()
        finally:
            current_run.reset(token)


def cancellation_requested():
    control = current_run.get()
    return control is not None and control.cancelled.is_set()
