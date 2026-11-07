import json
import sys

from genesisai.agent.loop import Runner
from genesisai.core.jobs.service import JobService


def test_directory_pages_reach_beyond_old_total_cap_and_explicit_dependencies(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    for index in range(1105):
        (root / f"file_{index:04d}.txt").touch()
    dependency = root / "node_modules"
    dependency.mkdir()
    (dependency / "dependency.custom").write_text("dependency", encoding="utf-8")
    result = call(runtime, "list_files", {"path": ".", "offset": 1000, "limit": 200}, "tail")["data"]
    assert any(item["path"].endswith("file_1104.txt") for item in result["files"])
    assert not any("node_modules" in item["path"] for item in result["files"])
    included = call(runtime, "list_files", {"path": "node_modules"}, "include")["data"]
    assert included["files"][0]["path"].endswith("dependency.custom")


def test_large_unknown_suffix_text_is_range_readable(tmp_path):
    from genesisai.shared.filetypes import detect_file_type
    root, _, _, _, runtime = development_env(tmp_path)
    path = root / "generated.customlang"
    with path.open("w", encoding="utf-8", newline="") as stream:
        for _ in range(10):
            stream.write("x" * 1024 * 1024)
        stream.write("\nTAIL_MARKER\n")
    assert detect_file_type(path) == "text"
    result = call(runtime, "read_files", {"path": path.name, "offset": 10 * 1024 * 1024, "limit": 100}, "large")["data"]
    assert result["files"][0]["text"] == "\nTAIL_MARKER\n"
    assert result["files"][0]["next_offset"] is None
    lines = call(runtime, "read_files", {"path": path.name, "line_start": 2, "line_end": 2}, "line")["data"]
    assert lines["files"][0]["text"] == "2: TAIL_MARKER"


def test_full_document_extraction_keeps_tail_and_all_sheets(tmp_path):
    import pytest
    docx = pytest.importorskip("docx")
    xlsx = pytest.importorskip("openpyxl")
    from genesisai.core.extensions.tools.filesystem.shared import extract
    doc = docx.Document()
    for _ in range(3100):
        doc.add_paragraph("x" * 70)
    doc.add_paragraph("DOCX_TAIL_MARKER")
    path = tmp_path / "large.docx"
    doc.save(path)
    text, truncated = extract(path)
    assert len(text) > 200000 and text.rstrip().endswith("DOCX_TAIL_MARKER") and not truncated
    book = xlsx.Workbook()
    for index in range(11):
        book.create_sheet(f"Sheet{index}")["A1002"] = "XLSX_TAIL_MARKER"
    path = tmp_path / "large.xlsx"
    book.save(path)
    text, truncated = extract(path)
    assert text.count("XLSX_TAIL_MARKER") == 11 and "## Sheet10" in text and not truncated
from genesisai.shared.messages import Response, ToolCall
from test_development_continuation import development_env
from test_p11_agent_runtime import FakeModel


def call(runtime, name, args, ident):
    result = runtime.execute({'id': ident, 'name': name, 'arguments': json.dumps(args)})
    assert result['ok'], result
    return result


def test_index_pages_cover_more_than_100_files_and_detect_changes(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    for i in range(115):
        (root / f'module_{i:03}.py').write_text(f'def f{i}():\n    pass\n', encoding='utf-8')
    args = {'path': '.', 'mode': 'index', 'limit': 40}
    pages, names = [], []
    for i in range(4):
        data = call(runtime, 'search_codebase', args, f'page{i}')['data']
        names.extend(item['file'] for item in data['files'])
        pages.append(data)
        if data['next_cursor'] is None:
            break
        args['cursor'] = data['next_cursor']
    assert len(names) == len(set(names)) == 115
    assert len(pages) == 3 and pages[-1]['total_files'] == 115
    (root / 'module_new.py').write_text('x = 1', encoding='utf-8')
    stale = runtime.execute({'id': 'stale', 'name': 'search_codebase', 'arguments': json.dumps({**args, 'cursor': pages[0]['next_cursor']})})
    assert stale['error']['code'] == 'stale_cursor'


def test_index_exclusions_and_large_file_coverage(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    (root / 'node_modules').mkdir()
    (root / 'node_modules' / 'hidden.js').write_text('export const ignored = 1;', encoding='utf-8')
    (root / 'big.py').write_text('# filler\n' * 25000 + 'def tail(): pass', encoding='utf-8')
    data = call(runtime, 'search_codebase', {'path': '.', 'mode': 'index'}, 'index')['data']
    assert data['total_files'] == 1
    assert not data['files'][0]['content_truncated'] and not data['partial_files']
    assert data['files'][0]['functions'][0]['name'] == 'tail'


def test_large_file_search_and_symbol_pages_reach_tail(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    (root / 'large.custom').write_text('x' * (9 * 1024 * 1024) + '\nUNIQUE_TAIL\n', encoding='utf-8')
    data = call(runtime, 'search_codebase', {'path': 'large.custom', 'query': 'UNIQUE_TAIL'}, 'search_tail')['data']
    assert data['matches'][0]['line'] == 2
    assert data['matches'][0]['read_request']['offset'] > 8 * 1024 * 1024
    (root / 'symbols.py').write_text(''.join(f'def function_{i}(): pass\n# TODO: task_{i}\n' for i in range(80)), encoding='utf-8')
    args = {'path': 'symbols.py', 'mode': 'index', 'max_output_chars': 2000}
    names, todos, cursors = [], [], set()
    for page in range(30):
        data = call(runtime, 'search_codebase', args, f'symbol_page_{page}')['data']
        names.extend(item['name'] for item in data['files'][0]['functions'])
        todos.extend(item['note'] for item in data['files'][0]['todos'])
        cursor = data['next_cursor']
        if cursor is None:
            break
        assert cursor not in cursors
        cursors.add(cursor)
        args['cursor'] = cursor
    assert names == [f'function_{i}' for i in range(80)]
    assert todos == [f'task_{i}' for i in range(80)]


def test_python_index_reuses_ast_without_string_false_positives(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    (root / 'module.py').write_text('"""\ndef fake(): pass\n"""\nfrom . import sibling\nasync def actual(\n    x: int,\n):\n    return x\n', encoding='utf-8')
    data = call(runtime, 'search_codebase', {'path': '.', 'mode': 'index'}, 'ast')['data']
    assert [f['name'] for f in data['files'][0]['functions']] == ['actual']
    assert data['files'][0]['coverage'] == 'python_ast'
    assert data['files'][0]['imports'] == [{'module': '.'}]
    assert runtime.project_index.refresh()['updated'] == 1
    assert runtime.project_index.refresh()['updated'] == 0


def test_streaming_editor_large_file_reference_and_conflicts(tmp_path):
    from genesisai.shared.ids import sha
    root, _, _, store, runtime = development_env(tmp_path)
    path = root / 'large.custom'
    prefix = b'x' * (9 * 1024 * 1024)
    path.write_bytes(prefix + b'\r\nold tail\r\n')
    replacement = root / 'replacement.txt'
    replacement.write_text('new tail', encoding='utf-8')
    result = call(runtime, 'editor', {'operation': 'replace', 'path': str(path), 'old_text': 'old tail',
        'new_text_ref': {'path': str(replacement), 'sha256': sha(replacement)}, 'expected_sha256': sha(path)}, 'large_replace')
    assert path.read_bytes() == prefix + b'\r\nnew tail\r\n'
    patch = root / 'change.diff'
    patch.write_text('@@ -2 +2 @@\n-new tail\n+patched tail\n', encoding='utf-8')
    call(runtime, 'editor', {'operation': 'patch', 'path': str(path), 'patch_ref': {'path': str(patch), 'sha256': sha(patch)}}, 'large_patch')
    assert path.read_bytes() == prefix + b'\r\npatched tail\r\n'
    assert result['data']['before_sha256'] != result['data']['after_sha256']
    assert len(store.record.changes) == 2


def test_incremental_index_updates_only_changed_module(tmp_path, record_testsuite_property):
    import time
    root, _, _, store, runtime = development_env(tmp_path)
    for i in range(130):
        (root / f'module_{i:03}.py').write_text(f'from module_{max(0, i-1):03} import value\nvalue = {i}\n', encoding='utf-8')
    started = time.perf_counter()
    assert runtime.project_index.refresh()['updated'] == 130
    first_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    assert runtime.project_index.refresh()['updated'] == 0
    unchanged_ms = (time.perf_counter() - started) * 1000
    (root / 'module_065.py').write_text('value = 999\n', encoding='utf-8')
    started = time.perf_counter()
    assert runtime.project_index.refresh(changed_paths=['module_065.py'])['updated'] == 1
    changed_ms = (time.perf_counter() - started) * 1000
    record_testsuite_property('routing_index_130_modules_ms', json.dumps({'initial': round(first_ms, 1), 'unchanged': round(unchanged_ms, 1), 'one_changed': round(changed_ms, 1)}))


def test_search_modes_and_output_budget_preserve_pagination(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    (root / 'a.py').write_text('\n'.join(f'needle_{i} = {i}' for i in range(30)), encoding='utf-8')
    (root / 'b.py').write_text('needle = 1\n', encoding='utf-8')
    files = call(runtime, 'search_codebase', {'path': '.', 'query': 'needle', 'output_mode': 'files'}, 'files')['data']
    assert len(files['matches']) == 2
    counts = call(runtime, 'search_codebase', {'path': '.', 'query': 'needle', 'output_mode': 'count'}, 'counts')['data']
    assert sum(m['matching_lines'] for m in counts['matches']) == 31
    args = {'path': 'a.py', 'query': 'needle', 'context_lines': 40, 'max_output_chars': 1000}
    first = call(runtime, 'search_codebase', args, 'first')['data']
    second = call(runtime, 'search_codebase', {**args, 'offset': first['next_offset']}, 'second')['data']
    assert second['matches'][0]['line'] == first['matches'][-1]['line'] + 1


def test_read_independent_ranges_and_continue_total_budget(tmp_path):
    root, _, _, _, runtime = development_env(tmp_path)
    (root / 'a.py').write_text('one\ntwo\nthree\n', encoding='utf-8')
    (root / 'b.py').write_text('four\nfive\nsix\n', encoding='utf-8')
    data = call(runtime, 'read_files', {'files': [{'path': 'a.py', 'line_start': 2, 'line_end': 2}, {'path': 'b.py', 'line_start': 3, 'line_end': 3}]}, 'ranges')['data']
    assert [item['text'] for item in data['files']] == ['2: two', '3: six']
    (root / 'long.txt').write_text('A' * 250000 + 'TAIL', encoding='utf-8')
    data = call(runtime, 'read_files', {'files': [{'path': 'long.txt', 'offset': 249500, 'limit': 2000}, {'path': 'a.py'}], 'max_output_chars': 1000}, 'read')['data']
    assert 'TAIL' in data['files'][0]['text']
    assert sum(len(f['text']) for f in data['files']) <= 1000
    data = call(runtime, 'read_files', {'files': [{'path': 'long.txt', 'line_start': 1, 'line_end': 1}], 'max_output_chars': 1000}, 'longline')['data']
    assert data['remaining_requests'][0]['offset'] == 0
    resumed = call(runtime, 'read_files', {'files': data['remaining_requests'], 'max_output_chars': 1000}, 'resumed')['data']
    assert len(resumed['files'][0]['text']) == 1000


def test_background_test_waits_and_verifies_only_current_work(tmp_path):
    root, manager, _, store, runtime = development_env(tmp_path)
    (root / 'test_ok.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n', encoding='utf-8')
    try:
        data = call(runtime, 'run_commands', {'command': [sys.executable, '-m', 'unittest', 'test_ok'], 'background': True, 'timeout_seconds': 20}, 'start')['data']
        assert store.record.run_runtime['pending_verification_jobs'] == [data['job_id']]
        result = call(runtime, 'read_job', {'job_id': data['job_id'], 'wait_seconds': 5, 'output': True}, 'wait')
        assert result['data']['status'] == 'succeeded'
        assert result['data']['passed'] is True
        assert result['observations'][0]['passed'] is True
        assert not store.record.run_runtime['pending_verification_jobs']
        manager.observe([{'kind': 'resource_changed', 'path': str(root / 'test_ok.py'), 'sha256': None}])
        stale = call(runtime, 'read_job', {'job_id': data['job_id']}, 'stale')
        assert not stale['data']['verification_current'] and not stale['observations']
        task = manager.active_task()
        task['status'] = 'cancelled'
        manager._put(task)
        manager.begin_task('新的开发任务')
        different = call(runtime, 'read_job', {'job_id': data['job_id']}, 'different-task')
        assert not different['data']['verification_current'] and not different['observations']
    finally:
        runtime.jobs.close()


def test_job_recovery_does_not_replay_process(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    try:
        started = runtime.jobs.start([sys.executable, '-c', 'import time; time.sleep(20)'], store.workspace, 25)
        restored = JobService(store, lambda x: x)
        assert restored.wait(started['job_id'], .5)['status'] in {'queued', 'running'}
        runtime.jobs.close()
        assert restored.stop(started['job_id'])['status'] == 'stopped'
    finally:
        runtime.jobs.close()


def test_long_run_passes_checkpoint_without_resetting_usage(tmp_path):
    _, _, _, store, runtime = development_env(tmp_path)
    for i in range(24):
        (store.workspace / f'part_{i}.txt').write_text(f'content {i}')
    responses = [Response(tool_calls=[ToolCall(f'read{i}', 'read_files', json.dumps({'path': f'part_{i}.txt'}))], finish_reason='tool_calls') for i in range(24)]
    model = FakeModel(*responses, Response(content='检查结束'))
    result = Runner(model, runtime, max_rounds=30, max_tools=40).start('检查本地代码工具')
    assert result['status'] == 'completed'
    state = store.record.run_runtime
    assert state['progress_checkpoint']['round'] == 20
    assert state['usage']['model_calls'] == 25 and store.record.tool_count == 24
    assert state['budget']['model_calls'] == 30
