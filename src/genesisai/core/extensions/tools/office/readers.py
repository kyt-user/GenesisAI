"""Streaming document units and resumable character-bounded pages."""
from contextlib import contextmanager
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from genesisai.shared.filetypes import inspect_ooxml
from genesisai.shared.security import ToolError

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def word_text(node):
    return ''.join((child.text or '') if child.tag == W + 't' else '\t' if child.tag == W + 'tab' else '\n'
                   for child in node.iter() if child.tag in {W + 't', W + 'tab', W + 'br', W + 'cr'})


@contextmanager
def package(path):
    inspect_ooxml(path)
    with zipfile.ZipFile(path) as archive:
        yield archive


def docx_units(path, section='body'):
    with package(path) as archive:
        members = ['word/document.xml'] if section in {'body', 'paragraphs', 'tables'} else sorted(
            name for name in archive.namelist() if name.startswith('word/' + ('header' if section == 'headers' else 'footer')) and name.endswith('.xml'))
        for member in members:
            depth, table, paragraph = 0, -1, 0
            tables = []
            with archive.open(member) as stream:
                for event, node in ET.iterparse(stream, events=('start', 'end')):
                    if event == 'start':
                        if node.tag == W + 'tbl':
                            depth += 1
                            table += 1
                            tables.append([table, 0])
                        continue
                    if node.tag == W + 'p' and not depth:
                        text = word_text(node)
                        if section != 'tables':
                            style = node.find('./' + W + 'pPr/' + W + 'pStyle')
                            yield {'kind': 'paragraph', 'paragraph': paragraph, 'part': member,
                                   'style': style.get(W + 'val') if style is not None else None, 'text': text}
                        paragraph += 1
                        node.clear()
                    elif node.tag == W + 'tr':
                        if section in {'body', 'tables', 'headers', 'footers'}:
                            cells = ['\n'.join(word_text(p) for p in cell.findall(W + 'p')) for cell in node.findall(W + 'tc')]
                            yield {'kind': 'table_row', 'table': tables[-1][0], 'row': tables[-1][1], 'part': member, 'text': '\t'.join(cells)}
                        tables[-1][1] += 1
                        node.clear()
                    elif node.tag == W + 'tbl':
                        depth -= 1
                        tables.pop()
                        node.clear()


def xlsx_sheets(archive):
    relationships = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
    targets = {item.get('Id'): item.get('Target') for item in relationships}
    workbook = ET.fromstring(archive.read('xl/workbook.xml'))
    for sheet in workbook.findall('./' + S + 'sheets/' + S + 'sheet'):
        target = targets[sheet.get(R + 'id')]
        member = target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/' + target)
        if not member.startswith('xl/') or member not in archive.namelist():
            raise ToolError('corrupt_document', '工作表关系路径无效')
        yield sheet.get('name'), member


def xlsx_units(path, *, sheet=None, cell_range=None, view='cells'):
    from openpyxl.utils.cell import range_boundaries, coordinate_to_tuple
    bounds = range_boundaries(cell_range) if cell_range else (1, 1, 16384, 1048576)
    if any(type(v) is not int or v < 1 for v in bounds) or bounds[2] < bounds[0] or bounds[3] < bounds[1]:
        raise ToolError('validation_error', 'range 必须为完整的 A1 单元格矩形')
    with package(path) as archive:
        sheets = list(xlsx_sheets(archive))
        if sheet is None:
            for name, _ in sheets:
                yield {'kind': 'sheet', 'sheet': name, 'text': name}
            return
        member = next((member for name, member in sheets if name == sheet), None)
        if member is None:
            raise ToolError('not_found', '工作表不存在：' + sheet)
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            with archive.open('xl/sharedStrings.xml') as stream:
                for _, node in ET.iterparse(stream):
                    if node.tag == S + 'si':
                        strings.append(''.join(t.text or '' for t in node.iter(S + 't')))
                        node.clear()
        with archive.open(member) as stream:
            for _, node in ET.iterparse(stream):
                if view == 'merges' and node.tag == S + 'mergeCell':
                    region = node.get('ref', '')
                    left, top, right, bottom = range_boundaries(region)
                    if left <= bounds[2] and right >= bounds[0] and top <= bounds[3] and bottom >= bounds[1]:
                        yield {'kind': 'merge', 'sheet': sheet, 'text': region}
                if node.tag != S + 'c':
                    continue
                coordinate = node.get('r', '')
                row, column = coordinate_to_tuple(coordinate)
                if not (bounds[0] <= column <= bounds[2] and bounds[1] <= row <= bounds[3]):
                    node.clear()
                    continue
                formula = node.find(S + 'f')
                style = int(node.get('s', '0'))
                value = node.find(S + 'v')
                text = value.text or '' if value is not None else ''
                kind = node.get('t', 'n')
                if kind == 's':
                    text = strings[int(text)]
                elif kind == 'inlineStr':
                    text = ''.join(t.text or '' for t in node.iter(S + 't'))
                if view != 'merges' and (view != 'formulas' or formula is not None) and (view != 'styles' or style):
                    yield {'kind': 'cell', 'sheet': sheet, 'coordinate': coordinate, 'value_type': kind,
                           'style_id': style, 'formula': formula is not None,
                           'formula_attributes': dict(formula.attrib) if formula is not None else {},
                           'text': ('=' + formula.text if formula.text else '[shared/array formula reference]') if formula is not None else text}
                node.clear()


def pptx_units(path):
    from pptx import Presentation
    inspect_ooxml(path)
    deck = Presentation(path)
    def shape_text(shapes):
        for shape in shapes:
            if getattr(shape, 'has_text_frame', False): yield shape.text
            if getattr(shape, 'has_table', False):
                for row in shape.table.rows: yield '\t'.join(cell.text for cell in row.cells)
            if hasattr(shape, 'shapes'): yield from shape_text(shape.shapes)
    for index, slide in enumerate(deck.slides):
        texts = list(shape_text(slide.shapes))
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ''
        yield {'kind': 'slide', 'number': index + 1, 'objects': len(slide.shapes),
               'text': '\n'.join([*texts, '[notes]\n' + notes] if notes else texts)}


def pdf_units(path):
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ToolError('encrypted', 'PDF 已加密；需要独立授权的密码输入，本工具不接收模型参数中的密码')
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ''
        yield {'kind': 'page', 'number': index + 1, 'page_count': len(reader.pages), 'text': text,
               'extraction': 'text' if text.strip() else 'no_text_ocr_required'}


def page(units, *, offset=0, text_offset=0, limit=100, max_chars=16000):
    if any(type(v) is not int for v in (offset, text_offset, limit, max_chars)) or min(offset, text_offset) < 0 or min(limit, max_chars) < 1:
        raise ToolError('validation_error', '分页参数无效')
    items, remaining, next_page = [], max_chars, None
    for index, unit in enumerate(units):
        if index < offset:
            continue
        start = text_offset if index == offset else 0
        if len(items) >= limit or remaining <= 0:
            next_page = {'offset': index, 'text_offset': start}
            break
        text = unit['text']
        if start > len(text):
            raise ToolError('validation_error', 'text_offset 超出当前文档单元')
        body = text[start:start + remaining]
        items.append({**unit, 'text': body, 'offset': index, 'text_offset': start, 'total_chars': len(text)})
        remaining -= len(body)
        if start + len(body) < len(text):
            next_page = {'offset': index, 'text_offset': start + len(body)}
            break
    return {'items': items, 'next': next_page, 'coverage': {'offset': offset, 'text_offset': text_offset,
            'units_returned': len(items), 'text_chars': max_chars - remaining, 'end_reached': next_page is None}}


def units(path, kind, **options):
    if kind == 'docx':
        return docx_units(path, options.get('section', 'body'))
    if kind == 'xlsx':
        return xlsx_units(path, sheet=options.get('sheet'), cell_range=options.get('range'), view=options.get('view', 'cells'))
    if kind == 'pptx':
        return pptx_units(path)
    return pdf_units(path)


def iter_text(path, kind):
    if kind == 'xlsx':
        with package(path) as archive:
            names = [name for name, _ in xlsx_sheets(archive)]
        for name in names:
            yield '## ' + name + '\n'
            for unit in xlsx_units(path, sheet=name):
                yield unit['coordinate'] + '\t' + unit['text'] + '\n'
    else:
        from itertools import chain
        selected = chain(*(docx_units(path, section) for section in ('body', 'headers', 'footers'))) if kind == 'docx' else units(path, kind)
        for unit in selected:
            prefix = f"[page {unit['number']}]\n" if kind in {'pdf', 'pptx'} else ''
            if kind == 'pdf' and unit['extraction'] == 'no_text_ocr_required':
                prefix += '[无可提取文字；需要独立OCR能力]\n'
            yield prefix + unit['text'] + '\n'


def text_page(path, kind, offset, limit):
    consumed, parts, remaining = 0, [], limit
    for chunk in iter_text(path, kind):
        if remaining and consumed + len(chunk) > offset:
            part = chunk[max(0, offset - consumed):][:remaining]
            parts.append(part)
            remaining -= len(part)
        consumed += len(chunk)
    text = ''.join(parts)
    return text, {'total_chars': consumed, 'coverage': 'all_document_text_units',
                  'rendering': 'not_available', 'ocr': 'not_available'}, offset + len(text) if offset + len(text) < consumed else None
