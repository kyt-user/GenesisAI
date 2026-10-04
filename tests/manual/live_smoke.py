"""显式执行公网与模型探测，只使用脚本生成的测试资料。"""
import argparse
import json
import time
import hashlib
from pathlib import Path
from genesisai.app.cli import load_environment

from genesisai.model.config import build_model
from genesisai.agent.runner import Runner
from genesisai.shared.security import Access
from genesisai.core.state.store import HostLock, Store, atomic_json
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.core.extensions.tools.web.network import Network
from genesisai.core.extensions.tools.web.providers.duckduckgo import DuckDuckGoSearchProvider
from genesisai.core.extensions.tools.web.providers.bing import BingSearchProvider
from genesisai.core.extensions.tools.web.contracts import SearchQuery


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,help='模型 YAML 配置路径')
    parser.add_argument('--network-only',action='store_true',help='只检查公开网络，不调用模型')
    parser.add_argument('--allow-remote-fixtures',action='store_true',help='明确允许将脚本生成的测试资料发送给远程模型')
    args=parser.parse_args()
    load_environment()
    root=Path(__file__).resolve().parents[2]/'workspace'/('live-'+str(int(time.time())))
    root.mkdir(parents=True)
    report={'directory':str(root),'network':{},'model':[]}
    network=Network()
    for name,action in {
        'fetch':lambda:network.fetch('https://www.python.org/about/'),
        'query':lambda:[h.to_dict() for h in DuckDuckGoSearchProvider(client=network).search(SearchQuery('Python official about'),limit=3)],
        'bing_query':lambda:[h.to_dict() for h in BingSearchProvider(client=network).search(SearchQuery('Python official about'),limit=3)]
    }.items():
        try:
            result=action()
            report['network'][name]={'status':'passed' if result else 'failed','result':result}
        except Exception as exc:
            report['network'][name]={'status':'failed','error':type(exc).__name__,'message':str(exc)[:200]}
    atomic_json(root/'report.json',report)
    print(json.dumps({k:{'status':v['status']} for k,v in report['network'].items()}),flush=True)
    if not args.network_only:
        inputs=root/'inputs'; inputs.mkdir(); (inputs/'notes.md').write_text('Python is a programming language. Internal fixture identifier UNIQUE_LOCAL_42.',encoding='utf-8')
        prompts=[
            'Read notes.md from the authorized input directory. Copy it into sorted/notes.md and create a short Markdown index index.md with the registered local source citation. Keep originals unchanged.',
            'Search the public web for Python official information, fetch an official result, and write a short report web.md with registered source references. Do not use search snippets as fetched evidence.',
            'Read notes.md, search for Python official information and fetch an official page. Combine local fixture and public information in fusion.md with both source references. Keep sources distinct.'
        ]
        for number,prompt in enumerate(prompts,1):
            work=root/f'case-{number}'; work.mkdir()
            try:
                model,remote=build_model(args.config)
                if remote and not args.allow_remote_fixtures:
                    raise ValueError('Remote fixtures require --allow-remote-fixtures')
                with HostLock(work):
                    store=Store(work); output=work/'outputs'
                    store.data.update(grants=[str(inputs)],output=str(output));store.save()
                    runtime=ToolRuntime(store,Access([inputs],output,work),confirm_writes=False,confirm_search=False)
                    outcome=Runner(model,runtime,seconds=600,max_rounds=20).start(prompt)
                    status=outcome['status']
                    checks={}
                    calls=[item['call']['name'] for item in store.data['calls'].values()]
                    if number==1:
                        copied=output/'sorted/notes.md'; index=output/'index.md'
                        checks=dict(used_file_copy='file_copy' in calls,
                                    exact_copy=copied.is_file() and hashlib.sha256(copied.read_bytes()).digest()==hashlib.sha256((inputs/'notes.md').read_bytes()).digest(),
                                    index_exists=index.is_file())
                    elif number==2:
                        checks=dict(report_exists=(output/'web.md').is_file(),web_source=any(v['kind']=='web' for v in store.data['sources'].values()))
                    else:
                        kinds={v['kind'] for v in store.data['sources'].values()}
                        checks=dict(report_exists=(output/'fusion.md').is_file(),local_and_web={'file','web'}.issubset(kinds))
                    if status=='completed' and not all(checks.values()):
                        status='failed_acceptance'
                    report['model'].append(dict(case=number,status=status,runner_status=outcome['status'],checks=checks,answer=outcome['answer'],calls=calls,artifacts=store.data['artifacts'],sources=store.data['sources']))
            except Exception as exc:
                report['model'].append(dict(case=number,status='failed',error=type(exc).__name__,message=str(exc)[:200]))
            atomic_json(root/'report.json',report)
            print('case',number,report['model'][-1]['status'],flush=True)
    print(str(root/'report.json'),flush=True)


if __name__=='__main__': main()
