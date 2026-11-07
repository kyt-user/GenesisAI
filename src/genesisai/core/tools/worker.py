"""Execute trusted Python tool code in a disposable, owned process.

Only the host owns the session, registry and live services. The tool can use its
normal context, while the supervisor can stop local work even in a blocking call.
"""

from __future__ import annotations

import builtins
import operator
import pickle
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from contextvars import copy_context
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

import cloudpickle

from genesisai.core.tools.base import HostBoundTool
from genesisai.shared.process_tree import kill_group, owned_process
from genesisai.shared.run_control import current_run
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline


@dataclass
class Ref:
    index: int


def send(stream, value):
    data = cloudpickle.dumps(value)
    stream.write(struct.pack("!Q", len(data)))
    stream.write(data)
    stream.flush()


def receive(stream):
    head = stream.read(8)
    if len(head) != 8:
        raise EOFError("Tool worker closed its channel")
    return pickle.loads(stream.read(struct.unpack("!Q", head)[0]))


# Context objects stay on the host. Method results are values, while attributes
# such as store.record.changes and store.record.artifacts[id] retain identity.
class HostObjects:
    def __init__(self):
        self.objects = []
        self.external_io = set()

    def ref(self, obj, *, external_io=False):
        if external_io:
            self.external_io.add(len(self.objects))
        self.objects.append(obj)
        return Ref(len(self.objects) - 1)

    def handle(self, operation, index, args, kwargs):
        obj = self.objects[index]
        if operation == "call":
            if index in self.external_io:
                return self._read_external(obj, args, kwargs)
            value = obj(*args, **kwargs)
            if type(value).__name__ in {"dict_keys", "dict_items", "dict_values"}:
                return list(value)
            return value
        if operation == "value":
            return obj
        if operation == "setattr":
            setattr(obj, *args)
            return None
        if operation == "set":
            obj[args[0]] = args[1]
            return None
        value = (getattr if operation == "attr" else operator.getitem)(obj, *args)
        if (
            isinstance(value, (str, int, float, bool, Path, bytes, tuple))
            or value is None
        ):
            return value
        return self.ref(value, external_io=index in self.external_io)

    @staticmethod
    def _read_external(method, args, kwargs):
        # A client supplied by the host can block in DNS or a native transport.
        # It has no session handle. Discard late responses after killing the tool.
        response = Queue(maxsize=1)

        def read():
            try:
                response.put((True, method(*args, **kwargs)))
            except Exception as exc:  # noqa: BLE001 - propagate failures across the worker boundary
                response.put((False, exc))

        Thread(target=copy_context().run, args=(read,), daemon=True).start()
        try:
            deadline = tool_deadline.get()
            ok, value = response.get(
                timeout=max(0, deadline - time.monotonic()) if deadline is not None else None
            )
        except Empty:
            raise ToolError("tool_timeout", "网络服务超过工具时限") from None
        if not ok:
            raise value
        return value


def _identity(value):
    return value


class Remote:
    """Worker-side reference to an application service or a mutable record."""

    def __init__(self, channel, ref):
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "index", ref.index)

    def request(self, op, *args, **kwargs):
        read, write, lock = self.channel
        with lock:
            send(write, ("host", (op, self.index, args, kwargs)))
            kind, value = receive(read)
        if kind == "error":
            raise_error(value)
        return Remote(self.channel, value) if isinstance(value, Ref) else value

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self.request("attr", name)

    def __setattr__(self, name, value):
        self.request("setattr", name, value)

    def __getitem__(self, key):
        return self.request("item", key)

    def __setitem__(self, key, value):
        self.request("set", key, value)

    def __call__(self, *args, **kwargs):
        return self.request("call", *args, **kwargs)

    def __iter__(self):
        return iter(self.request("value"))

    def __len__(self):
        return len(self.request("value"))

    def __reduce__(self):
        return _identity, (self.request("value"),)


def error_info(exc):
    # Exception constructors (notably ToolError) need not accept exc.args.
    return (
        getattr(exc, "error_type", None),
        type(exc).__name__,
        str(exc),
        getattr(exc, "retryable", False),
        getattr(exc, "reason", None),
        exc.args if type(exc).__module__ == "builtins" else (),
    )


def raise_error(info):
    code, name, message, retryable, reason, args = info
    if code:
        error = ToolError(code, message, retryable)
        error.reason = reason
        raise error
    error_class = getattr(builtins, name, None)
    if isinstance(error_class, type) and issubclass(error_class, Exception):
        raise error_class(*args)
    raise RuntimeError(message)


def _reader(pipe, inbox, groups):
    try:
        while True:
            kind, value = receive(pipe)
            if kind == "process_group":
                pid, started = value
                if started:
                    groups.add(pid)
                else:
                    groups.discard(pid)
            else:
                inbox.put((kind, value))
    except Exception as exc:  # noqa: BLE001 - propagate failures across the worker boundary
        inbox.put(("closed", str(exc)))


class ToolWorker:
    def __init__(self, process, host, deadline):
        self.process = process
        self.host = host
        self.deadline = deadline
        self.inbox = Queue()
        self.groups = set()
        self.reader = Thread(
            target=_reader, args=(process.stdout, self.inbox, self.groups), daemon=True
        )
        self.reader.start()

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ToolError("tool_timeout", "工具执行超时，已终止本地执行进程")

    def _send(self, packet):
        self.check_deadline()
        try:
            send(self.process.stdin, packet)
        except (BrokenPipeError, OSError) as exc:
            raise ToolError("worker_lost", "工具执行进程连接已关闭") from exc

    def request(self, method, args):
        self._send((method, args))
        control = current_run.get()
        cancelled_at = None
        while True:
            if control and control.cancelled.is_set():
                cancelled_at = cancelled_at or time.monotonic()
                if time.monotonic() - cancelled_at >= 0.5:
                    raise ToolError(
                        "worker_cancelled", "已停止本地工具进程，可能已产生部分修改"
                    )
            remaining = self.deadline - time.monotonic() if self.deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise ToolError("tool_timeout", "工具执行超时，已终止本地执行进程")
            try:
                kind, value = self.inbox.get(
                    timeout=min(remaining, 0.1) if control and remaining is not None else .1 if control else remaining
                )
            except Empty:
                continue
            self.check_deadline()
            if kind == "host":
                try:
                    token = tool_deadline.set(self.deadline)
                    try:
                        response = ("ok", self.host.handle(*value))
                    finally:
                        tool_deadline.reset(token)
                except Exception as exc:  # noqa: BLE001 - propagate failures across the worker boundary
                    response = ("error", error_info(exc))
                self._send(response)
            elif kind == "progress":
                if control:
                    control.emit(*value)
            elif kind == "error":
                raise_error(value)
            elif kind == "ok":
                return value
            else:
                raise ToolError("worker_lost", "工具执行进程意外退出")


@contextmanager
def tool_worker(context, implementation, observer, deadline):
    from genesisai.core.tools.permissions import accepted_tool
    host = HostObjects()
    control = current_run.get()
    worker = None
    try:
        with owned_process(
            [sys.executable, "-I", "-m", "genesisai.core.tools.worker_main"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            worker = ToolWorker(process, host, deadline)
            # The context is copied only for local access checks. Session changes
            # and service calls remain on their original application instances.
            if isinstance(implementation, HostBoundTool):
                implementation = copy(implementation)
                implementation.host_service = host.ref(implementation.host_service)
            network = (
                host.ref(context.network, external_io=True)
                if context.hosted_network
                else context.network
            )
            providers = (
                [host.ref(p, external_io=True) for p in context.providers]
                if context.hosted_providers
                else context.providers
            )
            worker.request(
                "init",
                (
                    implementation,
                    observer,
                    context.access,
                    host.ref(context.store),
                    host.ref(context.runtime),
                    network,
                    providers,
                    host.ref(control.cancelled) if control else None,
                    getattr(context.runtime.logger.journal, "_secrets", ()),
                    context.call_id,
                    context.authorization,
                    accepted_tool.get(),
                ),
            )
            yield worker
    finally:
        if worker:
            worker.reader.join()
            for pid in worker.groups:
                kill_group(pid)
