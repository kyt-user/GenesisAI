"""Host-owned LSP connections and durable, scope-checked result pages."""
import json
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor
from threading import RLock, Event, Thread
from urllib.parse import urlsplit
from urllib.request import url2pathname

from genesisai.core.extensions.tools.filesystem.shared import read_text_document
from genesisai.core.jobs.storage import Slot, SlotBusy, load, save
from genesisai.shared.ids import sha, uid
from genesisai.shared.security import ToolError
from genesisai.shared.run_control import RunControl, current_run
from genesisai.shared.time_limits import tool_deadline
from .client import Connection, MAX_MESSAGE_BYTES, check


def region(value):
    try:
        start, end = value['start'], value['end']
        numbers = [start['line'], start['character'], end['line'], end['character']]
        if any(type(v) is not int or v < 0 for v in numbers) or numbers[2:] < numbers[:2]:
            raise ValueError()
        return dict(zip(('line', 'column', 'end_line', 'end_column'), (v + 1 for v in numbers)))
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError('lsp_protocol', '语言服务位置范围无效') from exc


class LanguageService:
    def __init__(self, servers, project_dir, *, state_root, max_concurrent=4):
        self.servers, self.cache_root = servers, Path(project_dir).resolve() / 'lsp-results'
        self.job_root = Path(state_root).resolve() / 'jobs'
        self.max_concurrent = max_concurrent
        self.connections, self.lock = {}, RLock()
        self.guards, self.jobs = {}, {}
        self.failures = {}
        self.executor = None
        self.closing = False
        self.pulse_stop = Event()

    def _save(self, snapshot):
        if self.cache_root.resolve() != self.cache_root:
            raise ToolError('invalid_path', '语言查询快照目录不能通过链接重定向')
        self.cache_root.mkdir(parents=True, exist_ok=True)
        path = self.cache_root / (snapshot['query_id'] + '.json')
        save(path, snapshot)

    def _pulse(self):
        while not self.pulse_stop.wait(.5):
            with self.lock:
                jobs = list(self.jobs.values())
            for job in jobs:
                with job['lock']:
                    snapshot = job['snapshot']
                    if ((job['parent'] and job['parent'].cancelled.is_set())
                            or (self.cache_root / (snapshot['query_id'] + '.cancel')).exists()):
                        job['control'].cancel()
                    if snapshot['status'] in {'queued', 'running'}:
                        snapshot['heartbeat'] = time.time()
                        try:
                            if job['control'].cancelled.is_set() and job['future'].cancel():
                                snapshot.update(status='cancelled', error={'code': 'user_cancelled', 'message': '语言服务查询已取消'})
                                with self.lock:
                                    self.jobs.pop(snapshot['query_id'], None)
                            self._save(snapshot)
                        except OSError:
                            job['control'].cancel()
                            with self.lock:
                                self.failures[snapshot['query_id']] = '语言查询状态写入失败，请核对；未自动重发'

    def roots(self, server, access):
        roots = []
        for value in server.get('workspace_roots', [str(access.workspace)]):
            raw = Path(value)
            path = access.read(raw if raw.is_absolute() else access.workspace / raw)
            if not path.is_dir():
                raise ToolError('invalid_path', '语言服务工作根必须是授权目录')
            if path not in roots:
                roots.append(path)
        return roots

    def target(self, value, roots, access):
        path = access.read(value)
        if not path.is_file() or not any(path.is_relative_to(root) for root in roots):
            raise ToolError('permission_denied', '路径不属于已声明且授权的语言服务工作根')
        return path

    def _snapshot(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'lsp_[a-f0-9]{16}', identity):
            raise ToolError('invalid_query', '语言查询引用无效')
        path = self.cache_root / (identity + '.json')
        if path.resolve() != path or not path.is_file():
            raise ToolError('invalid_query', '语言查询快照不存在或路径无效')
        snapshot = load(path)
        if identity in self.failures:
            snapshot.update(status='unknown', error=self.failures[identity])
        if snapshot['status'] in {'queued', 'running'} and time.time() - snapshot['heartbeat'] > 15:
            snapshot.update(status='unknown', error='语言查询宿主心跳失联；未自动重发，请核对或取消后重新查询')
        return snapshot

    def prepare(self, access, args):
        offset, limit = args.get('offset', 0), args.get('limit', 100)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ToolError('invalid_query', '分页参数无效')
        if args.get('query_id'):
            if set(args) - {'query_id', 'offset', 'limit'}:
                raise ToolError('invalid_query', '引用分页不能同时指定新查询')
            snapshot = self._snapshot(args['query_id'])
            if snapshot['workspace'] != str(access.workspace):
                raise ToolError('permission_denied', '查询引用不属于当前工作区')
            path_value, operation = snapshot['source'], snapshot['operation']
        else:
            if offset:
                raise ToolError('invalid_query', '后续页面必须指定 query_id')
            path_value, operation = args.get('path'), args.get('operation')
            snapshot = None
        if not isinstance(path_value, str) or operation not in {'definition', 'references', 'diagnostics', 'capabilities'}:
            raise ToolError('lsp_unsupported', '需要 path 和已实现的 operation')
        path = Path(path_value)
        path = access.read(path if path.is_absolute() else access.workspace / path)
        server = self.servers.get(path.suffix.lower())
        if server is None:
            raise ToolError('lsp_unsupported', '该文件类型没有配置语言服务')
        roots = self.roots(server, access)
        path = self.target(path, roots, access)
        if operation != 'capabilities' and path.stat().st_size > MAX_MESSAGE_BYTES:
            raise ToolError('lsp_protocol', '文档超过8MiB语言服务协议消息预算')
        digest = sha(path) if operation != 'capabilities' else None
        if snapshot and (snapshot['sha256'] != digest or snapshot['roots'] != [str(p) for p in roots]
                         or snapshot['server'] != server):
            raise ToolError('stale_query', '源文件、工作根或服务配置变化，请重新查询')
        if not snapshot and operation in {'definition', 'references'}:
            text = read_text_document(path)[0]
            line, column = args.get('line'), args.get('column')
            lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            if (type(line) is not int or type(column) is not int or not 1 <= line <= len(lines)
                    or not 1 <= column <= 1 + len(lines[line-1].encode('utf-16-le')) // 2):
                raise ToolError('invalid_position', '查询位置需要文档范围内的正整数行列（UTF-16）')
        return {'target': str(path), 'command': server['command'], 'language': server['language'],
                'operation': operation, 'sha256': digest, 'workspace_roots': [str(p) for p in roots]}

    def execute(self, access, args, preview):
        current = self.prepare(access, args)
        if current != preview:
            raise ToolError('stale_query', '查询审批后源文件或工作根发生变化')
        if args.get('query_id'):
            return self.page(self._snapshot(args['query_id']), access, args)
        identity = uid('lsp')
        server = self.servers[Path(current['target']).suffix.lower()]
        snapshot = {**current, 'source': current['target'], 'workspace': str(access.workspace),
                    'server': server, 'roots': current['workspace_roots'], 'query_id': identity,
                    'status': 'queued', 'heartbeat': time.time()}
        control = RunControl(lambda *args: None)
        parent = current_run.get()
        with self.lock:
            if self.closing:
                raise ToolError('lsp_closing', '语言服务正在关闭，请稍后重新查询')
            self._save(snapshot)
            if self.executor is None:
                self.executor = ThreadPoolExecutor(max_workers=self.max_concurrent, thread_name_prefix='genesis-lsp')
                self.pulse_stop = Event()
                self.pulse = Thread(target=self._pulse, daemon=True)
                self.pulse.start()
            job = {'snapshot': snapshot, 'control': control, 'lock': RLock(), 'parent': parent}
            self.jobs[identity] = job
            job['future'] = self.executor.submit(self._run, job, access, args, preview)
            job['future'].add_done_callback(lambda future: self._finished(identity, future))
        return {'query_id': identity, 'status': 'queued', 'read_more': {'tool': 'read_lsp_result', 'query_id': identity}}

    def _finished(self, identity, future):
        if future.cancelled():
            return
        if future.exception() is not None:
            with self.lock:
                self.failures[identity] = '语言查询最终状态无法保存，请核对；未自动重发'

    def _run(self, job, access, args, preview):
        token = tool_deadline.set(None)
        try:
            result = job['control'].execute(lambda: self._query(access, args, preview, job))
            with job['lock']:
                job['snapshot'].update(result, status='completed', heartbeat=time.time())
                self._save(job['snapshot'])
        except BaseException as exc:
            with job['lock']:
                job['snapshot'].update(status='cancelled' if job['control'].cancelled.is_set() else 'failed',
                    error={'code': getattr(exc, 'error_type', 'lsp_error'), 'message': str(exc)}, heartbeat=time.time())
                self._save(job['snapshot'])
        finally:
            tool_deadline.reset(token)
            with self.lock:
                self.jobs.pop(job['snapshot']['query_id'], None)

    def _global_slot(self, job):
        settings = self.job_root / 'limits.json'
        limits = {'max_concurrent': 4, 'log_max_bytes': 128 * 1024**2, 'result_max_bytes': 128 * 1024**2}
        if settings.exists():
            supplied = load(settings)
            if not isinstance(supplied, dict) or supplied.keys() - limits.keys():
                raise ToolError('job_config', '作业 limits.json 包含未知设置')
            limits.update(supplied)
        if any(type(value) is not int or value < 1 for value in limits.values()):
            raise ToolError('job_config', '作业资源配置必须为正整数')
        pool = self.job_root / 'slots'
        pool.mkdir(parents=True, exist_ok=True)
        while True:
            check(float('inf'), 'queue')
            for index in range(limits['max_concurrent']):
                try:
                    return Slot(pool / f'{index}.lock')
                except SlotBusy:
                    continue
            job['control'].cancelled.wait(.1)

    def _query(self, access, args, preview, job):
        current = self.prepare(access, args)
        if current != preview:
            raise ToolError('stale_query', '查询审批后源文件或工作根发生变化')
        path = Path(current['target'])
        server = self.servers[path.suffix.lower()]
        roots = [Path(value) for value in current['workspace_roots']]
        key = json.dumps([str(access.workspace), roots_as_strings(roots), server], sort_keys=True)
        with self.lock:
            guard = self.guards.setdefault(key, RLock())
        while not guard.acquire(timeout=.1):
            check(float('inf'), 'queue')
        slot = None
        try:
            # Do not hold a global execution slot while waiting for this connection.
            slot = self._global_slot(job)
            check(float('inf'), 'queue')
            if self.prepare(access, args) != preview:
                raise ToolError('stale_query', '排队期间源文件或工作根发生变化')
            with job['lock']:
                job['snapshot'].update(status='running', heartbeat=time.time())
                self._save(job['snapshot'])
            connection = self.connections.get(key)
            if connection and connection.process.poll() is not None:
                connection.close()
                del self.connections[key]
                connection = None
            if connection is None:
                connection = Connection(server, access.workspace, roots)
                self.connections[key] = connection
            caps = connection.capabilities
            supported = [op for op, cap in [('definition', 'definitionProvider'), ('references', 'referencesProvider'),
                                           ('diagnostics', 'diagnosticProvider')] if caps.get(cap) not in (None, False)]
            if current['operation'] == 'capabilities':
                return {'capabilities': {'supported_operations': supported, 'implemented_operations': ['definition', 'references', 'diagnostics'],
                        'not_implemented': ['rename', 'codeAction', 'formatting', 'push_diagnostics'],
                        'workspace_roots': current['workspace_roots'], 'position_encoding': 'utf-16'}}
            text = read_text_document(path)[0]
            result = connection.query(path, server['language'], text, current['operation'], args.get('line'), args.get('column'))
            if sha(path) != current['sha256']:
                raise ToolError('stale_query', '查询期间源文件变化，请重新查询')
            items, omitted = self.normalize(result, current['operation'], path, roots, access)
            return {**current, 'source': str(path), 'workspace': str(access.workspace), 'server': server,
                    'roots': roots_as_strings(roots), 'query_id': job['snapshot']['query_id'], 'items': items, 'omitted': omitted}
        except BaseException:
            if key in self.connections:
                self.connections.pop(key).close()
            raise
        finally:
            if slot is not None:
                slot.close()
            guard.release()

    def normalize(self, result, operation, source, roots, access):
        if operation == 'diagnostics':
            if not isinstance(result, dict) or result.get('kind') != 'full' or not isinstance(result.get('items'), list):
                raise ToolError('lsp_protocol', '需要完整的文档诊断报告')
            values = [(source.as_uri(), item, True) for item in result['items']]
            for uri, related in (result.get('relatedDocuments') or {}).items():
                if related.get('kind') != 'full':
                    raise ToolError('lsp_protocol', '关联文档诊断不完整')
                values.extend((uri, item, True) for item in related['items'])
        else:
            values = [(entry.get('uri') or entry.get('targetUri'), entry, False)
                      for entry in ([] if result is None else result if isinstance(result, list) else [result])]
        items, omitted, digests = [], {}, {}
        for uri, entry, diagnostic in values:
            check(float('inf'), 'query')
            parsed = urlsplit(uri or '')
            if parsed.scheme.lower() != 'file' or parsed.netloc not in {'', 'localhost'} or parsed.query or parsed.fragment:
                omitted['unsupported_uri'] = omitted.get('unsupported_uri', 0) + 1
                continue
            try:
                path = self.target(Path(url2pathname(parsed.path)), roots, access)
            except (ToolError, OSError):
                omitted['unauthorized_or_missing'] = omitted.get('unauthorized_or_missing', 0) + 1
                continue
            if path not in digests:
                digests[path] = sha(path)
            item = {'path': str(path), 'sha256': digests[path], **region(entry.get('range') or entry.get('targetSelectionRange'))}
            if diagnostic:
                if not isinstance(entry.get('message'), str):
                    raise ToolError('lsp_protocol', '诊断缺少文本')
                item.update({key: entry[key] for key in ('message', 'severity', 'code', 'source', 'tags') if key in entry})
            items.append(item)
        return items, omitted

    def page(self, snapshot, access, args):
        if snapshot['status'] != 'completed':
            return {'query_id': snapshot['query_id'], 'status': snapshot['status'], 'error': snapshot.get('error'),
                    'read_more': {'tool': 'read_lsp_result', 'query_id': snapshot['query_id']}}
        if snapshot['operation'] == 'capabilities':
            return {'query_id': snapshot['query_id'], 'status': 'completed', **snapshot['capabilities']}
        offset, limit = args.get('offset', 0), args.get('limit', 100)
        items = snapshot['items'][offset:offset + limit]
        digests = {}
        for item in items:
            path = self.target(item['path'], [Path(p) for p in snapshot['roots']], access)
            if path not in digests:
                digests[path] = sha(path)
            if digests[path] != item['sha256']:
                raise ToolError('stale_query', '查询目标文件变化，请重新查询')
        return {'query_id': snapshot['query_id'], 'status': 'completed', 'operation': snapshot['operation'],
                'diagnostics' if snapshot['operation'] == 'diagnostics' else 'locations': items,
                'offset': offset, 'next_offset': offset + len(items) if offset + len(items) < len(snapshot['items']) else None,
                'total': len(snapshot['items']), 'omitted': snapshot['omitted'], 'position_encoding': 'utf-16',
                'source_sha256': snapshot['sha256'], 'workspace_roots': snapshot['roots']}

    def cancel(self, access, identity):
        snapshot = self._snapshot(identity)
        if snapshot['workspace'] != str(access.workspace):
            raise ToolError('permission_denied', '查询引用不属于当前工作区')
        if snapshot['status'] in {'queued', 'running', 'unknown'}:
            path = self.cache_root / (identity + '.cancel')
            try:
                with path.open('x', encoding='utf-8') as stream:
                    stream.write('cancel requested')
            except FileExistsError:
                pass
            with self.lock:
                if identity in self.jobs:
                    self.jobs[identity]['control'].cancel()
            return {'query_id': identity, 'status': 'unknown' if snapshot['status'] == 'unknown' else 'cancelling',
                    'cancel_requested': True}
        return {'query_id': identity, 'status': snapshot['status']}

    def close(self):
        with self.lock:
            self.closing = True
            executor, self.executor = self.executor, None
            for job in self.jobs.values():
                job['control'].cancel()
        if executor:
            executor.shutdown(wait=True)
            self.pulse_stop.set()
            self.pulse.join(timeout=2)
        with self.lock:
            values, self.connections = list(self.connections.values()), {}
            for connection in values:
                connection.close()
            self.closing = False


def roots_as_strings(roots):
    return [str(path) for path in roots]
