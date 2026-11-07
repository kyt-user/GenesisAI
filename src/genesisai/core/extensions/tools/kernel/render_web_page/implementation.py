"""Explicit JavaScript snapshot with every HTTP read mediated by pinned transport."""
import asyncio
from copy import copy
from urllib.parse import urlsplit

import httpx

from genesisai.core.extensions.tools.kernel.fetch_web_content.implementation import Implementation as PublicReader
from genesisai.core.extensions.tools.web.budget import remaining
from genesisai.core.extensions.tools.web.network import normalized_host
from genesisai.shared.security import ToolError, digest


class Implementation(PublicReader):
    name = 'render_web_page'
    source_kind = 'web_render'

    def prepare(self, context, args):
        preview = super().prepare(context, args)
        self.canonicalize(args['url'])
        for domain in args.get('resource_domains', []):
            if domain != '*' and (':' in domain or '/' in domain or any(c.isspace() for c in domain)):
                raise ToolError('validation_error', 'resource_domains 需要主机名、*.子域或*')
        return {**preview, 'javascript': True, 'methods': ['GET'],
                'resource_domains': [urlsplit(args['url']).hostname, *args.get('resource_domains', [])]}

    def execute(self, context, args):
        self.render_options = {key: args.get(key) for key in (
            'resource_domains', 'wait_for', 'settle_seconds', 'max_requests', 'max_total_bytes')}
        result = super().execute(context, args)
        if result.get('read_more'):
            result['read_more'].update({key: value for key, value in self.render_options.items() if value is not None})
        return result

    def cache_key(self):
        return digest(self.render_options)

    def fetch_source(self, context, url, args):
        from playwright.async_api import async_playwright, Error
        network = copy(context.network)
        domains = [normalized_host(urlsplit(url).hostname), *args.get('resource_domains', [])]
        network.extra_scopes = (*network.extra_scopes, {'domains': domains})
        requests, blocked, failures = [], [], []
        total_bytes = 0
        max_requests = args.get('max_requests', 256)
        max_bytes = args.get('max_total_bytes', 64 * 1024**2)

        async def render():
            nonlocal total_bytes
            # Resolve the initial navigation through the pinned client. Playwright
            # routes do not intercept every native redirect hop.
            initial = await asyncio.to_thread(network.get, url)
            initial_url = str(initial.url)
            initial_pending = True
            async with async_playwright() as playwright:
                try:
                    browser = await playwright.chromium.launch(headless=True,
                        proxy={'server': 'http://127.0.0.1:9', 'bypass': ''},
                        args=['--proxy-bypass-list=<-loopback>', '--disable-quic', '--disable-background-networking',
                              '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                              '--host-resolver-rules=MAP * ~NOTFOUND'])
                except Error as exc:
                    raise ToolError('browser_unavailable', 'Chromium 未安装或无法启动；安装 genesisai[browser] 并执行 python -m playwright install chromium') from exc
                try:
                    browser_context = await browser.new_context(service_workers='block', accept_downloads=False)
                    async def route_request(route):
                        nonlocal total_bytes, initial_pending
                        request = route.request
                        entry = {'url': request.url, 'method': request.method}
                        if request.method != 'GET' or len(requests) >= max_requests:
                            blocked.append({**entry, 'reason': 'method' if request.method != 'GET' else 'request_quota'})
                            await route.abort('blockedbyclient')
                            return
                        requests.append(entry)
                        try:
                            if initial_pending and request.is_navigation_request() and request.url == initial_url:
                                initial_pending = False
                                response = initial
                            else:
                                response = await asyncio.to_thread(network.get, request.url)
                            entry['resolved_url'] = str(response.url)
                            if str(response.url) != request.url and request.resource_type == 'document':
                                raise ToolError('browser_navigation_redirect', '动态导航重定向请以 resolved_url 重新显式渲染')
                            if total_bytes + len(response.content) > max_bytes:
                                raise ToolError('size_limit', '浏览器子资源达到总字节配额')
                            total_bytes += len(response.content)
                            headers = {key: value for key, value in response.headers.items()
                                       if key.lower() not in {'content-length', 'content-encoding', 'transfer-encoding', 'connection', 'set-cookie'}}
                            await route.fulfill(status=response.status_code, headers=headers, body=response.content)
                        except (ToolError, RuntimeError, ValueError) as exc:
                            code = getattr(exc, 'error_type', 'fetch_failed')
                            blocked.append({**entry, 'reason': code})
                            failures.append(exc)
                            await route.abort('blockedbyclient')
                    async def websocket(route):
                        blocked.append({'url': route.url, 'method': 'WEBSOCKET', 'reason': 'unsupported'})
                        await route.close()
                    await browser_context.route('**/*', route_request)
                    await browser_context.route_web_socket('**/*', websocket)
                    page = await browser_context.new_page()
                    page.set_default_timeout(remaining() * 1000)
                    response = await page.goto(initial_url, wait_until='domcontentloaded', timeout=remaining() * 1000)
                    if response is None or response.status >= 400:
                        raise ToolError('fetch_failed', '浏览器主文档未成功读取')
                    if args.get('wait_for'):
                        await page.locator(args['wait_for']).first.wait_for(state='attached', timeout=remaining() * 1000)
                    await asyncio.sleep(args.get('settle_seconds', 1))
                    remaining()
                    dom = (await page.content()).encode('utf-8')
                    if len(dom) > max_bytes:
                        raise ToolError('size_limit', '渲染 DOM 达到字节配额')
                    return page.url, dom
                except Error as exc:
                    if failures:
                        raise failures[0]
                    raise ToolError('browser_render_failed', '浏览器渲染未完成：' + str(exc)) from exc
                finally:
                    await browser.close()

        async def bounded():
            try:
                return await asyncio.wait_for(render(), remaining())
            except TimeoutError as exc:
                raise ToolError('timeout', '浏览器渲染达到当前网络总时限') from exc
        final_url, dom = asyncio.run(bounded())
        result = network.extract(httpx.Response(200, content=dom, headers={'content-type': 'text/html; charset=utf-8'},
                                                request=httpx.Request('GET', final_url)))
        result['extraction'] = {'complete': False, 'has_text': bool(result['text'].strip()),
            'method': 'chromium_dom_snapshot', 'javascript': True,
            'resource_requests': len(requests), 'resources': requests, 'resource_bytes': total_bytes, 'blocked': blocked,
            'wait_for': args.get('wait_for'), 'settle_seconds': args.get('settle_seconds', 1),
            'limitations': ['仅表示当前等待条件下的DOM快照，不表示页面未来不会更新', '独立无登录浏览器；仅GET读取，不提交表单或访问WebSocket',
                             '子资源重定向由固定DNS客户端读取，浏览器看到原请求URL；动态文档跳转重定向会显式阻止']}
        return result
