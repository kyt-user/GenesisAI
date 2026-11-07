"""Explicit scope grants and isolated processes, exercised through real workers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from genesisai.app.authorization_commands import execute_authorization_command
from genesisai.core.jobs.storage import Slot
from genesisai.core.persistence.session import Session
from genesisai.core.tools.permissions import PermissionPolicy, current_authorization
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.shared.process_tree import safe_environment
from genesisai.shared.security import Access, ToolError


@pytest.fixture
def runtime(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = Session(tmp_path / 'state', workspace=workspace)
    store.record.apply(grants=[str(workspace)], output=str(workspace))
    store.commit()
    runtime = ToolRuntime(store, Access([workspace], workspace, store.root, workspace_root=workspace),
                          permission_mode='user', providers=[])
    try:
        yield runtime
    finally:
        runtime.jobs.stop_all()
        runtime.close()


def invoke(runtime, name, args, identity, approved=None):
    return runtime.execute({'id': identity, 'name': name, 'arguments': json.dumps(args)}, approved)


def grant(runtime, **values):
    return execute_authorization_command(runtime, '/authorize ' + json.dumps(values))


def test_path_grant_reused_revoked_and_bound_to_session(runtime):
    allowed = runtime.store.workspace / 'reports'
    authorization = grant(runtime, scope='session', tools=['editor'], paths=[str(allowed)])
    for number in range(2):
        result = invoke(runtime, 'editor', {'operation': 'create', 'path': f'reports/{number}.md', 'content': 'ok'}, f'write_{number}')
        assert result['ok'], result
    pending = invoke(runtime, 'editor', {'operation': 'create', 'path': 'outside.md', 'content': 'x'}, 'outside')
    assert pending['pending']
    restored = Session(runtime.store.root, runtime.store.id)
    assert restored.record.authorizations[0]['id'] == authorization['id']
    execute_authorization_command(runtime, '/revoke ' + authorization['id'])
    result = invoke(runtime, 'editor', {'operation': 'create', 'path': 'reports/after.md', 'content': 'x'}, 'revoked')
    assert result['pending']
    other = Session(runtime.store.root, workspace=runtime.store.workspace)
    assert other.record.authorizations == []


def test_new_scope_can_cover_prepared_call_without_stale_one_shot(runtime):
    args = {'operation': 'create', 'path': 'covered.md', 'content': 'value'}
    assert invoke(runtime, 'editor', args, 'prepared')['pending']
    grant(runtime, scope='session', tools=['editor'], paths=[str(runtime.store.workspace)])
    assert invoke(runtime, 'editor', args, 'prepared')['ok']


def test_task_grant_expires_on_task_transition_not_wall_clock(runtime, monkeypatch):
    first = runtime.tasks.begin_task('first', run_id='run_first')
    authorization = grant(runtime, scope='task', tools=['editor'], paths=[str(runtime.store.workspace)])
    assert authorization['task_id'] == first['id']
    clock = time.time()
    monkeypatch.setattr(time, 'time', lambda: clock + 7 * 86400)
    assert runtime.authorizations.list()[0]['active']
    runtime.tasks.begin_task('replacement', run_id='run_second', new_task=True)
    assert not runtime.authorizations.list()[0]['active']
    assert invoke(runtime, 'editor', {'operation': 'create', 'path': 'x.md', 'content': 'x'}, 'new_task')['pending']


def test_one_shot_has_no_expiry_but_still_binds_resources(monkeypatch):
    policy = PermissionPolicy('user')
    preview = {'target': '/reviewed/source', 'sha256': 'first'}
    approved = policy.freeze(preview)
    prior = {'digest': approved}
    monkeypatch.setattr(time, 'time', lambda: 999999999999)
    policy.validate(preview, prior, approved)
    with pytest.raises(ToolError, match='资源'):
        policy.validate({**preview, 'sha256': 'changed'}, prior, approved)


def test_strong_registration_and_deletion_never_use_range_grant(runtime):
    grant(runtime, scope='session', tools=['register_capability', 'editor'], paths=[str(runtime.store.workspace)])
    assert runtime.policy.requires_confirmation(runtime.registry.get('register_capability'), {}, {})
    assert runtime.policy.requires_confirmation(runtime.registry.get('editor'), {'operation': 'delete'}, {})
    runtime.set_permission('auto')
    assert runtime.policy.requires_confirmation(runtime.registry.get('editor'), {'operation': 'delete'}, {})


def test_domain_match_is_exact_or_explicit_subdomain(runtime):
    grant(runtime, scope='session', tools=['fetch_web_content'], domains=['Example.COM', '*.example.org'])
    descriptor = runtime.registry.get('fetch_web_content')
    assert runtime.policy.authorization_for(descriptor, {'url': 'https://example.com/a'}, {})
    assert runtime.policy.authorization_for(descriptor, {'url': 'https://a.example.org/a'}, {})
    for url in ('https://example.com.attacker.test', 'https://example.org', 'https://unapproved.test'):
        assert runtime.policy.requires_confirmation(descriptor, {'url': url}, {})
    assert runtime.policy.requires_confirmation(descriptor, {'query': 'arbitrary search'}, {})


def test_shell_scope_requires_program_prefix_and_explicit_environment(runtime, monkeypatch):
    monkeypatch.setenv('AUDIT_BUILD_SECRET', 'fixture-secret-90b317')
    with pytest.raises(ToolError, match='command_prefixes'):
        grant(runtime, scope='session', tools=['run_commands'], paths=[str(runtime.store.workspace)])
    code = "import os,json; from pathlib import Path; Path('environment.json').write_text(json.dumps({'mapped':os.environ.get('BUILD_CREDENTIAL'),'hidden':os.environ.get('AUDIT_BUILD_SECRET')})); print('done')"
    authorization = grant(runtime, scope='session', tools=['run_commands'], paths=[str(runtime.store.workspace)],
        command_prefixes=[[sys.executable, '-c', code]], env_from={'BUILD_CREDENTIAL': 'AUDIT_BUILD_SECRET'})
    args = {'command': [sys.executable, '-c', code], 'env_from': {'BUILD_CREDENTIAL': 'AUDIT_BUILD_SECRET'}}
    result = invoke(runtime, 'run_commands', args, 'mapped_command')
    assert result['ok'], result
    if result['data']['status'] in {'queued', 'running'}:
        assert runtime.jobs.wait(result['data']['job_id'], 10)['status'] == 'succeeded'
    value = json.loads((runtime.store.workspace / 'environment.json').read_text())
    assert value == {'mapped': 'fixture-secret-90b317', 'hidden': None}
    jobs = runtime.jobs.status()['jobs']
    assert jobs[0]['authorization_id'] == authorization['id']
    changed = {**args, 'command': [sys.executable, '-c', "print('different command')"]}
    assert invoke(runtime, 'run_commands', changed, 'outside_command')['pending']
    changed = {**args, 'env_from': {'ANOTHER_TARGET': 'AUDIT_BUILD_SECRET'}}
    assert invoke(runtime, 'run_commands', changed, 'outside_environment')['pending']


def test_revoked_scope_is_rechecked_when_real_job_leaves_queue(runtime):
    code = "from pathlib import Path; Path('forbidden.txt').write_text('must not run')"
    authorization = grant(runtime, scope='session', tools=['run_commands'], paths=[str(runtime.store.workspace)],
        command_prefixes=[[sys.executable, '-c', code]])
    runtime.jobs.settings.parent.mkdir(parents=True, exist_ok=True)
    runtime.jobs.settings.write_text(json.dumps({'max_concurrent': 1}), encoding='utf-8')
    pool = runtime.store.root / 'jobs' / 'slots'
    pool.mkdir()
    slot = Slot(pool / '0.lock')
    token = current_authorization.set(authorization)
    try:
        identity = runtime.jobs.start([sys.executable, '-c', code], runtime.store.workspace)['job_id']
        assert runtime.jobs.wait(identity, .2)['status'] == 'queued'
        # Simulate another authorized UI revoking the grant; worker must check
        # persisted authorization even without the CLI's additional stop marker.
        runtime.authorizations.revoke(authorization['id'])
    finally:
        current_authorization.reset(token)
        slot.close()
    status = runtime.jobs.wait(identity, 10)
    assert status['status'] == 'failed' and status['code'] == 'authorization_revoked', status
    assert not (runtime.store.workspace / 'forbidden.txt').exists()


def test_explicit_constraints_keep_source_scope_and_revocation(runtime):
    runtime.tasks.begin_task('implement', run_id='run_task')
    with pytest.raises(ToolError, match='constraints'):
        execute_authorization_command(runtime, '/constraints {"scope":"turn","read_only":true}')
    result = execute_authorization_command(runtime, '/constraints {"read_only":true,"network_forbidden":true}')
    assert result['constraints'] == {'read_only': True, 'network_forbidden': True}
    result = execute_authorization_command(runtime, '/constraints {"read_only":false}')
    assert result['constraints'] == {'read_only': False, 'network_forbidden': True}
    task = runtime.tasks.active_task()
    assert task['instructions'][-1]['source'] == 'cli'
    assert any(item['key'] == 'read_only' and item['status'] == 'revoked' for item in task['constraints'])
    assert all(item['source_id'] for item in task['constraints'])
    runtime.tasks.begin_task('unrelated', run_id='run_new', new_task=True)
    assert runtime.tasks.effective_constraints(runtime.tasks.active_task(), 'run_new') == {'read_only': False, 'network_forbidden': False}


def test_real_child_defaults_are_allowlisted_not_name_substring_filtered(monkeypatch):
    monkeypatch.setenv('MONKEY_BUILD', 'legitimate-setting')
    monkeypatch.setenv('UNCLASSIFIED_PRIVATE_VALUE', 'fixture-private')
    monkeypatch.setenv('AUDIT_BUILD_TOKEN', 'fixture-token')
    env = safe_environment({'MONKEY_BUILD': 'MONKEY_BUILD', 'BUILD_TOKEN': 'AUDIT_BUILD_TOKEN'})
    result = subprocess.run([sys.executable, '-c',
        "import os,json;print(json.dumps({k:os.environ.get(k) for k in ['MONKEY_BUILD','BUILD_TOKEN','UNCLASSIFIED_PRIVATE_VALUE','AUDIT_BUILD_TOKEN']}))"],
        env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'MONKEY_BUILD': 'legitimate-setting', 'BUILD_TOKEN': 'fixture-token',
                                        'UNCLASSIFIED_PRIVATE_VALUE': None, 'AUDIT_BUILD_TOKEN': None}
    with pytest.raises(ToolError, match='未配置'):
        safe_environment({'VALUE': 'AUDIT_MISSING_VARIABLE_6789'})
    with pytest.raises(ToolError, match='env_from'):
        safe_environment({'GENESISAI_RESULT_PATH': 'AUDIT_BUILD_TOKEN'})


def test_tool_definition_environment_mapping_matches_test_and_registered_execution(runtime, monkeypatch):
    from test_capability_lifecycle import tool_package, created, reference, accept
    secret = 'fixture-env-390b7a'
    monkeypatch.setenv('AUDIT_TOOL_SOURCE', secret)
    monkeypatch.setenv('UNLISTED_PRIVATE', 'fixture-hidden')
    runtime.set_permission('auto')
    package = tool_package('mapped_environment_tool')
    package['definition']['env_from'] = {'BUILD_KEY': 'AUDIT_TOOL_SOURCE'}
    package['files']['tool.py'] = (
        "import json,os,sys\nfrom pathlib import Path\njson.load(sys.stdin)\n"
        "values={'mapped':os.environ.get('BUILD_KEY'),'original':os.environ.get('AUDIT_TOOL_SOURCE'),"
        "'private':os.environ.get('UNLISTED_PRIVATE')}\n"
        "Path('observed_environment.json').write_text(json.dumps(values))\n"
        "Path(os.environ['GENESISAI_RESULT_PATH']).write_text(json.dumps({'value':len(values['mapped'] or ''),'cwd':os.getcwd()}),encoding='utf-8')\n")
    package['tests'][0]['expect']['properties']['value']['const'] = len(secret)
    item = created(runtime, package)
    tested = invoke(runtime, 'test_capability', reference(item), 'test_mapping')
    assert tested['ok'], tested
    assert tested['data']['report']['passed'], tested
    accept(runtime, item)
    runtime.set_permission('user')
    grant(runtime, scope='session', tools=['mapped_environment_tool'], paths=[str(runtime.store.workspace)],
          env_from={'BUILD_KEY': 'AUDIT_TOOL_SOURCE'})
    result = invoke(runtime, 'mapped_environment_tool', {'value': 3}, 'run_mapping')
    assert result['ok'], result
    assert result['data']['value']['value'] == len(secret)
    observed = json.loads((runtime.store.workspace / 'observed_environment.json').read_text())
    assert observed == {'mapped': secret, 'original': None, 'private': None}
    for path in (runtime.store.root / 'jobs').rglob('*.json'):
        assert secret not in path.read_text(encoding='utf-8'), path


@pytest.mark.parametrize('mode', ['user', 'auto'])
def test_private_network_requires_explicit_domain_and_address_grant(tmp_path, mode):
    from types import SimpleNamespace
    from genesisai.core.tools.authorization import AuthorizationService
    from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
    store = SimpleNamespace(id='session_scope', workspace=tmp_path,
        record=SimpleNamespace(authorizations=[]), commit=lambda: None)
    authorizations = AuthorizationService(store)
    descriptor = ToolDescriptor('fetch_private_resource', True, 'kernel', 'private', tmp_path,
        ToolSpec(parameters=object_schema([]), permission='network', network=True, network_boundary='private'))
    policy = PermissionPolicy(mode, authorizations)
    args = {'url': 'https://api.corp.test/data', 'address_ranges': ['10.20.30.40']}
    assert policy.requires_confirmation(descriptor, args, {})
    item = authorizations.grant({'scope': 'session', 'tools': ['fetch_private_resource'],
        'domains': ['api.corp.test', 'fd00:0:0:0:0:0:0:1'], 'address_ranges': ['10.20.0.0/16', 'fd00::/8']}, source='cli')
    assert item['domains'][1] == 'fd00::1'
    assert policy.authorization_for(descriptor, args, {})['id'] == item['id']
    assert not policy.requires_confirmation(descriptor, args, {})
    assert not policy.requires_confirmation(descriptor,
        {'url': 'https://[fd00::1]/data', 'address_ranges': ['fd00::1/128']}, {})
    for invalid in (
        {**args, 'address_ranges': []}, {**args, 'address_ranges': ['10.0.0.0/8']},
        {**args, 'address_ranges': ['192.168.1.1']},
        {**args, 'address_ranges': ['10.20.30.40', '192.168.1.1']},
        {**args, 'url': 'https://other.corp.test/data'},
    ):
        assert policy.requires_confirmation(descriptor, invalid, {}), invalid
    authorizations.revoke(item['id'])
    assert policy.requires_confirmation(descriptor, args, {})


def test_network_boundary_requires_a_network_tool():
    from genesisai.core.tools.base import ToolSpec, SchemaConfigError, object_schema
    with pytest.raises(SchemaConfigError, match='network_boundary'):
        ToolSpec(parameters=object_schema([]), permission='read', network_boundary='private').validate()


def test_running_job_observes_task_grant_expiry_without_cli_stop(runtime):
    runtime.tasks.begin_task('first', run_id='run_first')
    code = "import time\nfrom pathlib import Path\nPath('started').write_text('running')\nwhile True:\n time.sleep(.1)\n"
    grant(runtime, scope='task', tools=['run_commands'], paths=[str(runtime.store.workspace)],
          command_prefixes=[[sys.executable, '-c', code]])
    result = invoke(runtime, 'run_commands', {'command': [sys.executable, '-c', code], 'background': True}, 'long_task')
    assert result['ok'], result
    identity = result['data']['job_id']
    deadline = time.monotonic() + 10
    while not (runtime.store.workspace / 'started').exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert (runtime.store.workspace / 'started').exists()
    runtime.tasks.begin_task('replacement', run_id='run_second', new_task=True)
    status = runtime.jobs.wait(identity, 10)
    assert status['status'] == 'stopped' and status['code'] == 'authorization_revoked', status


@pytest.mark.parametrize('operation', ['external_tool', 'builtin_tool'])
def test_authorization_cancel_preserves_unknown_side_effect(tmp_path, monkeypatch, operation):
    from types import SimpleNamespace
    import threading
    from genesisai.core.jobs.worker import run_operation
    from genesisai.core.jobs import operations
    calls = []
    def execute(*args):
        calls.append(args)
        time.sleep(.2)
        raise ToolError('unknown_execution', 'effect outcome remains unknown')
    monkeypatch.setattr(operations, 'execute', execute)
    signal = threading.Event()
    signal.set()
    authorization = SimpleNamespace(cancelled=signal,
        failure={'code': 'authorization_revoked', 'error': 'revoked'})
    result = run_operation({'operation': operation, 'data': {}, 'limits': {}}, tmp_path,
        {'timeout_seconds': None}, lambda **kwargs: None, lambda value: value, authorization=authorization)
    assert result['status'] == 'unknown' and result['code'] == 'unknown_execution', result
    assert len(calls) == 1










def test_queued_job_observes_revocation_before_a_slot_is_free(runtime):
    code = "from pathlib import Path; Path('must_not_start').write_text('unexpected')"
    authorization = grant(runtime, scope='session', tools=['run_commands'], paths=[str(runtime.store.workspace)],
        command_prefixes=[[sys.executable, '-c', code]])
    runtime.jobs.settings.parent.mkdir(parents=True, exist_ok=True)
    runtime.jobs.settings.write_text(json.dumps({'max_concurrent': 1}), encoding='utf-8')
    pool = runtime.store.root / 'jobs' / 'slots'
    pool.mkdir()
    slot = Slot(pool / '0.lock')
    token = current_authorization.set(authorization)
    try:
        identity = runtime.jobs.start([sys.executable, '-c', code], runtime.store.workspace)['job_id']
        assert runtime.jobs.wait(identity, .2)['status'] == 'queued'
        runtime.authorizations.revoke(authorization['id'])
        status = runtime.jobs.wait(identity, 10)
        assert status['status'] == 'failed' and status['code'] == 'authorization_revoked', status
        assert not (runtime.store.workspace / 'must_not_start').exists()
    finally:
        current_authorization.reset(token)
        slot.close()
