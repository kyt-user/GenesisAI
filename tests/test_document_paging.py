"""Real Office packages: tails, resumable units, overwrite conflicts and PDF text."""
import json
from pathlib import Path
import zipfile

import pytest

from genesisai.core.extensions.tools.office import helpers, readers
from genesisai.core.extensions.tools.filesystem.shared import extract
from genesisai.shared.filetypes import detect_file_type
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError
from test_upgrade_p2_p6 import runtime_for, execute


@pytest.fixture
def office(tmp_path):
    workspace, output, store, runtime = runtime_for(tmp_path)
    try:
        yield workspace, output, store, runtime
    finally:
        runtime.close()


def test_docx_tail_table_and_large_unit_are_fully_reachable(office):
    from docx import Document
    workspace, _, _, runtime = office
    path = workspace / 'long.docx'
    doc = Document()
    for i in range(3105): doc.add_paragraph('paragraph_' + str(i))
    doc.add_paragraph('巨型段落\t换行\n' * 2000 + 'LONG_TAIL')
    table = doc.add_table(rows=205, cols=2)
    table.cell(204, 1).text = 'TABLE_TAIL'
    doc.sections[0].header.paragraphs[0].text = 'HEADER_TEXT'
    doc.save(path)
    page = helpers.docx_read(runtime.context, {'path': str(path), 'section': 'paragraphs', 'offset': 3100, 'max_chars': 1000})
    pieces = []
    while True:
        pieces.extend(item['text'] for item in page['items'] if item['paragraph'] == 3105)
        if page['next'] is None: break
        page = helpers.docx_read(runtime.context, page['next'])
    assert ''.join(pieces) == '巨型段落\t换行\n' * 2000 + 'LONG_TAIL'
    table_page = helpers.docx_read(runtime.context, {'path': str(path), 'section': 'tables', 'offset': 204})
    assert 'TABLE_TAIL' in table_page['items'][0]['text']
    body, clipped = extract(path)
    assert 'LONG_TAIL' in body and 'TABLE_TAIL' in body and 'HEADER_TEXT' in body and not clipped
    page = helpers.docx_read(runtime.context, {'path': str(path), 'limit': 1})
    doc.add_paragraph('changed'); doc.save(path)
    with pytest.raises(ToolError, match='文档已变化'):
        helpers.docx_read(runtime.context, page['next'])


def test_xlsx_more_sheets_far_cells_and_formula_style_cursors(office):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    workspace, _, _, runtime = office
    path = workspace / 'wide.xlsx'
    book = Workbook(); book.remove(book.active)
    for i in range(25): book.create_sheet(f'Sheet{i}')
    sheet = book['Sheet24']
    sheet['BC1501'] = 'FAR_TAIL'
    for i in range(1, 2105):
        sheet.cell(i, 1, '=1+1').font = Font(bold=True)
    sheet.merge_cells('BC1501:BD1501')
    sheet.merge_cells('A1:B1')
    book.save(path); book.close()
    catalog = helpers.xlsx_read(runtime.context, {'path': str(path), 'offset': 20})
    assert [item['sheet'] for item in catalog['items']] == [f'Sheet{i}' for i in range(20, 25)]
    region = helpers.xlsx_read(runtime.context, {'path': str(path), 'sheet': 'Sheet24', 'range': 'BC1501:BC1501'})
    assert region['items'][0]['text'] == 'FAR_TAIL'
    merges = helpers.xlsx_read(runtime.context, {'path': str(path), 'sheet': 'Sheet24', 'range': 'BC1501:BD1501', 'view': 'merges'})
    assert [item['text'] for item in merges['items']] == ['BC1501:BD1501']
    for view in ('formulas', 'styles'):
        result = helpers.xlsx_read(runtime.context, {'path': str(path), 'sheet': 'Sheet24', 'view': view, 'offset': 2100})
        assert result['items'][-1]['coordinate'] == 'A2104' and result['next'] is None
    text, _ = extract(path)
    assert 'FAR_TAIL' in text and '## Sheet24' in text


def test_pptx_after_200_and_append_more_than_100_slides(office):
    from pptx import Presentation
    workspace, output, _, runtime = office
    initial = [{'title': str(i), 'bullets': ['content']} for i in range(105)]
    first = helpers.pptx_create(runtime.context, {'path': 'deck.pptx', 'slides': initial})
    added = [{'title': 'tail_' + str(i)} for i in range(100)]
    second = helpers.pptx_create(runtime.context, {'path': 'deck.pptx', 'source': str(output / 'deck.pptx'),
        'slides': added, 'overwrite': True, 'expected_sha256': first['sha256']})
    result = helpers.pptx_read(runtime.context, {'path': str(output / 'deck.pptx'), 'offset': 200})
    assert len(result['items']) == 5 and 'tail_99' in result['items'][-1]['text']
    assert len(Presentation(output / 'deck.pptx').slides) == 205
    assert second['before_sha256'] == first['sha256']


def test_pdf_long_lines_not_cropped_and_page_tail_is_readable(office):
    from pypdf import PdfReader
    from reportlab.pdfgen.canvas import Canvas
    workspace, output, _, runtime = office
    long_line = 'ASCII_SEGMENT_' * 300 + 'LONG_LINE_TAIL'
    result = helpers.pdf_create(runtime.context, {'path': 'wrapped.pdf', 'lines': [long_line]})
    pages = PdfReader(output / 'wrapped.pdf').pages
    extracted = ''.join((page.extract_text() or '').replace('\n', '') for page in pages)
    assert long_line == extracted and len(pages) > 1
    path = workspace / 'many.pdf'
    writer = Canvas(str(path))
    for i in range(205): writer.drawString(30, 700, f'page_{i+1}'); writer.showPage()
    writer.save()
    tail = helpers.pdf_read(runtime.context, {'path': str(path), 'offset': 200})
    assert len(tail['items']) == 5 and 'page_205' in tail['items'][-1]['text']
    assert tail['coverage']['end_reached']


def test_docx_append_overwrite_requires_hash_and_preserves_previous_version(office):
    from docx import Document
    _, output, store, runtime = office
    first = helpers.docx_create(runtime.context, {'path': 'note.docx', 'paragraphs': ['first']})
    path = output / 'note.docx'
    original = path.read_bytes()
    with pytest.raises(ToolError, match='已存在'):
        helpers.docx_create(runtime.context, {'path': 'note.docx', 'paragraphs': ['wrong']})
    with pytest.raises(ToolError, match='expected_sha256'):
        helpers.docx_create(runtime.context, {'path': 'note.docx', 'paragraphs': ['wrong'], 'overwrite': True})
    result = helpers.docx_create(runtime.context, {'path': 'note.docx', 'source': str(path), 'paragraphs': ['second'],
        'overwrite': True, 'expected_sha256': first['sha256']})
    assert [p.text for p in Document(path).paragraphs] == ['first', 'second']
    change = store.record.changes[-1]
    assert Path(change['backup']).read_bytes() == original
    assert change['before_sha256'] == first['sha256'] and result['change_id'] == change['id']


def test_generated_writer_cannot_publish_over_concurrently_changed_target(office):
    from docx import Document
    _, output, _, runtime = office
    first = helpers.docx_create(runtime.context, {'path': 'race.docx', 'paragraphs': ['first']})
    target = output / 'race.docx'
    changed = b'concurrent user change'
    def writer(path):
        Document().save(path)
        target.write_bytes(changed)
    with pytest.raises(ToolError, match='写入期间'):
        helpers.save_document(runtime.context, {'path': 'race.docx', 'overwrite': True,
            'expected_sha256': first['sha256']}, 'docx', writer)
    assert target.read_bytes() == changed


def test_unified_ooxml_budget_applies_to_generic_and_specific_readers(office, monkeypatch):
    from docx import Document
    workspace, _, _, runtime = office
    path = workspace / 'budget.docx'; Document().save(path)
    monkeypatch.setenv('GENESISAI_DOCUMENT_MAX_EXPANDED_BYTES', '100')
    for operation in (lambda: detect_file_type(path), lambda: extract(path),
                      lambda: helpers.docx_read(runtime.context, {'path': str(path)})):
        with pytest.raises(ToolError, match='解包资源配额'): operation()


def test_pdf_scan_page_is_explicit_and_tool_schemas_accept_paging(office):
    from pypdf import PdfWriter
    workspace, _, _, runtime = office
    path = workspace / 'scan.pdf'
    writer = PdfWriter(); writer.add_blank_page(100, 100); writer.write(path)
    response = execute(runtime, 'pdf_read', {'path': str(path), 'offset': 0, 'max_chars': 100})
    assert response['ok'], response
    assert response['data']['items'][0]['extraction'] == 'no_text_ocr_required'
    assert response['data']['ocr'] == 'not_available'
    assert '独立OCR' in extract(path)[0]


def test_zip_entry_budget_is_configurable_and_compression_guard_retained(tmp_path, monkeypatch):
    from genesisai.shared.filetypes import inspect_ooxml
    path = tmp_path / 'many.docx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<document/>')
        for i in range(5010): archive.writestr(f'parts/{i}.xml', '')
    assert detect_file_type(path) == 'docx'
    monkeypatch.setenv('GENESISAI_DOCUMENT_MAX_ENTRIES', '5000')
    with pytest.raises(ToolError, match='解包资源配额'): inspect_ooxml(path)
    monkeypatch.delenv('GENESISAI_DOCUMENT_MAX_ENTRIES')
    compressed = tmp_path / 'compressed.docx'
    with zipfile.ZipFile(compressed, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', 'x' * (2 * 1024 * 1024))
    monkeypatch.setenv('GENESISAI_DOCUMENT_MAX_COMPRESSION_RATIO', '10')
    with pytest.raises(ToolError, match='压缩倍率'): inspect_ooxml(compressed)


def test_overwrite_does_not_expand_output_authorization(tmp_path):
    from genesisai.shared.security import Access
    output = tmp_path / 'output'; output.mkdir()
    outside = tmp_path / 'outside.docx'; outside.write_bytes(b'user data')
    access = Access([tmp_path], output, tmp_path / 'state', workspace_root=tmp_path)
    for name in ('../outside.docx', str(outside)):
        with pytest.raises(ToolError, match='输出只能'):
            access.write(name, overwrite=True)
    assert outside.read_bytes() == b'user data'


def test_overwrite_respects_redirected_links_and_external_output_grant(tmp_path):
    import os
    import subprocess
    from genesisai.shared.security import Access
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    output = workspace / 'output'; output.mkdir()
    external = tmp_path / 'authorized-output'; external.mkdir()
    original = external / 'existing.docx'; original.write_bytes(b'original external data')
    link = output / 'redirected'
    if os.name == 'nt':
        env = {**os.environ, 'GENESIS_TEST_JUNCTION': str(link), 'GENESIS_TEST_TARGET': str(external)}
        result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
            'try { New-Item -ItemType Junction -Path $env:GENESIS_TEST_JUNCTION -Target $env:GENESIS_TEST_TARGET -ErrorAction Stop | Out-Null } '
            'catch { if ($_.Exception.NativeErrorCode -eq 5) { exit 5 }; Write-Error $_; exit 1 }'],
            env=env, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 5:
            pytest.skip('Windows 当前运行环境不允许创建 junction；需要允许 reparse point 的专项验收')
        assert result.returncode == 0, result.stderr.decode('mbcs', errors='replace')
    else:
        link.symlink_to(external, target_is_directory=True)
    assert link.resolve() == external.resolve()
    restricted = Access([workspace], output, tmp_path / 'state', workspace_root=workspace)
    with pytest.raises(ToolError, match='输出路径越界'):
        restricted.write('redirected/existing.docx', overwrite=True)
    granted = Access([workspace], external, tmp_path / 'state', workspace_root=workspace)
    assert granted.write('existing.docx', overwrite=True) == original
    assert original.read_bytes() == b'original external data'


def test_external_relationships_remain_outside_limited_edit(office):
    from docx import Document
    workspace, _, _, runtime = office
    path = workspace / 'external.docx'
    document = Document(); document.add_paragraph('original')
    document.part.relate_to('https://example.invalid/reference',
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink', is_external=True)
    document.save(path)
    assert 'external_links' in helpers.ooxml_features(path)
    with pytest.raises(ToolError, match='复杂内容'):
        helpers.docx_edit(runtime.context, {'source': str(path), 'path': 'edited.docx', 'find': 'original', 'replace': 'changed'})
    assert Document(path).paragraphs[0].text == 'original'


def test_nested_docx_table_rows_have_stable_table_coordinates(tmp_path):
    from docx import Document
    path = tmp_path / 'nested.docx'
    doc = Document()
    outer = doc.add_table(rows=2, cols=1)
    outer.cell(0, 0).text = 'outer0'
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).text = 'inner0'
    outer.cell(1, 0).text = 'outer1'
    doc.save(path)
    rows = list(readers.docx_units(path, 'tables'))
    assert [(item['table'], item['row']) for item in rows] == [(1, 0), (0, 0), (0, 1)]
    assert 'inner0' in rows[0]['text'] and rows[2]['text'] == 'outer1'


def test_creation_schemas_and_real_outputs_exceed_old_total_limits(office):
    from docx import Document
    from pypdf import PdfReader
    _, output, _, runtime = office
    paragraphs = ['row'] * 1000 + ['DOCX_CREATE_TAIL']
    created = execute(runtime, 'docx_create', {'path': 'large-created.docx', 'paragraphs': paragraphs}, 'long_docx')
    assert created['ok'], created
    assert Document(output / 'large-created.docx').paragraphs[-1].text == 'DOCX_CREATE_TAIL'
    lines = ['row'] * 5000 + ['PDF_CREATE_TAIL']
    created = execute(runtime, 'pdf_create', {'path': 'large-created.pdf', 'lines': lines}, 'long_pdf')
    assert created['ok'], created
    assert 'PDF_CREATE_TAIL' in PdfReader(output / 'large-created.pdf').pages[-1].extract_text()
