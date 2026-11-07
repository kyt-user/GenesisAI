"""PTY bridge kept inside the job host's owned process group/job object."""
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from genesisai.core.jobs.input_channel import InputServer
from genesisai.core.jobs.job_input import require_current
from genesisai.core.jobs.storage import save
from genesisai.core.jobs.stream_output import StreamOutput
from genesisai.core.state.io_journal import IOJournal
from genesisai.shared.security import ToolError


class WindowsTerminal:
    def __init__(self, payload):
        from winpty import PTY
        from winpty.enums import Backend
        size = payload['info']['interaction']
        self.pty = PTY(size['columns'], size['rows'], backend=Backend.ConPTY)
        command = payload['invocation']
        options = {'cwd': payload['info']['cwd'],
                   'env': '\0'.join(key + '=' + value for key, value in payload['env'].items()) + '\0'}
        if len(command) > 1:
            options['cmdline'] = ' ' + subprocess.list2cmdline(command[1:])
        if not self.pty.spawn(command[0], **options):
            raise ToolError('terminal_start_failed', 'ConPTY 程序没有启动')
        self.pid = self.pty.pid

    def read(self):
        return self.pty.read(blocking=False).encode('utf-8')

    def write(self, text):
        written = self.pty.write(text)
        if written != len(text.encode('utf-8')):
            raise ToolError('unknown_execution', 'PTY 输入未确认完整发送；禁止重发')

    def resize(self, rows, columns):
        self.pty.set_size(columns, rows)

    def alive(self):
        return self.pty.isalive()

    def eof(self):
        return self.pty.iseof()

    def exit_code(self):
        return self.pty.get_exitstatus()

    def close(self):
        self.pty.cancel_io()
        # The outer owned Windows job owns this bridge and every spawned child.
        # No os.kill(pid, 0) or reattachment to an unverified PID is used.


class PosixTerminal:
    def __init__(self, payload):
        import fcntl
        import termios
        self.master, slave = os.openpty()
        self.ended = False
        try:
            # owned_process launched this bridge as the session leader. Keep
            # the program in this process group so cancellation contains both.
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGQUIT, signal.SIG_IGN)
            def child_signals():
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                signal.signal(signal.SIGQUIT, signal.SIG_DFL)
            self.process = subprocess.Popen(payload['invocation'], cwd=payload['info']['cwd'],
                env=payload['env'], stdin=slave, stdout=slave, stderr=slave, preexec_fn=child_signals)
            self.pid = self.process.pid
            os.set_blocking(self.master, False)
            size = payload['info']['interaction']
            self.resize(size['rows'], size['columns'])
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)

    def read(self):
        try:
            value = os.read(self.master, 65536)
            if not value:
                self.ended = True
            return value
        except BlockingIOError:
            return b''
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            self.ended = True
            return b''

    def write(self, text):
        import select
        remaining = memoryview(text.encode('utf-8'))
        while remaining:
            try:
                written = os.write(self.master, remaining)
                if written <= 0:
                    raise ToolError('unknown_execution', 'PTY 输入未确认完整发送')
                remaining = remaining[written:]
            except BlockingIOError:
                if not self.alive():
                    raise ToolError('unknown_execution', '输入发送期间程序已退出')
                select.select([], [self.master], [], .1)

    def resize(self, rows, columns):
        import fcntl
        import struct
        import termios
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))

    def alive(self):
        return self.process.poll() is None

    def eof(self):
        return self.ended

    def exit_code(self):
        return self.process.wait()

    def close(self):
        os.close(self.master)


def main():
    payload = json.loads(sys.stdin.buffer.readline())
    root, info = Path(payload['root']), payload['info']
    redactor = object.__new__(IOJournal)
    redactor._secrets = frozenset(value for value in payload['secrets'] if isinstance(value, str) and value)
    output = StreamOutput(redactor.clean)
    terminal = server = None
    started = False
    try:
        terminal = (WindowsTerminal if os.name == 'nt' else PosixTerminal)(payload)
        started = True
        def validate(request):
            if (root / 'stop.json').exists():
                raise ToolError('cancelled', '作业正在停止，输入未发送')
            require_current(info, request['authorization_id'])
        def close_input():
            raise ToolError('validation_error', 'PTY 输入 EOF 必须显式提供目标程序所需控制字符')
        server = InputServer(root, payload['control'], payload['limits']['input_max_bytes'],
            write=terminal.write, close_input=close_input, resize=terminal.resize,
            validate=validate, redactor=redactor).start()
        save(root / 'terminal.json', {'status': 'running', 'target_pid': terminal.pid,
                                     'backend': 'conpty' if os.name == 'nt' else 'posix-pty'})
        save(root / 'control.ready', {'ready': True})
        while True:
            chunk = terminal.read()
            visible = output.feed(chunk)
            if visible:
                sys.stdout.buffer.write(visible.encode('utf-8'))
                sys.stdout.buffer.flush()
            if server.fault:
                raise ToolError('unknown_execution', server.fault)
            if not terminal.alive() and terminal.eof():
                break
            time.sleep(.01)
        final = output.feed(b'', final=True)
        if final:
            sys.stdout.buffer.write(final.encode('utf-8'))
            sys.stdout.buffer.flush()
        code = terminal.exit_code()
        if type(code) is not int:
            raise ToolError('unknown_execution', '终端未提供可验证的退出状态')
        save(root / 'terminal.json', {'status': 'exited', 'exit_code': code, 'target_pid': terminal.pid})
        return 0 if code == 0 else 1
    except BaseException as exc:
        save(root / 'terminal.json', {'status': 'unknown' if started else 'failed',
             'code': 'unknown_execution' if started else getattr(exc, 'error_type', 'terminal_start_failed'),
             'error': redactor.clean(str(exc))})
        return 1
    finally:
        if server:
            server.close()
        if terminal:
            terminal.close()


if __name__ == '__main__':
    raise SystemExit(main())
