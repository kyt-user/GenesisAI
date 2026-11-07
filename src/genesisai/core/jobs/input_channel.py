"""Authenticated local input channel; receipts never contain submitted text."""
from contextvars import copy_context
import hashlib
import hmac
import json
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener
import os
from pathlib import Path
import re
import queue
import secrets
import threading
import time

from genesisai.core.jobs.storage import load, save
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline


def interaction(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() - {'mode', 'rows', 'columns'} or value.get('mode') not in {'pipe', 'pty'}:
        raise ToolError('validation_error', 'interaction 需要明确的 pipe/pty 模式')
    result = {'mode': value['mode']}
    if value['mode'] == 'pipe' and any(name in value for name in ('rows', 'columns')):
        raise ToolError('validation_error', '只有 PTY 模式接受终端尺寸')
    if value['mode'] == 'pty':
        if os.name == 'nt':
            from importlib.util import find_spec
            if find_spec('winpty') is None:
                raise ToolError('missing_dependency', 'Windows PTY 需要安装 genesisai[terminal]')
        for name, default in (('rows', 24), ('columns', 80)):
            number = value.get(name, default)
            if type(number) is not int or not 1 <= number <= 32767:
                raise ToolError('validation_error', '终端尺寸须为 1..32767 的整数')
            result[name] = number
    return result


def create_control(root, session_id, job_id):
    root = Path(root)
    family = 'AF_PIPE' if os.name == 'nt' else 'AF_UNIX'
    address = r'\\.\pipe\genesisai-' + secrets.token_hex(16) if os.name == 'nt' else str(root / 'control.sock')
    value = {'family': family, 'address': address, 'key': secrets.token_hex(32),
             'session_id': session_id, 'job_id': job_id}
    save(root / 'control.json', value)
    return value


def identity(call_id):
    if not isinstance(call_id, str) or not call_id:
        raise ToolError('validation_error', '作业输入必须绑定调用编号')
    return 'input_' + hashlib.sha256(call_id.encode('utf-8')).hexdigest()[:32]


def receipt_path(root, input_id):
    if not isinstance(input_id, str) or not re.fullmatch(r'input_[a-f0-9]{32}', input_id):
        raise ToolError('validation_error', '输入回执编号无效')
    return Path(root) / 'inputs' / (input_id + '.json')


def fingerprint(request, key):
    return hmac.new(bytes.fromhex(key), json.dumps(request, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8'), hashlib.sha256).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def submit(control, request):
    # Connecting, authenticating and writing a large pipe frame may block in
    # native I/O. Keep the caller responsive and prohibit a late commit after
    # cancellation, even if the background connector wakes much later.
    state = {'lock': threading.Lock(), 'done': threading.Event(), 'abandoned': False,
             'committed': False, 'connection': None}
    def run():
        try:
            state['result'] = _submit(control, request, state)
        except BaseException as exc:
            state['error'] = exc
        finally:
            state['done'].set()
    threading.Thread(target=copy_context().run, args=(run,), daemon=True).start()
    deadline = tool_deadline.get()
    try:
        while not state['done'].wait(.05):
            cancelled = cancellation_requested()
            timed_out = deadline is not None and time.monotonic() >= deadline
            if cancelled or timed_out:
                with state['lock']:
                    state['abandoned'] = True
                    committed = state['committed']
                raise ToolError('unknown_execution' if committed else 'cancelled' if cancelled else 'tool_timeout',
                    ('输入已提交但回执未确认；查询 ' + request['input_id'] + '，禁止重发') if committed
                    else '输入尚未提交，连接已取消或超时')
        if 'error' in state:
            raise state['error']
        return state['result']
    except BaseException:
        with state['lock']:
            state['abandoned'] = True
            connection = state['connection']
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        raise


def _submit(control, request, state):
    committed = False
    connection = None
    try:
        connection = Client(control['address'], family=control['family'], authkey=bytes.fromhex(control['key']))
        with state['lock']:
            state['connection'] = connection
            if state['abandoned']:
                raise ToolError('cancelled', '输入连接已取消；未提交输入')
        connection.send_bytes(encode(request))
        while not connection.poll(.1):
            if cancellation_requested():
                raise ToolError('cancelled', '输入尚未提交，连接已取消')
        result = json.loads(connection.recv_bytes())
        if result.get('status') == 'ready':
            if cancellation_requested():
                raise ToolError('cancelled', '输入尚未提交，连接已取消')
            # After this frame, loss of the acknowledgement is uncertainty, never
            # permission to resend. The same input_id only returns its receipt.
            with state['lock']:
                if state['abandoned']:
                    raise ToolError('cancelled', '输入连接已取消；未提交输入')
                committed = state['committed'] = True
            connection.send_bytes(encode({'commit': request['input_id']}))
            while not connection.poll(.1):
                if cancellation_requested():
                    raise ToolError('unknown_execution', '输入已提交但回执未收取；请查询 input_id，禁止重发')
            result = json.loads(connection.recv_bytes())
        return result
    except ToolError:
        raise
    except (OSError, EOFError, ValueError) as exc:
        code = 'unknown_execution' if committed else 'job_control_unavailable'
        raise ToolError(code, '输入回执未取得；请查询 ' + request['input_id'] +
                        ('，禁止重发' if committed else '；尚未发送提交帧')) from exc
    finally:
        if connection is not None:
            connection.close()


class InputServer:
    def __init__(self, root, control, limit, *, write, close_input, resize, validate, redactor):
        self.root, self.control, self.limit = Path(root), control, limit
        self.write, self.close_input, self.resize = write, close_input, resize
        self.validate, self.redactor = validate, redactor
        self.closed = threading.Event()
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.pending_bytes = 0
        self.eof = False
        self.fault = None
        self.listener = None
        self.thread = self.writer = None

    def start(self):
        (self.root / 'inputs').mkdir(exist_ok=True)
        self.listener = Listener(self.control['address'], family=self.control['family'],
                                 authkey=bytes.fromhex(self.control['key']))
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.writer.start()
        self.thread.start()
        return self

    def _serve(self):
        while not self.closed.is_set():
            connection = None
            try:
                connection = self.listener.accept()
                self._handle(connection)
            except AuthenticationError:
                continue
            except ToolError as exc:
                if connection is not None:
                    try:
                        connection.send_bytes(encode({'status': 'rejected', 'code': exc.error_type,
                                                      'error': self.redactor.clean(str(exc))}))
                    except (OSError, EOFError):
                        pass
            except (OSError, EOFError):
                if self.closed.is_set():
                    return
            except Exception:
                self.fault = '交互输入服务发生错误；请核对回执，不自动重发输入'
                return
            finally:
                if connection is not None:
                    connection.close()

    def _handle(self, connection):
        request = json.loads(connection.recv_bytes(self.limit * 6 + 4096))
        fields = {'input_id', 'job_id', 'session_id', 'action', 'text', 'secret_chars', 'secret_source', 'rows', 'columns', 'authorization_id'}
        if (not isinstance(request, dict) or set(request) != fields
                or request['job_id'] != self.control['job_id'] or request['session_id'] != self.control['session_id']
                or request['action'] not in {'write', 'close', 'resize'} or not isinstance(request['text'], str)
                or (request['secret_chars'] is not None and (type(request['secret_chars']) is not int
                    or not 0 <= request['secret_chars'] <= len(request['text'])))
                or len(request['text'].encode('utf-8')) > self.limit):
            raise ToolError('validation_error', '输入请求范围或格式无效')
        source = request['secret_source']
        if ((request['secret_chars'] is None) != (source is None)
                or (source is not None and (not isinstance(source, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', source)))):
            raise ToolError('validation_error', '输入密钥来源无效')
        if request['action'] == 'resize':
            interaction({'mode': 'pty', 'rows': request['rows'], 'columns': request['columns']})
        elif request['rows'] is not None or request['columns'] is not None:
            raise ToolError('validation_error', '只有 resize 接受终端尺寸')
        if request['action'] != 'write' and (request['text'] or source is not None):
            raise ToolError('validation_error', 'close/resize 不接受输入文本或密钥')
        path = receipt_path(self.root, request['input_id'])
        digest = fingerprint(request, self.control['key'])
        if path.exists():
            prior = load(path)
            if prior['digest'] != digest:
                raise ToolError('confirmation_invalid', '同一输入编号对应的内容发生变化')
            connection.send_bytes(encode(prior))
            return
        size = len(request['text'].encode('utf-8'))
        with self.lock:
            if self.pending_bytes + max(1, size) > self.limit:
                raise ToolError('input_capacity', '待发送输入达到配置配额；请读取现有回执后再提交')
        self.validate(request)
        record = {'input_id': request['input_id'], 'job_id': request['job_id'], 'digest': digest,
                  'action': request['action'], 'bytes': size, 'status': 'prepared', 'created': time.time()}
        save(path, record)
        connection.send_bytes(encode({**record, 'status': 'ready'}))
        while not connection.poll(.1):
            if self.closed.is_set():
                save(path, {**record, 'status': 'not_sent'})
                return
        try:
            commit = json.loads(connection.recv_bytes(4096))
        except (OSError, EOFError):
            save(path, {**record, 'status': 'not_sent'})
            return
        if commit != {'commit': request['input_id']}:
            save(path, {**record, 'status': 'not_sent'})
            raise ToolError('validation_error', '输入提交帧无效')
        record['status'] = 'accepted'
        save(path, record)
        with self.lock:
            self.pending_bytes += max(1, size)
        self.queue.put((request, record, path))
        connection.send_bytes(encode(record))

    def _write_loop(self):
        while not self.closed.is_set():
            item = self.queue.get()
            if item is None:
                return
            request, record, path = item
            begun = False
            try:
                if self.closed.is_set():
                    record['status'] = 'not_sent'
                else:
                    self.validate(request)
                    if self.eof and request['action'] == 'write':
                        raise ToolError('job_input_closed', '该作业标准输入已关闭')
                    save(path, {**record, 'status': 'sending'})
                    begun = True
                    if request['secret_chars'] is not None:
                        secret = request['text'][:request['secret_chars']]
                        self.redactor.add_secret(secret)
                        self.redactor.add_secret(secret.rstrip('\r\n'))
                    if request['action'] == 'write':
                        self.write(request['text'])
                    elif request['action'] == 'close':
                        self.close_input()
                        self.eof = True
                    else:
                        self.resize(request['rows'], request['columns'])
                    record.update(status='sent', completed=time.time())
            except BaseException as exc:
                record.update(status='unknown' if begun else 'rejected',
                    code='unknown_execution' if begun else getattr(exc, 'error_type', 'job_input_error'),
                    error='输入操作已开始但结果未确认；禁止重发，请核对输出' if begun else '输入未发送：' + self.redactor.clean(str(exc)))
            finally:
                with self.lock:
                    self.pending_bytes -= max(1, record['bytes'])
            try:
                save(path, record)
            except BaseException:
                self.fault = '输入回执无法发布；请核对作业输出，禁止重发'
                return

    def close(self):
        self.closed.set()
        self.queue.put(None)
        if self.listener is not None:
            self.listener.close()
        for thread in (self.thread, self.writer):
            if thread:
                thread.join(.2)
