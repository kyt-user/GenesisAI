"""Office/PDF 可选能力的受控实现与结构验证。"""

from __future__ import annotations

from pathlib import Path
import importlib
import zipfile

from genesisai.shared.filetypes import detect_file_type
from genesisai.shared.security import ToolError
from genesisai.shared.ids import sha



def require(module, extra="office"):
    try: return importlib.import_module(module)
    except ImportError as exc: raise ToolError("missing_dependency", f"缺少 {module}；请安装 genesisai[{extra}]") from exc


def source(context, path, expected):
    path = context.access.read(path)
    actual = detect_file_type(path)
    if actual != expected:
        raise ToolError("format_mismatch", f"文件内容是 {actual}，不是 {expected}")
    ref = context.store.source(kind="file", path=str(path), sha256=sha(path), title=path.name)
    return path, ref


def ooxml_features(path):
    """列出有限编辑可能无法无损保留的 OOXML 特性。"""
    with zipfile.ZipFile(path) as archive:
        names = [name.casefold() for name in archive.namelist()]
        import xml.etree.ElementTree as ET
        external = any(link.get('TargetMode') == 'External'
                       for name in archive.namelist() if name.endswith('.rels')
                       for link in ET.fromstring(archive.read(name)))
    features = []
    if any(name.endswith("vbaproject.bin") for name in names):
        features.append("macros")
    if external or any("externallinks/" in name or "externalLink".casefold() in name for name in names):
        features.append("external_links")
    if any("embeddings/" in name or "oleobject" in name for name in names):
        features.append("embedded_objects")
    return features


def reject_complex_edit(path):
    features = ooxml_features(path)
    if features:
        raise ToolError("unsupported_complex_content", "有限编辑拒绝可能丢失的复杂内容：" + ", ".join(features))


def artifact(context, path, kind, *, limitations=None):
    actual = detect_file_type(path)
    if actual != kind:
        raise ToolError("artifact_invalid", f"产物验证失败：期望 {kind}，实际 {actual}")
    ref = context.store.artifact(path)
    context.store.record.artifacts[ref].update(format=kind, size=path.stat().st_size, verified=True, verification='structural_reopen', rendering='not_available', limitations=limitations or [])
    context.store.commit()
    return {"path": str(path), "artifact_refs": [ref], "format": kind, "sha256": sha(path), "verified": True, "verification": "structural_reopen", "rendering": "not_available", "limitations": limitations or []}


def read_document(context, args, kind):
    from genesisai.core.extensions.tools.office.readers import units, page
    path, ref = source(context, args['path'], kind)
    digest = sha(path)
    if args.get('expected_sha256') is not None and args['expected_sha256'] != digest:
        raise ToolError('document_changed', '文档已变化，请重新开始分页')
    result = page(units(path, kind, **{k: args[k] for k in ('section', 'sheet', 'range', 'view') if k in args}),
                  **{k: args[k] for k in ('offset', 'text_offset', 'limit', 'max_chars') if k in args})
    if sha(path) != digest:
        raise ToolError('document_changed', '读取期间文档已变化')
    if result['next']:
        result['next'] = {**args, **result['next'], 'expected_sha256': digest}
    result.update(source_ref=ref, sha256=digest, format=kind, rendering='not_available',
                  ocr='not_available', formulas_not_recalculated=kind == 'xlsx')
    if kind != 'pdf': result['complex_features'] = ooxml_features(path)
    result['coverage']['scope'] = {'docx': args.get('section', 'body'), 'xlsx': args.get('sheet', 'sheet_catalog'),
                                  'pptx': 'slide_text_and_notes', 'pdf': 'extractable_page_text'}[kind]
    return result


def docx_read(context, args):
    return read_document(context, args, 'docx')


def write_target(context, args, kind):
    target = context.access.write(args['path'], overwrite=args.get('overwrite', False))
    if target.suffix.casefold() != '.' + kind:
        raise ToolError('format_mismatch', '输出扩展名必须是 .' + kind)
    before = sha(target) if target.exists() else None
    if args.get('overwrite') and (before is None or args.get('expected_sha256') != before):
        raise ToolError('patch_conflict', '覆盖需要存在的目标及正确的 expected_sha256')
    if not args.get('overwrite') and args.get('expected_sha256') is not None:
        raise ToolError('validation_error', 'expected_sha256 只能用于显式覆盖')
    return target, before


def preview_write(context, args, kind):
    target, before = write_target(context, args, kind)
    result = {'target': str(target), 'before_sha256': before, 'overwrite': args.get('overwrite', False)}
    if args.get('source'):
        source_path = context.access.read(args['source'])
        result.update(source=str(source_path), source_sha256=sha(source_path))
    return result


def save_document(context, args, kind, writer, *, limitations=()):
    import os
    from tempfile import NamedTemporaryFile
    from genesisai.shared.changes import backup_file, record_change
    target, before = write_target(context, args, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=target.parent, suffix='.' + kind, prefix='.genesis-office-', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        writer(temporary)
        if detect_file_type(temporary) != kind:
            raise ToolError('artifact_invalid', '临时产物格式验证失败')
        # Reopen the actual generated package before publishing any bytes.
        if kind == 'docx': require('docx').Document(temporary)
        elif kind == 'xlsx': require('openpyxl').load_workbook(temporary, read_only=True).close()
        elif kind == 'pptx': require('pptx').Presentation(temporary)
        else: require('pypdf', 'pdf').PdfReader(temporary)
        if (sha(target) if target.exists() else None) != before:
            raise ToolError('patch_conflict', '写入期间目标已变化，未覆盖')
        backup = backup_file(context.store, target) if before is not None else None
        if before is None:
            # Windows rename fails if a target appeared; POSIX link likewise
            # publishes atomically without overwriting a concurrent creation.
            if os.name == 'nt': os.rename(temporary, target)
            else: os.link(temporary, target)
        else:
            os.replace(temporary, target)
        change = record_change(context.store, operation='office_write', path=target, before_sha=before,
                               after_sha=sha(target), backup=backup)
        result = artifact(context, target, kind, limitations=list(limitations))
        result.update(change_id=change['id'], before_sha256=before)
        return result
    finally:
        temporary.unlink(missing_ok=True)  # Only this operation's generated temporary file.


def docx_create(context, args):
    Document = require('docx').Document
    if args.get('source'):
        source_path, ref = source(context, args['source'], 'docx')
        reject_complex_edit(source_path)
        document = Document(source_path)
    else:
        document = Document()
    if args.get('title'): document.add_heading(args['title'], level=0)
    for value in args.get('paragraphs', []): document.add_paragraph(value)
    rows = args.get('table') or []
    if rows:
        width = max(len(row) for row in rows)
        table = document.add_table(rows=len(rows), cols=width)
        for r, row in enumerate(rows):
            for c, value in enumerate(row): table.cell(r, c).text = str(value)
    return save_document(context, args, 'docx', document.save)


def docx_edit(context, args):
    source_path, ref = source(context, args["source"], "docx")
    reject_complex_edit(source_path)
    Document = require("docx").Document
    document = Document(source_path)
    replaced = 0
    def paragraphs(container):
        yield from container.paragraphs
        for table in container.tables:
            for row in table.rows:
                for cell in row.cells:
                    yield from paragraphs(cell)
    seen = set()
    containers = [document, *(s.header for s in document.sections), *(s.footer for s in document.sections)]
    for paragraph in (p for container in containers for p in paragraphs(container)):
        if paragraph._p in seen: continue
        seen.add(paragraph._p)
        if args["find"] in paragraph.text:
            paragraph.text = paragraph.text.replace(args["find"], args["replace"])
            replaced += 1
    if not replaced: raise ToolError("not_found", "DOCX 中没有找到目标文字")
    result = save_document(context, args, "docx", document.save, limitations=["目标段落内的复杂 run 样式可能被归一化"])
    result.update(source_ref=ref, replacements=replaced); return result


def xlsx_read(context, args):
    return read_document(context, args, 'xlsx')


def xlsx_create(context, args):
    openpyxl = require("openpyxl")
    book = openpyxl.Workbook(); book.remove(book.active)
    if not args['sheets'] or len({name.casefold() for name in args['sheets']}) != len(args['sheets']):
        raise ToolError('validation_error', '工作表名称不能为空或重复')
    for name, rows in args["sheets"].items():
        if not name or len(name) > 31: raise ToolError('validation_error', '工作表名称长度必须为1至31字符')
        sheet = book.create_sheet(name)
        for row in rows: sheet.append(row)
    try: return save_document(context, args, "xlsx", book.save, limitations=["公式已写入但未由 Excel 重新计算"])
    finally: book.close()


def xlsx_edit(context, args):
    source_path, ref = source(context, args["source"], "xlsx")
    reject_complex_edit(source_path)
    openpyxl = require("openpyxl")
    book = openpyxl.load_workbook(source_path)
    for address, value in args["cells"].items():
        if "!" not in address: raise ToolError("validation_error", "单元格键必须是 Sheet!A1")
        sheet, cell = address.split("!", 1)
        if sheet not in book.sheetnames: raise ToolError("not_found", f"工作表不存在：{sheet}")
        book[sheet][cell] = value
    try:
        result = save_document(context, args, "xlsx", book.save, limitations=["公式已写入但未由 Excel 重新计算"])
        result["source_ref"] = ref
        return result
    finally: book.close()


def pptx_read(context, args):
    return read_document(context, args, 'pptx')


def pptx_create(context, args):
    Presentation = require('pptx').Presentation
    if args.get('source'):
        source_path, ref = source(context, args['source'], 'pptx')
        reject_complex_edit(source_path)
        deck = Presentation(source_path)
    else:
        deck = Presentation()
    for item in args['slides']:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = item['title']
        slide.placeholders[1].text = '\n'.join(item.get('bullets', []))
    return save_document(context, args, 'pptx', deck.save, limitations=['仅支持基础标题与项目符号版式'])


def pdf_read(context, args):
    return read_document(context, args, 'pdf')


def pdf_create(context, args):
    canvas = require('reportlab.pdfgen.canvas', 'pdf')
    pagesizes = require('reportlab.lib.pagesizes', 'pdf')
    pdfmetrics = require('reportlab.pdfbase.pdfmetrics', 'pdf')
    cidfonts = require('reportlab.pdfbase.cidfonts', 'pdf')
    font = 'STSong-Light'
    pdfmetrics.registerFont(cidfonts.UnicodeCIDFont(font))
    def write(path):
        writer = canvas.Canvas(str(path), pagesize=pagesizes.A4)
        width, height = pagesizes.A4
        y = height - 50
        writer.setFont(font, 12)
        for value in args['lines']:
            for line in str(value).split('\n'):
                remaining = line
                while remaining or line == '':
                    low, high = 1, len(remaining)
                    while low < high:
                        mid = (low + high + 1) // 2
                        if pdfmetrics.stringWidth(remaining[:mid], font, 12) <= width - 100: low = mid
                        else: high = mid - 1
                    if y < 50:
                        writer.showPage(); y = height - 50; writer.setFont(font, 12)
                    writer.drawString(50, y, remaining[:low]); y -= 18
                    remaining = remaining[low:]
                    if not remaining: break
        writer.save()
    return save_document(context, args, 'pdf', write, limitations=['基础文字PDF；自动换行分页，无版式保真承诺'])
