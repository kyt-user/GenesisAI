"""Private stdio entry point for trusted tool workers."""

import os
import sys
from threading import RLock
from types import SimpleNamespace

from genesisai.core.state.io_journal import IOJournal
from genesisai.core.tools.base import HostBoundTool, ToolContext
from genesisai.core.tools.observations import ObservationRegistry
from genesisai.core.tools.worker import Ref, Remote, error_info, receive, send
from genesisai.shared.process_tree import group_listener
from genesisai.shared.run_control import RunControl, current_run


def main():
    read = os.fdopen(os.dup(sys.stdin.fileno()), "rb")
    with open(os.devnull, "rb") as empty:
        os.dup2(empty.fileno(), sys.stdin.fileno())
    write = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    # Reserve the protocol pipe. A tool or its child can print freely.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    channel = (read, write, RLock())

    def report_group(pid, started):
        with channel[2]:
            send(write, ("process_group", (pid, started)))

    group_listener.set(report_group)
    implementation = context = observer = None
    while True:
        try:
            method, args = receive(read)
        except EOFError:
            return
        except Exception as exc:  # noqa: BLE001 - report extension and decoding failures to the host
            send(write, ("error", error_info(exc)))
            return
        try:
            if method == "init":
                (
                    implementation,
                    observer,
                    access,
                    store,
                    runtime,
                    network,
                    providers,
                    cancelled,
                    secrets,
                ) = args
                if isinstance(implementation, HostBoundTool):
                    implementation.host_service = Remote(
                        channel, implementation.host_service
                    )
                runtime = Remote(channel, runtime)
                # Redact command output locally so streamed lines do not require
                # a host call per line. Journal writes still use the host logger.
                redactor = object.__new__(IOJournal)
                redactor._secrets = secrets
                local_logger = SimpleNamespace(
                    journal=redactor,
                    record=runtime.logger.record,
                    error=runtime.logger.error,
                )
                object.__setattr__(runtime, "logger", local_logger)

                def service(value):
                    return Remote(channel, value) if isinstance(value, Ref) else value

                context = ToolContext(
                    Remote(channel, store),
                    access,
                    service(network),
                    [service(p) for p in providers],
                    runtime,
                )
                if cancelled:

                    def emit(kind, data):
                        with channel[2]:
                            send(write, ("progress", (kind, data)))

                    control = RunControl(emit)
                    control.cancelled = Remote(channel, cancelled)
                    current_run.set(control)
                result = None
            elif method == "prepare":
                result = implementation.prepare(context, args)
            else:
                payload = implementation.execute(context, args)
                adapter = observer or getattr(implementation, "observe", None)
                result = payload, ObservationRegistry.collect(adapter, args, payload)
            send(write, ("ok", result))
        except Exception as exc:  # noqa: BLE001 - report extension and decoding failures to the host
            send(write, ("error", error_info(exc)))


if __name__ == "__main__":
    main()
