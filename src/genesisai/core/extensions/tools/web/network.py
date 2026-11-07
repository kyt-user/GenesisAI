"""Public HTTP client: pinned DNS, bounded streaming, shared deadlines and retries."""
from contextlib import nullcontext
import http.client
import io
import ipaddress
import socket
import ssl
from threading import Event, Thread
from types import SimpleNamespace
import zlib
from urllib.parse import urlencode, urljoin, urlsplit

import httpx
import brotli

from genesisai.shared.security import ToolError
from genesisai.shared.run_control import current_run
from genesisai.core.extensions.tools.web.contracts import canonicalize_url
from .budget import NetworkLimits, active_limits, active_scope, network_budget, remaining, pause

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def public_ip(value):
    ip = ipaddress.ip_address(value)
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def normalized_host(value):
    value = value.lower().rstrip('.')
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value.encode('idna').decode()


class Network:
    canonicalize = staticmethod(canonicalize_url)
    address_failure = ('unsafe_url', '禁止私网、保留地址或混合 DNS 结果')

    def __init__(self, resolver=None, transport=None, *, limits=None, scope=None):
        self.resolver = resolver or socket.getaddrinfo
        self.transport = transport or self._request
        self.default_limits = NetworkLimits.parse(limits)
        self.scope = scope
        self.extra_scopes = ()

    @property
    def limits(self):
        return active_limits.get() or self.default_limits

    def resolve(self, url):
        url = self.canonicalize(url)
        parts = urlsplit(url)
        for scope in (self.scope, active_scope.get(), *self.extra_scopes):
            if scope is None:
                continue
            host = normalized_host(parts.hostname)
            domains = scope.get('domains') if isinstance(scope, dict) else None
            if not isinstance(domains, list) or not domains or any(not isinstance(value, str) for value in domains):
                raise ToolError('permission_denied', '网络范围授权无效')
            def matches(value):
                if value == '*':
                    return True
                if value.startswith('*.'):
                    suffix = normalized_host(value[2:])
                    return host.endswith('.' + suffix)
                return host == normalized_host(value)
            if not any(matches(value) for value in domains):
                raise ToolError('permission_denied', '请求域名超出当前范围授权')
        completed, result = Event(), []
        def resolve():
            try:
                result.append(self.resolver(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80), type=socket.SOCK_STREAM))
            except Exception as exc:
                result.append(exc)
            finally:
                completed.set()
        Thread(target=resolve, daemon=True).start()
        while not completed.wait(.05):
            remaining()
        remaining()
        if isinstance(result[0], Exception):
            raise result[0]
        addresses = list(dict.fromkeys(record[4][0] for record in result[0]))
        if not addresses or any(not self.allowed_address(ip) for ip in addresses):
            raise ToolError(*self.address_failure)
        return url, addresses[0]

    def allowed_address(self, value):
        return public_ip(value)

    def _request(self, url, ip, headers):
        parsed = urlsplit(url)
        timeout = min(self.limits.io_seconds, remaining())
        connection = (http.client.HTTPSConnection(parsed.hostname, port=parsed.port, context=ssl.create_default_context(), timeout=timeout)
                      if parsed.scheme == 'https' else http.client.HTTPConnection(parsed.hostname, port=parsed.port, timeout=timeout))
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection((ip, address[1]), timeout, source_address)
        def interrupt():
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        control = current_run.get()
        try:
            with control.interruptible(interrupt) if control else nullcontext():
                connection.request('GET', parsed.path + ('?' + parsed.query if parsed.query else ''), headers=headers)
                if connection.sock:
                    connection.sock.settimeout(min(self.limits.io_seconds, remaining()))
                response = connection.getresponse()
                content = io.BytesIO()
                while True:
                    timeout = min(self.limits.io_seconds, remaining())
                    stream_socket = connection.sock or getattr(getattr(response.fp, 'raw', None), '_sock', None)
                    if stream_socket:
                        stream_socket.settimeout(timeout)
                    piece = response.read1(min(65536, self.limits.response_bytes + 1 - content.tell()))
                    if not piece:
                        break
                    content.write(piece)
                    if content.tell() > self.limits.response_bytes:
                        raise ToolError('size_limit', '响应超过配置的单资源字节上限')
                return response.status, dict(response.getheaders()), content.getvalue()
        finally:
            connection.close()

    def _decode(self, body, headers):
        encoding = headers.get('content-encoding', 'identity').lower()
        maximum = self.limits.decoded_bytes
        if encoding in {'gzip', 'deflate'}:
            if not body:
                raise ToolError('invalid_response', '压缩正文不完整')
            decoded = bytearray()
            while body:
                remaining()
                decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15)
                decoded.extend(decoder.decompress(body, maximum + 1 - len(decoded)))
                if len(decoded) > maximum or decoder.unconsumed_tail:
                    raise ToolError('size_limit', '解压正文超过配置上限')
                if not decoder.eof:
                    raise ToolError('invalid_response', '压缩正文不完整')
                body = decoder.unused_data
                if body and encoding != 'gzip':
                    raise ToolError('invalid_response', '压缩正文含未处理尾部')
            body = bytes(decoded)
        elif encoding == 'br':
            decoder, decoded = brotli.Decompressor(), bytearray()
            for offset in range(0, len(body), 256):
                remaining()
                chunk = body[offset:offset + 256]
                while True:
                    decoded.extend(decoder.process(chunk, output_buffer_limit=maximum + 1 - len(decoded)))
                    if len(decoded) > maximum:
                        raise ToolError('size_limit', '解压正文超过配置上限')
                    if decoder.can_accept_more_data():
                        break
                    remaining()
                    chunk = b''
            if not decoder.is_finished():
                raise ToolError('invalid_response', '压缩正文不完整')
            body = bytes(decoded)
        elif encoding not in {'', 'identity'}:
            raise ToolError('unsupported_encoding', '不支持的响应编码')
        if len(body) > maximum:
            raise ToolError('size_limit', '正文超过配置的解码上限')
        return body

    def get(self, url, params=None, headers=None):
        if params:
            url += ('&' if '?' in url else '?') + urlencode(params)
        request_headers = {'User-Agent': 'GenesisAI/0.1 public-research', 'Accept-Encoding': 'identity', **(headers or {})}
        redirects, retries, attempts = 0, 0, 0
        with network_budget(self.limits):
            while True:
                remaining()
                attempts += 1
                try:
                    url, ip = self.resolve(url)
                    status, response_headers, body = self.transport(url, ip, request_headers)
                except ssl.SSLCertVerificationError:
                    raise ToolError('invalid_response', 'TLS 证书验证失败')
                except (OSError, http.client.HTTPException) as exc:
                    pause(self.limits.retry().delay(retries, exc))
                    retries += 1
                    continue
                remaining()
                if len(body) > self.limits.response_bytes:
                    raise ToolError('size_limit', '响应超过配置的单资源字节上限')
                normalized = {key.lower(): value for key, value in response_headers.items()}
                if status in RETRY_STATUS:
                    error = SimpleNamespace(response=SimpleNamespace(headers=normalized))
                    pause(self.limits.retry().delay(retries, error))
                    retries += 1
                    continue
                if status in {301, 302, 303, 307, 308}:
                    if redirects >= self.limits.max_redirects:
                        raise ToolError('redirect_limit', '重定向次数超过配置上限')
                    if not normalized.get('location'):
                        raise ToolError('invalid_response', '重定向缺少 Location')
                    target = self.canonicalize(urljoin(url, normalized['location']))
                    if urlsplit(target)[:2] != urlsplit(url)[:2]:
                        request_headers = {key: value for key, value in request_headers.items()
                                           if key.lower() in {'user-agent', 'accept', 'accept-encoding', 'accept-language'}}
                    url, redirects = target, redirects + 1
                    continue
                try:
                    body = self._decode(body, normalized)
                except (zlib.error, brotli.error) as exc:
                    raise ToolError('invalid_response', '压缩正文无效') from exc
                clean_headers = {key: value for key, value in response_headers.items()
                                 if key.lower() not in {'content-encoding', 'content-length'}}
                return httpx.Response(status, headers=clean_headers, content=body, request=httpx.Request('GET', url),
                                      extensions={'attempts': attempts, 'redirects': redirects})

    def close(self):
        pass

    def fetch(self, url, *, limits=None):
        token = active_limits.set(NetworkLimits.parse(limits) if limits is not None else self.limits)
        try:
            with network_budget(self.limits):
                return self._fetch(url)
        finally:
            active_limits.reset(token)

    def _fetch(self, url):
        return self.extract(self.get(url))

    def extract(self, response):
        if response.status_code != 200:
            raise ToolError('fetch_failed', f'HTTP {response.status_code}', response.status_code in RETRY_STATUS)
        media = response.headers.get('content-type', '').lower()
        links, blocks, pages = [], [], []
        source_text = None
        if 'pdf' in media:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(response.content))
            for number, page in enumerate(reader.pages, 1):
                remaining()
                pages.append({'page': number, 'text': page.extract_text() or ''})
            text, title = '\n'.join(page['text'] for page in pages), str(response.url)
        elif 'html' in media:
            import trafilatura
            from .links import discover_links
            from .structured_data import extract_structured_blocks, product_summary
            source_text = response.text
            links = discover_links(source_text, str(response.url), canonicalize=self.canonicalize)
            blocks = extract_structured_blocks(source_text)
            text = trafilatura.extract(source_text, include_tables=True) or ''
            facts = product_summary(blocks)
            if facts:
                text = 'Structured product data:\n' + facts + '\n\n' + text.lstrip()
            metadata = trafilatura.extract_metadata(source_text)
            title = metadata.title if metadata and metadata.title else str(response.url)
        elif media.startswith('text/plain'):
            source_text = text = response.text
            title = str(response.url)
        else:
            raise ToolError('unsupported_format', '不支持该网络资源类型')
        remaining()
        return dict(url=str(response.url), title=title, text=text, truncated=False, links=links,
                    structured_blocks=blocks, document_pages=pages, source_text=source_text,
                    raw_content=response.content, media_type=media, request=response.extensions,
                    extraction={'complete': True, 'has_text': bool(text.strip()),
                                'needs': None if text.strip() else 'browser_render_or_document_ocr'})
