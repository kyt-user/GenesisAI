"""Resumable frozen suites with an exclusive owner and explicit uncertain-case decisions."""
import re
import time

from genesisai.core.capabilities.contracts import schema_validator
from genesisai.core.jobs.storage import Slot, SlotBusy, load, save
from genesisai.shared.ids import uid
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline


def suite_root(service, report_id):
    if not isinstance(report_id, str) or not re.fullmatch(r'test_[a-f0-9]{16}', report_id):
        raise ToolError('invalid_report', '测试报告标识无效')
    root = service.store.root / 'tests' / report_id
    if root.is_symlink() or root.resolve() != root.absolute():
        raise ToolError('invalid_report', '测试报告目录不可重定向')
    return root


def validate_checkpoint(checkpoint, item):
    if any(checkpoint.get(key) != item[key] for key in ('id', 'revision', 'digest')):
        raise ToolError('revision_conflict', '续跑必须使用同一草稿的冻结版本与摘要')
    names = [case['name'] for case in item['package']['tests']]
    cases = checkpoint.get('cases', [])
    if [case['name'] for case in cases] != names[:len(cases)] or len(cases) > len(names):
        raise ToolError('invalid_report', '测试检查点的用例顺序无效')
    running = checkpoint.get('running')
    if running and (len(cases) == len(names) or running['name'] != names[len(cases)]):
        raise ToolError('invalid_report', '测试检查点的执行中用例无效')


def run_suite(service, args, *, progress_path=None):
    item = service.store.get(args['id'])
    service._check_revision(item, args)
    if not item['frozen']:
        raise ToolError('capability_unfrozen', '草稿尚未冻结，请先使用 freeze_capability')
    package = item['package']
    service._check_constraints(package)
    bundle = service.store.materialize(item)
    resuming = bool(args.get('resume_report_id'))
    if args.get('reconcile') and not resuming:
        raise ToolError('invalid_report', 'reconcile 必须与 resume_report_id 一起使用')
    report_id = args.get('resume_report_id') or uid('test')
    root = suite_root(service, report_id)
    if resuming and not (root / 'suite.json').is_file():
        raise ToolError('unknown_report', '不存在可续跑的测试检查点')
    root.mkdir(parents=True, exist_ok=resuming)
    try:
        lease = Slot(root / 'suite.lock')
    except SlotBusy as exc:
        raise ToolError('suite_busy', '该测试套件仍在运行；请读取原作业，不启动第二个执行器') from exc
    try:
        checkpoint = load(root / 'suite.json') if resuming else {
            'report_id': report_id, 'id': item['id'], 'revision': item['revision'], 'digest': item['digest'],
            'started': time.time(), 'total': len(package['tests']), 'completed': 0,
            'cases': [], 'running': None, 'reconciliations': [], 'attempt': 0}
        validate_checkpoint(checkpoint, item)
        def publish():
            checkpoint['completed'] = len(checkpoint['cases'])
            save(root / 'suite.json', checkpoint)
            if progress_path:
                save(progress_path, checkpoint)
        publish()
        running, decision = checkpoint['running'], args.get('reconcile')
        if running:
            if not decision or decision['case'] != running['name']:
                raise ToolError('reconciliation_required', '用例结果不明：' + running['name'] + '；显式 reconcile={case, action:retry|mark_failed} 后续跑')
            if decision['action'] not in {'retry', 'mark_failed'}:
                raise ToolError('invalid_report', '未知的用例核对决定')
            checkpoint['reconciliations'].append({**decision, 'previous': running, 'at': time.time()})
            if decision['action'] == 'mark_failed':
                checkpoint['cases'].append({**running, 'passed': False, 'code': 'execution_unknown',
                                            'error': '明确将结果不明的执行记为失败；未重跑'})
            checkpoint['running'] = None
            publish()
        elif decision:
            raise ToolError('invalid_report', '当前没有结果不明的用例可核对')
        for number in range(len(checkpoint['cases']), len(package['tests'])):
            if cancellation_requested():
                raise ToolError('user_cancelled', '测试已停止，可按报告标识续跑')
            if tool_deadline.get() and time.monotonic() >= tool_deadline.get():
                raise ToolError('timeout', '测试总时限已用完，可按报告标识续跑')
            case = package['tests'][number]
            checkpoint['attempt'] += 1
            workspace = root / (str(number) + '-' + str(checkpoint['attempt'])) / 'workspace'
            # Publish intent before any case-specific effects, including fixtures.
            record = {'name': case['name'], 'workspace': str(workspace), 'attempt': checkpoint['attempt'], 'passed': False}
            checkpoint['running'] = record
            publish()
            workspace.mkdir(parents=True)
            service.store.assets.materialize(case.get('fixtures', {}), workspace)
            try:
                result = service._trial(item, bundle, case['input'], workspace)
                record['result'] = result
                if 'expect_error' in case:
                    record['error'] = '预期错误未发生：' + case['expect_error']
                else:
                    from genesisai.core.capabilities.tool_results import value
                    checked = value(service, result) if package['kind'] == 'tool' else result
                    error = next(schema_validator(case['expect']).iter_errors(checked), None)
                    record['passed'] = error is None
                    if error:
                        record['error'] = f'{error.json_path}: {error.message}'[:2000]
            except Exception as exc:
                code = getattr(exc, 'error_type', 'trial_error')
                if code in {'user_cancelled', 'worker_cancelled', 'command_cancelled', 'unknown_execution', 'execution_unknown'}:
                    raise
                record.update(error=str(exc)[:2000], code=code, passed=case.get('expect_error') == code)
            checkpoint['cases'].append(record)
            checkpoint['running'] = None
            publish()
        service.store.materialize(item)
        if 'finished' not in checkpoint:
            checkpoint['finished'] = time.time()
            publish()
        report = {'id': report_id, 'revision': item['revision'], 'digest': item['digest'],
                  'passed': len(checkpoint['cases']) == len(package['tests']) and all(case['passed'] for case in checkpoint['cases']),
                  'cases': checkpoint['cases'], 'started': checkpoint['started'], 'finished': checkpoint['finished'],
                  'directory': str(root), 'reconciliations': checkpoint['reconciliations']}
        service.store.record_report(item['id'], item['revision'], item['digest'], report)
        return {**service.summary(service.store.get(item['id'])), 'verification_current': service.verification_current(item)}
    finally:
        lease.close()
