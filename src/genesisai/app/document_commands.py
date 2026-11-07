"""PDF passwords enter through a hidden terminal prompt, never a model call."""
import getpass
import shlex
import warnings

from genesisai.core.extensions.tools.office.helpers import save_document, source
from genesisai.shared.ids import sha
from genesisai.shared.security import ToolError


def hidden_password():
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        try: return getpass.getpass('PDF 密码（隐藏输入，不保存）：')
        except getpass.GetPassWarning:
            raise ValueError('当前终端不支持隐藏输入，请在交互式终端执行 /pdf-unlock') from None


def execute_pdf_unlock(runtime, text, *, password_reader=None):
    parts = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
             for part in shlex.split(text, posix=False)]
    if len(parts) != 3 or parts[0] != '/pdf-unlock':
        raise ValueError('用法：/pdf-unlock "输入路径.pdf" "新输出文件.pdf"；密码仅在随后隐藏输入')
    from pypdf import PdfReader, PdfWriter
    path, ref = source(runtime.context, parts[1], 'pdf')
    # Check output authorization before asking for any secret. Existing files are rejected.
    runtime.access.write(parts[2])
    digest = sha(path)
    reader = PdfReader(path)
    if not reader.is_encrypted:
        raise ToolError('not_encrypted', 'PDF 未加密，无需解锁')
    password = (password_reader or hidden_password)()
    try:
        if not reader.decrypt(password):
            raise ToolError('invalid_password', '密码错误，未生成解锁文件')
    finally:
        password = None
    if sha(path) != digest:
        raise ToolError('document_changed', '解锁期间源文档已变化')
    writer = PdfWriter(clone_from=reader)
    try:
        result = save_document(runtime.context, {'path': parts[2]}, 'pdf', writer.write,
                               limitations=['解锁副本不保留原文件的加密；数字签名不再代表此副本'])
    finally:
        writer.close()
    result.update(source_ref=ref, source_sha256=digest, decrypted=True)
    return result
