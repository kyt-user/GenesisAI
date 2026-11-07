"""Persistent, cancellable LSP transport with bounded protocol frames."""
import json
import subprocess
import time
from queue import Queue, Empty, Full
from threading import Event, Thread

from genesisai.shared.process_tree import owned_process, safe_environment
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline

MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def deadline(seconds):
    end = time.monotonic() + seconds
    host = tool_deadline.get()
    return min(end, host) if host is not None else end


def check(end, phase):
    if cancellation_requested():
        raise ToolError('user_cancelled', '语言服务查询已取消')
    if time.monotonic() >= end:
        raise ToolError('lsp_timeout', f'语言服务 {phase} 超时')


class Rpc:
    def __init__(self, process, roots):
        self.process, self.roots = process, roots
        self.sequence = 0
        self.inbox, self.stopped = Queue(maxsize=16), Event()
        self.reader = Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _read(self):
        length, complete = None, False
        for _ in range(32):
            header = self.process.stdout.readline(2048)
            if header in (b'\r\n', b'\n'):
                complete = True
                break
            if not header.endswith(b'\n'):
                raise ToolError('lsp_protocol', '语言服务响应头不完整')
            key, separator, value = header.partition(b':')
            if not separator:
                raise ToolError('lsp_protocol', '语言服务响应头无效')
            if key.lower() == b'content-length':
                if length is not None:
                    raise ToolError('lsp_protocol', '语言服务重复消息长度')
                try:
                    length = int(value.strip())
                except ValueError as exc:
                    raise ToolError('lsp_protocol', '语言服务消息长度无效') from exc
        if not complete or length is None or not 0 < length <= MAX_MESSAGE_BYTES:
            raise ToolError('lsp_protocol', '语言服务消息长度或头数量无效')
        data = bytearray()
        while len(data) < length:
            chunk = self.process.stdout.read(length - len(data))
            if not chunk:
                raise ToolError('lsp_protocol', '语言服务消息体不完整')
            data.extend(chunk)
        try:
            message = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ToolError('lsp_protocol', '语言服务 JSON 无效') from exc
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ToolError('lsp_protocol', '语言服务 JSON-RPC 消息无效')
        return message

    def _reader(self):
        while not self.stopped.is_set():
            try:
                value = self._read()
            except Exception as exc:
                value = exc
            while not self.stopped.is_set():
                try:
                    self.inbox.put(value, timeout=.1)
                    break
                except Full:
                    continue
            if isinstance(value, Exception):
                return

    def send(self, end, phase, **message):
        data = json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False).encode('utf-8')
        if len(data) > MAX_MESSAGE_BYTES:
            raise ToolError('lsp_protocol', '语言服务请求超过8MiB协议消息预算')
        completed, errors = Event(), []
        def write():
            try:
                self.process.stdin.write(f'Content-Length: {len(data)}\r\n\r\n'.encode() + data)
                self.process.stdin.flush()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()
        Thread(target=write, daemon=True).start()
        while not completed.wait(.05):
            check(end, phase)
        check(end, phase)
        if errors:
            raise ToolError('lsp_disconnected', '语言服务写入失败') from errors[0]

    def request(self, method, params, end, phase):
        self.sequence += 1
        self.send(end, phase, id=self.sequence, method=method, params=params)
        while True:
            check(end, phase)
            try:
                message = self.inbox.get(timeout=min(.1, max(.001, end - time.monotonic())))
            except Empty:
                continue
            if isinstance(message, Exception):
                raise message
            if 'method' in message:
                if 'id' in message:
                    method = message['method']
                    if method == 'workspace/configuration':
                        result = [None] * len(message.get('params', {}).get('items', []))
                    elif method == 'workspace/workspaceFolders':
                        result = self.roots
                    elif method == 'window/workDoneProgress/create':
                        result = None
                    else:
                        self.send(end, phase, id=message['id'], error={'code': -32601, 'message': 'Unsupported client method'})
                        continue
                    self.send(end, phase, id=message['id'], result=result)
                continue
            if message.get('id') != self.sequence:
                raise ToolError('lsp_protocol', '语言服务响应 ID 无效')
            if 'error' in message:
                raise ToolError('lsp_error', str(message['error'])[:600])
            if 'result' not in message:
                raise ToolError('lsp_protocol', '语言服务缺少结果')
            return message['result']


class Connection:
    def __init__(self, server, workspace, roots):
        self.server, self.documents = server, {}
        self.owner = owned_process(server['command'], cwd=workspace, env=safe_environment(),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.process = self.owner.__enter__()
        self.closed = False
        self.rpc = Rpc(self.process, [{'uri': root.as_uri(), 'name': root.name} for root in roots])
        try:
            end = deadline(server['startup_timeout_seconds'])
            result = self.rpc.request('initialize', {
                'processId': None, 'rootUri': roots[0].as_uri(), 'workspaceFolders': self.rpc.roots,
                'capabilities': {'workspace': {'workspaceFolders': True, 'configuration': True},
                                 'general': {'positionEncodings': ['utf-16']},
                                 'textDocument': {'diagnostic': {'dynamicRegistration': False}}}}, end, 'startup')
            self.capabilities = result['capabilities']
            if len(roots) > 1 and not self.capabilities.get('workspace', {}).get('workspaceFolders', {}).get('supported'):
                raise ToolError('lsp_unsupported', '语言服务未声明多根工作区支持')
            if self.capabilities.get('positionEncoding', 'utf-16') != 'utf-16':
                raise ToolError('lsp_unsupported', '语言服务位置编码不是 UTF-16')
            self.rpc.send(end, 'startup', method='initialized', params={})
        except BaseException:
            self.close()
            raise

    def query(self, path, language, text, operation, line=None, column=None):
        end = deadline(self.server['query_timeout_seconds'])
        uri = path.as_uri()
        version = self.documents.get(uri, 0) + 1
        if version > 1:
            self.rpc.send(end, 'query', method='textDocument/didClose', params={'textDocument': {'uri': uri}})
        self.rpc.send(end, 'query', method='textDocument/didOpen', params={'textDocument': {
            'uri': uri, 'languageId': language, 'version': version, 'text': text}})
        self.documents[uri] = version
        provider = 'diagnosticProvider' if operation == 'diagnostics' else operation + 'Provider'
        if self.capabilities.get(provider) in (None, False):
            raise ToolError('lsp_unsupported', '语言服务未声明请求能力：' + operation)
        params = {'textDocument': {'uri': uri}}
        if operation in {'definition', 'references'}:
            params['position'] = {'line': line - 1, 'character': column - 1}
        if operation == 'references':
            params['context'] = {'includeDeclaration': True}
        method = 'diagnostic' if operation == 'diagnostics' else operation
        return self.rpc.request('textDocument/' + method, params, end, 'query')

    def close(self):
        if not self.closed:
            self.closed = True
            self.rpc.stopped.set()
            self.owner.__exit__(None, None, None)
            self.rpc.reader.join(timeout=2)
