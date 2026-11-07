import io
import json
from pathlib import Path
import sys
import time
from threading import Timer
from types import SimpleNamespace

import pytest

from genesisai.core.extensions.lsp.client import Rpc
from genesisai.core.extensions.lsp.extension import register_lsp
from genesisai.core.extensions.lsp.service import LanguageService
from genesisai.core.jobs.storage import Slot, load, save
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.run_control import RunControl
from genesisai.shared.security import Access, ToolError
from test_acceptance import call

FIXTURE = Path(__file__).parent / 'fixtures' / 'lsp_server.py'


@pytest.fixture
def setup(tmp_path):
    root, dependency = tmp_path / 'workspace', tmp_path / 'dependency'
    root.mkdir(); dependency.mkdir()
    source, other = root / 'main.py', dependency / 'other.py'
    source.write_text('value = 1\n'); other.write_text('target = 2\n')
    outside = tmp_path / 'outside.py'; outside.write_text('private = 3\n')
    store = Session(tmp_path / 'state', workspace=root)
    store.record.apply(grants=[str(root), str(dependency)], output=str(root)); store.commit()
    access = Access([root, dependency], root, store.root, workspace_root=root)
    runtime = ToolRuntime(store, access, providers=[], permission_mode='auto')
    options = {'log': str(tmp_path / 'server.log'), 'targets': [source.as_uri(), other.as_uri()], 'outside': outside.as_uri()}
    option_path = tmp_path / 'fixture.json'; option_path.write_text(json.dumps(options))
    config = {'.py': {'command': [sys.executable, str(FIXTURE), str(option_path)], 'language': 'python',
        'workspace_roots': [str(root), str(dependency)], 'startup_timeout_seconds': 4, 'query_timeout_seconds': 4}}
    (tmp_path / 'lsp.json').write_text(json.dumps({'servers': config}))
    runtime.lsp = register_lsp(tmp_path, runtime.registry, runtime.loader, state_root=store.root)
    try:
        yield runtime, config, source, other, options, option_path
    finally:
        runtime.lsp.close()
        runtime.close()


def invoke(runtime, args, identity):
    result = runtime.execute(call('read_lsp_result' if 'query_id' in args else 'lsp_query', args, identity))
    assert result['ok'], result
    data = finish(runtime, result['data'], identity)
    assert data['status'] == 'completed', data
    return data


def finish(runtime, data, identity):
    end, attempt = time.monotonic() + 10, 0
    while data['status'] in {'queued', 'running', 'cancelling'}:
        assert time.monotonic() < end, data
        time.sleep(.05)
        attempt += 1
        result = runtime.execute(call('read_lsp_result', {'query_id': data['query_id']}, identity + f'_poll{attempt}'))
        assert result['ok'], result
        data = result['data']
    return data


def test_persistent_service_all_results_multiroot_recovery_and_changed_document(setup):
    runtime, config, source, other, options, _ = setup
    args = {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}
    first = invoke(runtime, args, 'first')
    assert first['total'] == 257 and len(first['locations']) == 100
    assert first['omitted'] == {'unsupported_uri': 1, 'unauthorized_or_missing': 1}
    all_items = first['locations'][:]
    second = invoke(runtime, {'query_id': first['query_id'], 'offset': 100}, 'second')
    third = invoke(runtime, {'query_id': first['query_id'], 'offset': 200}, 'third')
    all_items.extend(second['locations']); all_items.extend(third['locations'])
    assert len(all_items) == 257 and third['next_offset'] is None
    assert {item['path'] for item in all_items} == {str(source), str(other)}
    assert len([json.loads(line) for line in Path(options['log']).read_text().splitlines()
                if json.loads(line)['method'] == 'textDocument/references']) == 1
    source.write_text('value = 999\n')
    diagnostics = invoke(runtime, {'operation': 'diagnostics', 'path': str(source)}, 'changed')
    assert diagnostics['diagnostics'][0]['message'] == source.read_bytes().decode()
    log = [json.loads(line) for line in Path(options['log']).read_text().splitlines()]
    assert len({e['pid'] for e in log}) == 1
    assert len([e for e in log if e['method'] == 'initialize']) == 1
    assert len(next(e['params']['workspaceFolders'] for e in log if e['method'] == 'initialize')) == 2
    stale = runtime.execute(call('read_lsp_result', {'query_id': first['query_id']}, 'stale'))
    assert stale['error']['code'] == 'stale_query'
    runtime.lsp.close()
    recovered = LanguageService(runtime.lsp.servers, runtime.lsp.cache_root.parent, state_root=runtime.store.root)
    page_args = {'query_id': diagnostics['query_id']}
    assert recovered.execute(runtime.access, page_args, recovered.prepare(runtime.access, page_args))['total'] == 1
    caps = invoke(runtime, {'operation': 'capabilities', 'path': str(source)}, 'restart')
    assert 'diagnostics' in caps['supported_operations'] and 'rename' in caps['not_implemented']
    assert len({json.loads(line)['pid'] for line in Path(options['log']).read_text().splitlines()}) == 2


def test_pagination_revalidates_authorization_and_target_hash(setup):
    runtime, _, source, other, _, _ = setup
    first = invoke(runtime, {'operation': 'definition', 'path': str(source), 'line': 1, 'column': 1}, 'first')
    other.write_text('changed = 4\n')
    result = runtime.execute(call('read_lsp_result', {'query_id': first['query_id'], 'offset': 100}, 'changed'))
    assert result['error']['code'] == 'stale_query'
    runtime.access.host_access = False
    runtime.access.roots = [source.parent]
    result = runtime.execute(call('read_lsp_result', {'query_id': first['query_id']}, 'revoked'))
    assert result['error']['code'] == 'permission_denied'


@pytest.mark.parametrize('phase', ['startup', 'query'])
def test_timeout_closes_process_and_can_restart(setup, phase):
    runtime, _, source, _, options, option_path = setup
    options[phase + '_delay'] = 2
    option_path.write_text(json.dumps(options))
    runtime.lsp.servers['.py'][phase + '_timeout_seconds'] = .3
    args = {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}
    started = time.monotonic()
    result = runtime.execute(call('lsp_query', args, 'timeout'))
    assert result['ok'] and result['data']['status'] == 'queued'
    data = finish(runtime, result['data'], 'timeout')
    assert data['status'] == 'failed' and data['error']['code'] == 'lsp_timeout', data
    assert phase in data['error']['message'] and time.monotonic() - started < 10
    assert not runtime.lsp.connections
    options[phase + '_delay'] = 0
    option_path.write_text(json.dumps(options))
    runtime.lsp.servers['.py'][phase + '_timeout_seconds'] = 4
    assert invoke(runtime, args, 'restart')['total'] == 257


def test_cancel_pending_query_releases_owned_process(setup):
    runtime, _, source, _, options, option_path = setup
    options['query_delay'] = 4; option_path.write_text(json.dumps(options))
    control = RunControl(lambda *a: None)
    timer = Timer(.8, control.cancel); timer.start()
    try:
        result = control.execute(lambda: runtime.execute(call('lsp_query',
            {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}, 'cancel')))
        assert result['ok'] and result['data']['status'] == 'queued'
        data = finish(runtime, result['data'], 'cancel')
        assert data['status'] == 'cancelled' and data['error']['code'] == 'user_cancelled', data
        assert not runtime.lsp.connections
    finally:
        timer.cancel()


def test_long_query_returns_immediately_and_explicit_cancel_works(setup):
    runtime, _, source, _, options, option_path = setup
    options['query_delay'] = 8; option_path.write_text(json.dumps(options))
    started = time.monotonic()
    result = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}, 'async'))
    assert result['ok'] and result['data']['status'] == 'queued'
    assert time.monotonic() - started < 2
    cancel = runtime.execute(call('cancel_lsp_query', {'query_id': result['data']['query_id']}, 'stop'))
    assert cancel['ok']
    assert finish(runtime, result['data'], 'stopped')['status'] == 'cancelled'


def test_independent_servers_run_concurrently_and_missing_owner_is_not_replayed(setup):
    runtime, _, source, _, options, option_path = setup
    options['query_delay'] = 1; option_path.write_text(json.dumps(options))
    script = source.with_suffix('.js'); script.write_text('const value = 1;\n')
    runtime.lsp.servers['.js'] = {**runtime.lsp.servers['.py'], 'language': 'javascript'}
    first = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}, 'one'))
    second = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(script), 'line': 1, 'column': 1}, 'two'))
    assert first['ok'] and second['ok']
    assert finish(runtime, first['data'], 'one')['status'] == 'completed'
    assert finish(runtime, second['data'], 'two')['status'] == 'completed'
    queries = [json.loads(line) for line in Path(options['log']).read_text().splitlines()
               if json.loads(line)['method'] == 'textDocument/references']
    assert len({entry['pid'] for entry in queries}) == 2
    assert abs(queries[0]['time'] - queries[1]['time']) < .9
    snapshot_path = runtime.lsp.cache_root / (first['data']['query_id'] + '.json')
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
    snapshot.update(status='running', heartbeat=time.time() - 30)
    snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
    orphan = runtime.execute(call('read_lsp_result', {'query_id': first['data']['query_id']}, 'orphan'))
    assert orphan['ok'] and orphan['data']['status'] == 'unknown'
    cancellation = runtime.execute(call('cancel_lsp_query', {'query_id': first['data']['query_id']}, 'orphan_cancel'))
    assert cancellation['ok'] and cancellation['data']['status'] == 'unknown'
    assert cancellation['data']['cancel_requested'] is True
    assert len([line for line in Path(options['log']).read_text().splitlines()
                if json.loads(line)['method'] == 'textDocument/references']) == 2


def test_large_source_is_rejected_before_reading_but_capabilities_remain_available(setup):
    runtime, _, source, _, _, _ = setup
    source.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    result = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}, 'large'))
    assert not result['ok'] and result['error']['code'] == 'lsp_protocol'
    assert not runtime.lsp.connections
    assert 'references' in invoke(runtime, {'operation': 'capabilities', 'path': str(source)}, 'caps')['supported_operations']


def occupy_global_slots(runtime, count):
    root = runtime.store.root / 'jobs'
    (root / 'slots').mkdir(parents=True, exist_ok=True)
    save(root / 'limits.json', {'max_concurrent': count})
    return [Slot(root / 'slots' / f'{index}.lock') for index in range(count)]


def test_global_slots_keep_query_queued_with_heartbeat_until_released(setup):
    runtime, _, source, _, options, _ = setup
    slots = occupy_global_slots(runtime, 2)
    runtime.lsp.servers['.py'].update(startup_timeout_seconds=.5, query_timeout_seconds=.5)
    try:
        started = time.monotonic()
        submitted = runtime.execute(call('lsp_query', {'operation': 'capabilities', 'path': str(source)}, 'queued'))
        assert submitted['ok'] and submitted['data']['status'] == 'queued'
        assert time.monotonic() - started < 2
        identity = submitted['data']['query_id']
        snapshot_path = runtime.lsp.cache_root / (identity + '.json')
        first = load(snapshot_path)
        time.sleep(1.2)  # More than startup + query budgets: queueing consumes neither.
        current = load(snapshot_path)
        assert current['status'] == 'queued' and current['heartbeat'] > first['heartbeat']
        assert not Path(options['log']).exists() and not runtime.lsp.connections
        observed = runtime.execute(call('read_lsp_result', {'query_id': identity}, 'observe_queued'))
        assert observed['ok'] and observed['data']['status'] == 'queued'
        slots.pop().close()
        assert finish(runtime, submitted['data'], 'released')['status'] == 'completed'
    finally:
        for slot in slots:
            slot.close()


def test_query_cancelled_while_global_slots_full_never_starts_server(setup):
    runtime, _, source, _, options, _ = setup
    slots = occupy_global_slots(runtime, 1)
    try:
        submitted = runtime.execute(call('lsp_query', {'operation': 'capabilities', 'path': str(source)}, 'queued_cancel'))
        assert submitted['ok']
        time.sleep(.2)
        stopped = runtime.execute(call('cancel_lsp_query', {'query_id': submitted['data']['query_id']}, 'cancel_queued'))
        assert stopped['ok']
        result = finish(runtime, submitted['data'], 'cancelled')
        assert result['status'] == 'cancelled' and result['error']['code'] == 'user_cancelled'
        assert not Path(options['log']).exists() and not runtime.lsp.connections
    finally:
        for slot in slots:
            slot.close()
    assert 'references' in invoke(runtime, {'operation': 'capabilities', 'path': str(source)}, 'after_cancel')['supported_operations']


def test_local_queue_cancellation_does_not_wait_for_active_query(setup):
    runtime, _, source, _, options, option_path = setup
    runtime.lsp.max_concurrent = 1
    options['query_delay'] = 8
    option_path.write_text(json.dumps(options))
    runtime.lsp.servers['.py']['query_timeout_seconds'] = 10
    first = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}, 'busy'))
    second = runtime.execute(call('lsp_query', {'operation': 'capabilities', 'path': str(source)}, 'pending'))
    assert first['ok'] and second['ok']
    try:
        stopped = runtime.execute(call('cancel_lsp_query', {'query_id': second['data']['query_id']}, 'stop_pending'))
        assert stopped['ok']
        started = time.monotonic()
        assert finish(runtime, second['data'], 'pending_cancelled')['status'] == 'cancelled'
        assert time.monotonic() - started < 2
        assert runtime.lsp._snapshot(first['data']['query_id'])['status'] in {'queued', 'running'}
    finally:
        runtime.execute(call('cancel_lsp_query', {'query_id': first['data']['query_id']}, 'stop_busy'))


@pytest.mark.parametrize('global_limit,local_limit', [(1, 4), (4, 1)])
def test_global_and_local_limits_both_constrain_independent_servers(setup, global_limit, local_limit):
    runtime, _, source, _, options, option_path = setup
    slots = occupy_global_slots(runtime, global_limit)
    for slot in slots:
        slot.close()
    runtime.lsp.max_concurrent = local_limit
    options['query_delay'] = 1
    option_path.write_text(json.dumps(options))
    script = source.with_suffix('.js'); script.write_text('const value = 1;\n')
    runtime.lsp.servers['.js'] = {**runtime.lsp.servers['.py'], 'language': 'javascript'}
    first = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(source), 'line': 1, 'column': 1}, 'limited_one'))
    second = runtime.execute(call('lsp_query', {'operation': 'references', 'path': str(script), 'line': 1, 'column': 1}, 'limited_two'))
    assert first['ok'] and second['ok']
    assert finish(runtime, first['data'], 'limited_one')['status'] == 'completed'
    assert finish(runtime, second['data'], 'limited_two')['status'] == 'completed'
    queries = [json.loads(line) for line in Path(options['log']).read_text().splitlines()
               if json.loads(line)['method'] == 'textDocument/references']
    assert len({entry['pid'] for entry in queries}) == 2
    assert abs(queries[0]['time'] - queries[1]['time']) >= 1
    # Completion returns every global slot; persistent idle connections do not occupy them.
    slots = [Slot(runtime.store.root / 'jobs' / 'slots' / f'{index}.lock') for index in range(global_limit)]
    for slot in slots:
        slot.close()


@pytest.mark.parametrize('body', [b'Content-Length: 9000000\r\n\r\n', b'Content-Length: -1\r\n\r\n',
    b'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}', b'Content-Length: 20\r\n\r\n{}', b'X: 1\r\n' * 33])
def test_protocol_bounds_fail_without_hanging(body):
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(body))
    rpc = Rpc(process, [])
    try:
        with pytest.raises(ToolError, match='语言服务'):
            rpc.request('initialize', {}, time.monotonic() + 2, 'startup')
    finally:
        rpc.stopped.set(); rpc.reader.join(timeout=2)
