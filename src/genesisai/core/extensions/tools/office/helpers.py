"""Office/PDF 可选能力的受控实现与结构验证。"""

from __future__ import annotations

from pathlib import Path
import importlib
import zipfile

from genesisai.shared.filetypes import detect_file_type
from genesisai.shared.security import ToolError
from genesisai.core.state.store import sha


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
    features = []
    if any(name.endswith("vbaproject.bin") for name in names):
        features.append("macros")
    if any("externallinks/" in name or "externalLink".casefold() in name for name in names):
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
    context.store.data["artifacts"][ref].update(format=kind, size=path.stat().st_size, verified=True, verification="structural_reopen", rendering="not_available", limitations=limitations or [])
    context.store.save()
    return {"path": str(path), "artifact_refs": [ref], "format": kind, "sha256": sha(path), "verified": True, "verification": "structural_reopen", "rendering": "not_available", "limitations": limitations or []}


def docx_read(context, value):
    path, ref = source(context, value, "docx")
    Document = require("docx").Document
    document = Document(path)
    return {"source_ref": ref, "paragraphs": [{"style": p.style.name if p.style else None, "text": p.text} for p in document.paragraphs[:2000]], "tables": [[[cell.text for cell in row.cells] for row in table.rows[:200]] for table in document.tables[:50]], "headers": [p.text for section in document.sections for p in section.header.paragraphs], "footers": [p.text for section in document.sections for p in section.footer.paragraphs], "complex_features": ooxml_features(path)}


def docx_create(context, args):
    Document = require("docx").Document
    target = context.access.write(args["path"])
    if target.suffix.casefold() != ".docx": raise ToolError("format_mismatch", "输出扩展名必须是 .docx")
    target.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    if args.get("title"): document.add_heading(args["title"], level=0)
    for value in args.get("paragraphs", []): document.add_paragraph(value)
    rows = args.get("table") or []
    if rows:
        width = max(len(row) for row in rows)
        table = document.add_table(rows=len(rows), cols=width)
        for r, row in enumerate(rows):
            for c, value in enumerate(row): table.cell(r, c).text = str(value)
    document.save(target)
    return artifact(context, target, "docx")


def docx_edit(context, args):
    source_path, ref = source(context, args["source"], "docx")
    reject_complex_edit(source_path)
    Document = require("docx").Document
    target = context.access.write(args["path"])
    if target.suffix.casefold() != ".docx": raise ToolError("format_mismatch", "输出扩展名必须是 .docx")
    document = Document(source_path)
    replaced = 0
    for paragraph in document.paragraphs:
        if args["find"] in paragraph.text:
            paragraph.text = paragraph.text.replace(args["find"], args["replace"])
            replaced += 1
    if not replaced: raise ToolError("not_found", "DOCX 中没有找到目标文字")
    target.parent.mkdir(parents=True, exist_ok=True); document.save(target)
    result = artifact(context, target, "docx", limitations=["目标段落内的复杂 run 样式可能被归一化"])
    result.update(source_ref=ref, replacements=replaced); return result


def xlsx_read(context, value):
    path, ref = source(context, value, "xlsx")
    load_workbook = require("openpyxl").load_workbook
    book = load_workbook(path, data_only=False, read_only=False)
    try:
        sheets = {}
        for sheet in list(book)[:20]:
            values = [[cell.value for cell in row] for row in sheet.iter_rows(max_row=min(sheet.max_row or 1, 1000), max_col=min(sheet.max_column or 1, 50))]
            formulas = [cell.coordinate for row in sheet.iter_rows() for cell in row if isinstance(cell.value, str) and cell.value.startswith("=")]
            styled = [cell.coordinate for row in sheet.iter_rows() for cell in row if cell.has_style]
            sheets[sheet.title] = {"values": values, "max_row": sheet.max_row, "max_column": sheet.max_column, "merged": [str(rng) for rng in sheet.merged_cells.ranges], "formula_cells": formulas[:2000], "styled_cells": styled[:2000]}
        return {"source_ref": ref, "sheets": sheets, "formulas_not_recalculated": True, "complex_features": ooxml_features(path)}
    finally: book.close()


def xlsx_create(context, args):
    openpyxl = require("openpyxl")
    target = context.access.write(args["path"])
    if target.suffix.casefold() != ".xlsx": raise ToolError("format_mismatch", "输出扩展名必须是 .xlsx")
    book = openpyxl.Workbook(); book.remove(book.active)
    for name, rows in args["sheets"].items():
        sheet = book.create_sheet(str(name)[:31])
        for row in rows: sheet.append(row)
    target.parent.mkdir(parents=True, exist_ok=True); book.save(target); book.close()
    return artifact(context, target, "xlsx", limitations=["公式已写入但未由 Excel 重新计算"])


def xlsx_edit(context, args):
    source_path, ref = source(context, args["source"], "xlsx")
    reject_complex_edit(source_path)
    openpyxl = require("openpyxl")
    target = context.access.write(args["path"])
    book = openpyxl.load_workbook(source_path)
    for address, value in args["cells"].items():
        if "!" not in address: raise ToolError("validation_error", "单元格键必须是 Sheet!A1")
        sheet, cell = address.split("!", 1)
        if sheet not in book.sheetnames: raise ToolError("not_found", f"工作表不存在：{sheet}")
        book[sheet][cell] = value
    target.parent.mkdir(parents=True, exist_ok=True); book.save(target); book.close()
    result = artifact(context, target, "xlsx", limitations=["公式已写入但未由 Excel 重新计算"]); result["source_ref"] = ref; return result


def pptx_read(context, value):
    path, ref = source(context, value, "pptx")
    Presentation = require("pptx").Presentation
    deck = Presentation(path); slides = []
    for index, slide in enumerate(deck.slides):
        if index >= 200:
            break
        texts = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text]
        notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
        slides.append({"number": index + 1, "texts": texts, "notes": notes, "objects": len(slide.shapes)})
    return {"source_ref": ref, "slides": slides, "complex_features": ooxml_features(path)}


def pptx_create(context, args):
    Presentation = require("pptx").Presentation
    target = context.access.write(args["path"])
    if target.suffix.casefold() != ".pptx": raise ToolError("format_mismatch", "输出扩展名必须是 .pptx")
    deck = Presentation()
    for item in args["slides"]:
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = item["title"]
        slide.placeholders[1].text = "\n".join(item.get("bullets", []))
    target.parent.mkdir(parents=True, exist_ok=True); deck.save(target)
    return artifact(context, target, "pptx", limitations=["仅支持基础标题与项目符号版式"])


def pdf_read(context, value):
    path, ref = source(context, value, "pdf")
    PdfReader = require("pypdf", "pdf").PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted: raise ToolError("encrypted", "PDF 已加密")
    pages = [page.extract_text() or "" for page in reader.pages[:200]]
    if not any(text.strip() for text in pages): raise ToolError("no_text", "PDF 可能是扫描件；V1 未启用 OCR")
    return {"source_ref": ref, "page_count": len(reader.pages), "metadata": {str(k): str(v) for k, v in (reader.metadata or {}).items()}, "pages": pages}


def pdf_create(context, args):
    canvas = require("reportlab.pdfgen.canvas", "pdf")
    pagesizes = require("reportlab.lib.pagesizes", "pdf")
    target = context.access.write(args["path"])
    if target.suffix.casefold() != ".pdf": raise ToolError("format_mismatch", "输出扩展名必须是 .pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    writer = canvas.Canvas(str(target), pagesize=pagesizes.A4); width, height = pagesizes.A4; y = height - 50
    for line in args["lines"]:
        if y < 50: writer.showPage(); y = height - 50
        writer.drawString(50, y, str(line)[:120]); y -= 18
    writer.save(); return artifact(context, target, "pdf", limitations=["基础文本 PDF；不支持任意原 PDF 编辑"])

