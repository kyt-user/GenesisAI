"""Downloaded document input remains scoped and verifies actual bytes."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from genesisai.core.extensions.tools.web.source_assets import downloaded_document
from genesisai.core.persistence.session import Session
from genesisai.shared.security import ToolError
from test_scoped_authorizations import runtime


def downloaded(runtime):
    from io import BytesIO
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(300, 400)
    stream = BytesIO(); writer.write(stream); writer.close()
    body = stream.getvalue()
    ref = runtime.store.source(kind='web', url='https://fixture.test/scan', title='scan')
    root = runtime.store.root / 'sources'
    root.mkdir(exist_ok=True)
    (root / (ref + '.bin')).write_bytes(body)
    (root / (ref + '.json')).write_text(json.dumps({'raw_source': {'source_ref': ref,
        'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(), 'media_type': 'application/pdf'}}), encoding='utf-8')
    return ref, root / (ref + '.bin')


def test_download_requires_current_session_and_exact_body(runtime):
    ref, path = downloaded(runtime)
    assert downloaded_document(runtime.context, ref, 'pdf') == (path, ref)
    other = Session(runtime.store.root, workspace=runtime.store.workspace)
    with pytest.raises(ToolError, match='当前会话'):
        downloaded_document(SimpleNamespace(store=other), ref, 'pdf')
    with pytest.raises(ToolError):
        downloaded_document(runtime.context, '../' + ref, 'pdf')
    path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises(ToolError, match='哈希'):
        downloaded_document(runtime.context, ref, 'pdf')


def test_downloaded_pdf_ocr_runs_through_job(runtime):
    ref, path = downloaded(runtime)
    from test_scoped_authorizations import invoke
    from test_private_resources import completed
    value = completed(runtime, invoke(runtime, 'pdf_ocr', {'source_ref': ref, 'limit': 1}, 'ocr_download'))
    assert value['source_ref'] == ref and value['network_used'] is False
    assert value['coverage']['page_count'] == 1 and value['coverage']['end_reached']
    assert value['items'][0]['text'] == ''
