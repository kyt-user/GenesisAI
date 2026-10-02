import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesisai.app.cli import main
from genesisai.shared.messages import Response
from genesisai.state.store import Store
from genesisai.capabilities.web.providers.brave import BraveSearchProvider
from genesisai.capabilities.web.providers.duckduckgo import DuckDuckGoSearchProvider
from genesisai.capabilities.web.contracts import SearchQuery, SearchError
from test_acceptance import FakeModel, call, response


def test_A19_cli_commands_and_multi_turn(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    model=FakeModel(Response(content='First'),Response(content='Second'))
    monkeypatch.setattr(cli,'build_model',lambda p:(model,False))
    commands=iter(['/help','hello','/status','/history','again','/exit'])
    monkeypatch.setattr('rich.console.Console.input',lambda *a,**k:next(commands))
    assert main(['--workspace',str(tmp_path/'work')])==0
    state=json.loads(next((tmp_path/'work/.genesis/sessions').glob('*.json')).read_text(encoding='utf-8'))
    assert state['answer']=='Second' and len(model.contexts)==2
    assert any(m.content=='First' for m in model.contexts[1])


def test_A24_remote_denied_before_model(tmp_path, monkeypatch):
    import genesisai.app.cli as cli
    model=FakeModel()
    monkeypatch.setattr(cli,'build_model',lambda p:(model,True))
    assert main(['--workspace',str(tmp_path/'work'),'--prompt','private'])==2
    assert not model.contexts


def test_A11_junction(tmp_path):
    if os.name!='nt':
        pytest.skip('Windows junction case')
    from genesisai.shared.security import Access, ToolError
    authorized=tmp_path/'in'; outside=tmp_path/'private'; output=tmp_path/'out'
    authorized.mkdir(); outside.mkdir(); (outside/'secret.txt').write_text('secret')
    junction=authorized/'link'
    # 这里只创建指向固定测试路径的联接，绝不通过该联接删除内容。
    result=subprocess.run(['cmd','/c','mklink','/J',str(junction),str(outside)],capture_output=True)
    assert result.returncode==0, 'Junction creation unavailable: this required case is not verified'
    access=Access([authorized],output,tmp_path/'work')
    with pytest.raises(ToolError): access.read(str(junction/'secret.txt'))
    # 原生解除联接只删除联接本身，不会删除目标目录树。
    os.rmdir(junction)
    assert (outside/'secret.txt').exists()


def test_A11_symlink(tmp_path):
    from genesisai.shared.security import Access, ToolError
    authorized=tmp_path/'in'; outside=tmp_path/'private'
    authorized.mkdir(); outside.mkdir(); (outside/'secret.txt').write_text('secret')
    link=authorized/'link'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Host does not permit symlink creation; junction tested separately')
    access=Access([authorized],tmp_path/'out',tmp_path/'work')
    with pytest.raises(ToolError): access.read(str(link/'secret.txt'))
    link.unlink()


def test_A15_provider_parsing_and_rate_limit():
    import httpx
    def duck(request):
        return httpx.Response(200,text='<a class="result__a" href="https://example.com/">Example</a>')
    client=httpx.Client(transport=httpx.MockTransport(duck))
    assert DuckDuckGoSearchProvider(client=client).search(SearchQuery('example'))[0].title=='Example'
    client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(429)))
    with pytest.raises(SearchError) as error:
        BraveSearchProvider('fake',client=client).search(SearchQuery('example'))
    assert error.value.error_type=='provider_rate_limited'


def test_A02_package_imports():
    import ast
    root=Path(__file__).resolve().parents[1]/'src/genesisai'
    for path in root.rglob('*.py'):
        tree=ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom) and node.module:
                assert node.module.split('.')[0] not in {'runtime','core','capabilities','agents','xiaoerAI','Agent_lib'}
        assert 'sys.path' not in path.read_text(encoding='utf-8')


def test_A03_source_baseline():
    root=Path(__file__).resolve().parents[1]
    baseline=json.loads((root/'docs/SOURCE_BASELINE.json').read_text())
    for relative,expected in baseline.items():
        source=root.parent/'xiaoerAI'/relative
        assert source.exists() and hashlib.sha256(source.read_bytes()).hexdigest()==expected, relative


def test_A01_module_help(tmp_path):
    process=subprocess.run([sys.executable,'-m','genesisai','--help'],cwd=tmp_path,capture_output=True)
    assert process.returncode==0 and b'--workspace' in process.stdout


def test_A01_direct_cli_help():
    root=Path(__file__).resolve().parents[1]
    process=subprocess.run([sys.executable,str(root/'cli.py'),'--help'],cwd=root,capture_output=True)
    assert process.returncode==0 and b'--workspace' in process.stdout


def test_bing_rss_parser():
    import httpx
    from genesisai.capabilities.web.providers.bing import BingSearchProvider
    client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'<rss><channel><item><title>Python</title><link>https://www.python.org/</link><description>Official</description></item></channel></rss>')))
    hit=BingSearchProvider(client).search(SearchQuery('python'))[0]
    assert hit.provider=='bing_rss' and hit.url=='https://www.python.org/'


def test_shared_workspace_env_precedes_project_env(tmp_path,monkeypatch):
    import genesisai.app.cli as cli
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    shared=tmp_path/'shared.env'; shared.write_text('DEEPSEEK_API_KEY=shared\n',encoding='utf-8')
    cli.load_environment(shared)
    assert os.environ['DEEPSEEK_API_KEY']=='shared'


def test_default_model_uses_project_config(monkeypatch):
    from genesisai.model import config
    captured={}
    class Client:
        def __init__(self,**kwargs):
            captured.update(kwargs)
            self.client=type('OpenAI',(),{'base_url':'https://api.deepseek.com'})()
    monkeypatch.setattr('genesisai.model.providers.deepseek.DeepSeekClient',Client)
    monkeypatch.setenv('DEEPSEEK_API_KEY','configured-for-test')
    client,remote=config.build_model()
    assert captured['model']=='deepseek-v4-flash' and remote
    assert captured['generation']=={'temperature':0.6,'max_tokens':2048}


def test_model_yaml_rejects_unknown_and_invalid_generation(tmp_path):
    from genesisai.model.config import load_model_config
    base='''provider: ollama
model: local-model
generation:
  temperature: 0.2
  max_tokens: 2048
'''
    valid=tmp_path/'valid.yaml'; valid.write_text(base,encoding='utf-8')
    assert load_model_config(valid).provider == 'ollama'
    unknown=tmp_path/'unknown.yaml'; unknown.write_text(base+'surprise: true\n',encoding='utf-8')
    with pytest.raises(ValueError,match='未知字段'):
        load_model_config(unknown)
    invalid=tmp_path/'invalid.yaml'; invalid.write_text(base.replace('temperature: 0.2','temperature: 3'),encoding='utf-8')
    with pytest.raises(ValueError,match='超出范围'):
        load_model_config(invalid)


def test_model_error_is_actionable():
    from genesisai.agent.runner import Runner
    assert '模型服务内部错误' in Runner.model_error(type('InternalServerError',(Exception,),{})())
    assert '无法连接模型服务' in Runner.model_error(type('APIConnectionError',(Exception,),{})())

