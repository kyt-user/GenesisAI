"""Local process fixture; records lifecycle and speaks framed JSON-RPC."""
import json
import os
from pathlib import Path
import sys
import time

options = json.loads(Path(sys.argv[1]).read_text())
roots, documents = [], {}

def send(identity, result):
    raw = json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': result}).encode()
    sys.stdout.buffer.write(f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
    sys.stdout.buffer.flush()

while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    size = int(line.split(b':')[1])
    assert sys.stdin.buffer.readline() == b'\r\n'
    message = json.loads(sys.stdin.buffer.read(size))
    method = message['method']
    with Path(options['log']).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'pid': os.getpid(), 'time': time.monotonic(), 'method': method, 'params': message.get('params')}) + '\n')
    if method == 'initialize':
        time.sleep(options.get('startup_delay', 0))
        roots = message['params']['workspaceFolders']
        send(message['id'], {'capabilities': {'definitionProvider': True, 'referencesProvider': True,
            'diagnosticProvider': {}, 'workspace': {'workspaceFolders': {'supported': True}}}})
    elif method == 'textDocument/didOpen':
        document = message['params']['textDocument']
        documents[document['uri']] = document['text']
    elif method in ('textDocument/definition', 'textDocument/references', 'textDocument/diagnostic'):
        time.sleep(options.get('query_delay', 0))
        uri = message['params']['textDocument']['uri']
        region = {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 1}}
        if method == 'textDocument/diagnostic':
            send(message['id'], {'kind': 'full', 'items': [{'range': region, 'message': documents[uri], 'severity': 2}]})
        else:
            send(message['id'], [{'uri': options['targets'][i % len(options['targets'])], 'range': region} for i in range(257)]
                 + [{'uri': 'https://example.invalid/remote', 'range': region}, {'uri': options['outside'], 'range': region}])
