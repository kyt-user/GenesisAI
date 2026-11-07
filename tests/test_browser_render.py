"""Actual Chromium executes JavaScript; all HTTP is supplied by a pinned fixture."""
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest

from genesisai.agent.evidence import EvidenceBundle
from genesisai.core.extensions.tools.web.network import Network
from test_p11_agent_runtime import runtime_env
from test_p12_runtime import call
from test_web_resources import public


@pytest.fixture
def browser(runtime_env, monkeypatch):
    pytest.importorskip('playwright')
    installed = Path(__file__).resolve().parents[1] / 'temp' / 'playwright-browsers'
    if installed.is_dir():
        monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', str(installed))
    store, runtime = runtime_env
    log = store.root / 'render-network.jsonl'
    def transport(url, ip, headers):
        assert ip == '93.184.216.34'
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(url) + '\n')
        path = urlsplit(url).path
        if path == '/redirect':
            return 302, {'location': '/article'}, b''
        if path == '/escape':
            return 302, {'location': 'http://127.0.0.1/private'}, b''
        if path == '/article':
            return 200, {'content-type': 'text/html'}, b'''<html><head><title>Dynamic evidence</title></head><body>
            <article id="body">Loading</article><script src="/app.js"></script></body></html>'''
        if path == '/app.js':
            return 200, {'content-type': 'application/javascript'}, b'''
            fetch('/data.json').then(r=>r.json()).then(data=>{
              document.querySelector('#body').innerHTML='<p id="ready">'+data.text+'</p><a href="/more">More</a>';
            });
            fetch('https://outside.test/leak').catch(()=>{});
            fetch('http://127.0.0.1/private').catch(()=>{});
            fetch('/write', {method:'POST', body:'no'}).catch(()=>{});
            new WebSocket('wss://public.test/socket');
            '''
        if path == '/data.json':
            return 200, {'content-type': 'application/json'}, json.dumps({'text': 'DYNAMIC_RESULT_6192. ' + 'This article was rendered by a real browser from a JavaScript response. ' * 20}).encode()
        if path == '/wait':
            return 200, {'content-type': 'text/html'}, b'<html><body><article>Waiting forever for a missing selector.</article></body></html>'
        raise AssertionError('Unexpected direct request: ' + url)
    runtime.network = Network(resolver=public, transport=transport)
    return store, runtime, log


def finish(runtime, result):
    assert result['ok'], result
    while result['data'].get('status') in {'queued', 'running', 'stopping'}:
        result = runtime.execute(call('read_job', {'job_id': result['data']['job_id'], 'wait_seconds': 20}, 'poll_' + str(time.monotonic_ns())))
        assert result['ok'], result
    return result['data'].get('result', result['data'])


def test_real_browser_dynamic_snapshot_redirect_cache_and_boundaries(browser):
    store, runtime, log = browser
    args = {'url': 'https://public.test/redirect', 'wait_for': '#ready', 'settle_seconds': .3}
    data = finish(runtime, runtime.execute(call('render_web_page', args, 'render')))
    assert 'DYNAMIC_RESULT_6192' in data['text']
    assert data['url'] == 'https://public.test/article'
    assert not data['extraction']['complete']
    reasons = {item['reason'] for item in data['extraction']['blocked']}
    assert {'permission_denied', 'method', 'unsupported'} <= reasons
    requests = log.read_text().splitlines()
    assert len(requests) == 4
    assert all('outside.test' not in value and '127.0.0.1' not in value for value in requests)
    ref = data['source_refs'][0]
    assert store.record.sources[ref]['kind'] == 'web_render'
    assert b'DYNAMIC_RESULT_6192' in (store.root / 'sources' / (ref + '.bin')).read_bytes()
    EvidenceBundle(store).verify()
    cached = finish(runtime, runtime.execute(call('render_web_page', args, 'cached')))
    assert cached['cached'] and log.read_text().splitlines() == requests


def test_main_redirect_to_private_is_rejected_before_browser(browser):
    _, runtime, log = browser
    result = runtime.execute(call('render_web_page', {'url': 'https://public.test/escape'}, 'escape'))
    if result['ok']:
        job_id = result['data']['job_id']
        result = runtime.execute(call('read_job', {'job_id': job_id, 'wait_seconds': 20}, 'escape_result'))
    assert not result['ok'], result
    assert len(log.read_text().splitlines()) == 1


def test_browser_wait_can_be_stopped_without_an_execution_replay(browser):
    _, runtime, log = browser
    result = runtime.execute(call('render_web_page', {'url': 'https://public.test/wait', 'wait_for': '#never', 'network_limits': {'total_seconds': 60}}, 'wait'))
    assert result['ok'] and result['data']['status'] == 'running', result
    job_id = result['data']['job_id']
    result = runtime.execute(call('stop_job', {'job_id': job_id}, 'stop'))
    assert result['ok'], result
    end = time.monotonic() + 10
    while True:
        result = runtime.execute(call('read_job', {'job_id': job_id, 'wait_seconds': 1}, 'stop_poll_' + str(time.monotonic_ns())))
        if result['data']['status'] not in {'running', 'stopping'}:
            break
        assert time.monotonic() < end, result
    assert result['data']['status'] == 'stopped', result
    assert len(log.read_text().splitlines()) == 1
