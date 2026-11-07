"""Explicit local OCR, independent of ordinary PDF text extraction."""
import math
import os
from pathlib import Path

from genesisai.core.extensions.tools.office.helpers import source
from genesisai.core.extensions.tools.office.readers import page
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError


def local_engine():
    import rapidocr
    root = Path(rapidocr.__file__).parent / 'models'
    models = {'Det': 'PP-OCRv6_det_small.onnx', 'Cls': 'ch_ppocr_mobile_v2.0_cls_mobile.onnx',
              'Rec': 'PP-OCRv6_rec_small.onnx'}
    if any(not (root / name).is_file() for name in models.values()):
        raise ToolError('missing_model', '本地 OCR 模型缺失；安装 genesisai[ocr]，本工具不会联网下载模型')
    # Explicit bundled paths prevent the inference engine's implicit downloads.
    return rapidocr.RapidOCR(params={**{stage + '.model_path': str(root / name) for stage, name in models.items()},
        'Global.log_level': 'error', 'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 2})


class Implementation:
    def prepare(self, context, args): return None

    def execute(self, context, args):
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        if ('path' in args) == ('source_ref' in args):
            raise ToolError('validation_error', 'path 与 source_ref 必须且只能指定一个')
        if 'source_ref' in args:
            from genesisai.core.extensions.tools.web.source_assets import downloaded_document
            path, ref = downloaded_document(context, args['source_ref'], 'pdf')
        else:
            path, ref = source(context, args['path'], 'pdf')
        digest = sha(path)
        if args.get('expected_sha256') not in (None, digest):
            raise ToolError('document_changed', '文档已变化，请重新开始 OCR 分页')
        if PdfReader(path).is_encrypted:
            raise ToolError('encrypted', '先由用户在终端执行 /pdf-unlock，通过隐藏输入解锁至新文件')
        offset, limit, dpi = args.get('offset', 0), args.get('limit', 1), args.get('dpi', 150)
        pixel_budget = int(os.environ.get('GENESISAI_OCR_MAX_PAGE_PIXELS', '20000000'))
        if pixel_budget < 1:
            raise ToolError('configuration_error', 'GENESISAI_OCR_MAX_PAGE_PIXELS 必须为正整数')
        engine = local_engine()
        with pdfium.PdfDocument(path) as document:
            total = len(document)
            stop = min(total, offset + limit)
            def rendered_units():
                for index in range(offset, stop):
                    pdf_page = document[index]
                    try:
                        width, height = pdf_page.get_size()
                        scale = min(dpi / 72, math.sqrt(pixel_budget / max(1, width * height)))
                        bitmap = pdf_page.render(scale=scale)
                        try:
                            image = bitmap.to_pil()
                            try: recognized = engine(image)
                            finally: image.close()
                        finally: bitmap.close()
                        text = '\n'.join(recognized.txts or ())
                        scores = recognized.scores or ()
                        yield {'kind': 'ocr_page', 'number': index + 1, 'text': text,
                               'mean_confidence': sum(scores) / len(scores) if scores else None,
                               'line_count': len(scores), 'effective_dpi': round(scale * 72, 2)}
                    finally: pdf_page.close()
            result = page(rendered_units(), text_offset=args.get('text_offset', 0), limit=limit,
                          max_chars=args.get('max_chars', 16000))
            for item in result['items']: item['offset'] += offset
            if result['next']:
                result['next']['offset'] += offset
            elif stop < total:
                result['next'] = {'offset': stop, 'text_offset': 0}
            if result['next']:
                result['next'] = {**args, **result['next'], 'expected_sha256': digest}
            result['coverage'].update(offset=offset, page_count=total, end_reached=result['next'] is None,
                                      scope='local_ocr_text', recognition_is_uncertain=True)
        if sha(path) != digest:
            raise ToolError('document_changed', 'OCR 期间文档已变化，未返回混合版本')
        result.update(source_ref=ref, sha256=digest, engine='rapidocr_onnxruntime_cpu',
                      model='PP-OCRv6 small', network_used=False,
                      limitations=['OCR 为机器识别文字，需核对原页；不承诺复杂版式、手写或任意语言准确率'])
        return result
