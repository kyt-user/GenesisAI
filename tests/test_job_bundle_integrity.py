"""Recheck frozen execution bytes after a durable job leaves its queue."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from genesisai.core.capabilities.assets import file_identity, verify_bundle
from genesisai.core.jobs.storage import Slot
from genesisai.shared.security import ToolError
from test_capability_lifecycle import capability_runtime, created, tool_package, invoke, accept, reference


def test_queued_external_tool_does_not_execute_replaced_bundle(capability_runtime):
    runtime = capability_runtime
    item = created(runtime, tool_package('queued_integrity'))
    _, tested = invoke(runtime, 'test_capability', reference(item))
    assert tested['ok'] and tested['data']['report']['passed'], tested
    accept(runtime, item)
    bundle = runtime.capabilities.store.materialize(runtime.capabilities.store.get(item['id']))
    runtime.jobs.settings.write_text(json.dumps({'max_concurrent': 1}), encoding='utf-8')
    slot = Slot(runtime.store.root / 'jobs' / 'slots' / '0.lock')
    identity = None
    try:
        _, submitted = invoke(runtime, 'queued_integrity', {'value': 8})
        assert submitted['ok'], submitted
        identity = submitted['data']['job_id']
        assert runtime.jobs.status(identity)['status'] == 'queued'
        (bundle / 'tool.py').write_text(
            "import json, os\nfrom pathlib import Path\n"
            "Path('unreviewed.txt').write_text('new bytes ran')\n"
            "print(json.dumps({'value': 999, 'cwd': os.getcwd()}))\n", encoding='utf-8')
    finally:
        slot.close()
    try:
        state = runtime.jobs.wait(identity, 10)
        assert state['status'] == 'failed' and state['code'] == 'capability_integrity', state
        with pytest.raises(ToolError) as error:
            runtime.jobs.operation_result(identity)
        assert error.value.error_type == 'capability_integrity'
        assert not (runtime.store.workspace / 'unreviewed.txt').exists()
        assert not (runtime.store.workspace / 'output.txt').exists()
        assert len(runtime.jobs.status()['jobs']) == 2  # One test suite and one submitted invocation.
    finally:
        if identity:
            runtime.jobs.stop(identity)


@pytest.mark.parametrize('mutation', ['add', 'remove', 'change'])
def test_frozen_bundle_verifies_full_file_set_and_contents(tmp_path, mutation):
    root = tmp_path / 'bundle'
    root.mkdir()
    entry = root / 'entry.py'
    entry.write_text('original', encoding='utf-8')
    manifest = {'entry.py': file_identity(entry)}
    verify_bundle(root, manifest)
    if mutation == 'add':
        (root / 'extra.py').write_text('extra', encoding='utf-8')
    elif mutation == 'remove':
        entry.unlink()  # This test's own generated fixture only.
    else:
        entry.write_text('modified', encoding='utf-8')
    with pytest.raises(ToolError) as error:
        verify_bundle(root, manifest)
    assert error.value.error_type == 'capability_integrity'


def test_bundle_rejects_linked_ancestor(tmp_path):
    target = tmp_path / 'original'
    target.mkdir()
    root = target / 'bundle'
    root.mkdir()
    entry = root / 'entry.py'
    entry.write_text('original', encoding='utf-8')
    manifest = {'entry.py': file_identity(entry)}
    link = tmp_path / 'linked'
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ToolError) as error:
        verify_bundle(link / 'bundle', manifest)
    assert error.value.error_type == 'capability_integrity'
