"""Read batches must actually overlap isolated workers, without sharing session writes."""
import json
from pathlib import Path
import time

from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from test_file_tool_jobs import runtime


def test_read_batch_starts_both_jobs_before_waiting_for_results(runtime, monkeypatch):
    from genesisai.core.jobs import tool_operation
    from genesisai.shared.security import ToolError
    monkeypatch.setattr(tool_operation, 'FOREGROUND_SECONDS', 30)
    directory = runtime.store.workspace / 'coordination'
    directory.mkdir()

    class CoordinatedRead:
        def prepare(self, context, args):
            return {'path': str(context.access.workspace_path('coordination', allow_directory=True)),
                    'index': args['index']}

        def execute(self, context, args):
            from genesisai.shared.run_control import cancellation_requested
            directory = context.access.workspace_path('coordination', allow_directory=True)
            # These files are only the test's process barrier, not product output.
            (directory / ('ready_' + str(args['index']))).write_text('ready', encoding='utf-8')
            deadline = time.monotonic() + 10
            while not (directory / 'release').exists():
                if cancellation_requested():
                    raise ToolError('cancelled', 'fixture cancelled')
                if time.monotonic() >= deadline:
                    raise ToolError('fixture_serialized', 'the host waited before submitting the peer')
                time.sleep(.02)
            return {'index': args['index'], 'both_started': all((directory / ('ready_' + str(i))).exists() for i in (1, 2))}

    spec = ToolSpec(permission='read', path_scope='workspace', execution='job',
        parameters=object_schema(['index'], index={'type': 'integer', 'minimum': 1, 'maximum': 2}))
    runtime.register_tool(ToolDescriptor('coordinated_read', True, 'office', 'fixture', Path('.'), spec), CoordinatedRead())
    calls = [{'id': 'parallel_' + str(i), 'name': 'coordinated_read', 'arguments': json.dumps({'index': i})} for i in (1, 2)]
    jobs = []
    try:
        for call in calls:
            with runtime.submission_batch(calls):
                result = runtime.execute(call)
            assert result['ok'] and result['data'].get('job_id'), result
            jobs.append(result['data']['job_id'])
        deadline = time.monotonic() + 10
        while not all((directory / ('ready_' + str(i))).exists() for i in (1, 2)):
            assert time.monotonic() < deadline, [runtime.jobs.status(identity) for identity in jobs]
            time.sleep(.02)
        (directory / 'release').write_text('go', encoding='utf-8')
        for index, identity in enumerate(jobs, 1):
            assert runtime.jobs.wait(identity, 15)['status'] == 'succeeded'
            result = runtime.execute({'id': 'collect_' + str(index), 'name': 'read_job',
                'arguments': json.dumps({'job_id': identity})})
            assert result['ok'], result
            assert result['data']['result'] == {'index': index, 'both_started': True}
        assert not runtime.store.record.run_runtime.get('pending_execution_jobs')
    finally:
        (directory / 'release').write_text('finish', encoding='utf-8')