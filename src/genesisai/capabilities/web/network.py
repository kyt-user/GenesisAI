"""仅访问公网、固定解析地址，并限制正文和校验重定向的 HTTP 客户端。"""
import http.client
import io
import ipaddress
import socket
import ssl
import time
import zlib
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from genesisai.shared.security import ToolError
from genesisai.capabilities.web.contracts import canonicalize_url

MAX_BODY = 4 * 1024 * 1024


def public_ip(value):
    ip = ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


class Network:
    def __init__(self, resolver=None, transport=None, timeout=15):
        self.resolver = resolver or socket.getaddrinfo
        self.transport = transport or self._request
        self.timeout = timeout

    def resolve(self, url):
        url = canonicalize_url(url)
        parts = urlsplit(url)
        records = self.resolver(parts.hostname, 443 if parts.scheme == 'https' else 80, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not public_ip(ip) for ip in addresses):
            raise ToolError('unsafe_url', '禁止私网、保留地址或混合 DNS 结果')
        return url, addresses[0]

    def _request(self, url, ip, headers):
        parsed = urlsplit(url)
        connection = (http.client.HTTPSConnection(parsed.hostname, context=ssl.create_default_context(), timeout=self.timeout)
                      if parsed.scheme == 'https' else http.client.HTTPConnection(parsed.hostname, timeout=self.timeout))
        # Host 和 TLS SNI 保留原始域名，TCP 连接使用已经验证的 IP。
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection((ip, address[1]), timeout, source_address)
        deadline = time.monotonic() + self.timeout
        try:
            connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''), headers=headers)
            response = connection.getresponse()
            content = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise ToolError('timeout', '网络读取超时', True)
                piece = response.read1(min(65536, MAX_BODY + 1 - len(content)))
                if not piece:
                    break
                content.extend(piece)
                if len(content) > MAX_BODY:
                    raise ToolError('size_limit', '网页超过响应上限')
            return response.status, dict(response.getheaders()), bytes(content)
        finally:
            connection.close()

    def get(self, url, params=None, headers=None):
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        for redirect in range(6):
            url, ip = self.resolve(url)
            request_headers = {'User-Agent': 'GenesisAI/0.1 public-research', 'Accept-Encoding': 'identity', **(headers or {})}
            status, response_headers, body = self.transport(url, ip, request_headers)
            if len(body) > MAX_BODY:
                raise ToolError('size_limit', '响应过大')
            normalized = {k.lower(): v for k, v in response_headers.items()}
            if status in {301, 302, 303, 307, 308}:
                if headers:
                    raise ToolError('unsafe_redirect', '含认证头的请求不跟随重定向')
                url = urljoin(url, normalized.get('location', ''))
                continue
            encoding = normalized.get('content-encoding', 'identity').lower()
            if encoding in {'gzip', 'deflate'}:
                decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
                body = decoder.decompress(body, MAX_BODY+1)
                if len(body) > MAX_BODY or decoder.unconsumed_tail:
                    raise ToolError('size_limit', '解压正文超过上限')
                if not decoder.eof:
                    raise ToolError('invalid_response', '压缩正文不完整')
            elif encoding == 'br':
                import brotli
                decoder = brotli.Decompressor()
                decoded = bytearray()
                for offset in range(0, len(body), 256):
                    decoded.extend(decoder.process(body[offset:offset+256]))
                    if len(decoded) > MAX_BODY:
                        raise ToolError('size_limit', '解压正文超过上限')
                if not decoder.is_finished():
                    raise ToolError('invalid_response', '压缩正文不完整')
                body = bytes(decoded)
            elif encoding not in {'', 'identity'}:
                raise ToolError('unsupported_encoding', '不支持的响应编码')
            response_headers = {k:v for k,v in response_headers.items() if k.lower() not in {'content-encoding','content-length'}}
            return httpx.Response(status, headers=response_headers, content=body, request=httpx.Request('GET', url))
        raise ToolError('redirect_limit', '重定向次数超过上限')

    def close(self):
        pass

    def fetch(self, url):
        response = self.get(url)
        links = []
        if response.status_code != 200:
            raise ToolError('fetch_failed', f'HTTP {response.status_code}', response.status_code >= 500)
        media = response.headers.get('content-type', '').lower()
        if 'pdf' in media:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(response.content))
            text = '\n'.join(page.extract_text() or '' for page in reader.pages[:30])
            title = str(response.url)
            truncated = len(reader.pages) > 30
        elif 'html' in media:
            import trafilatura
            from genesisai.capabilities.web.links import discover_links
            from genesisai.capabilities.web.structured_data import extract_product_facts
            links = discover_links(response.text, str(response.url))
            text = trafilatura.extract(response.text, include_tables=True) or ''
            structured = extract_product_facts(response.text)
            if structured:
                # 将小而标准化的数据块放在前面，以便后续价格追问
                # 可以复用第一个缓存片段而无需翻页。
                text = ('Structured product data:\n' + structured + '\n\n' + text.lstrip()).strip()
            metadata = trafilatura.extract_metadata(response.text)
            title = metadata.title if metadata and metadata.title else str(response.url)
            truncated = False
        elif media.startswith('text/plain'):
            text, title, truncated = response.text, str(response.url), False
        else:
            raise ToolError('unsupported_format', '不支持该网络资源类型')
        if not text.strip():
            raise ToolError('no_text', '没有可提取正文；本版未启用 JavaScript 渲染或 OCR')
        return dict(url=str(response.url), title=title, text=text[:200000], truncated=truncated or len(text) > 200000, links=links)

