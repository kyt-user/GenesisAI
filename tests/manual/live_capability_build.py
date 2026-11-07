"""Opt-in real-model acceptance using generated assets and isolated state only."""

import argparse
import json
from pathlib import Path
import time

from genesisai.agent.loop import Runner
from genesisai.app.cli import load_environment
from genesisai.core.persistence.session import Session
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.model.config import build_model
from genesisai.model.settings import ProviderSettings, load_settings
from genesisai.model.providers.catalog import get_provider
from genesisai.shared.ids import uid
from genesisai.shared.security import Access
from genesisai.shared.run_control import RunControl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", required=True, action="store_true")
    parser.add_argument('--model', help='Qwen Token Plan 模型；默认使用已保存的套餐模型或套餐默认模型')
    parser.add_argument('--seconds', type=float, default=1800, help='本次真实验收的显式总时限（秒）')
    parser.add_argument('--token-budget', type=int, default=300000, help='本次运行的累计 Token 预算')
    parser.add_argument('--resume', type=Path, help='继续本脚本生成的 Qwen 验收目录，保留原报告')
    args = parser.parse_args()
    if args.seconds <= 0 or args.token_budget <= 0:
        parser.error('--seconds 和 --token-budget 必须为正数')
    load_environment()
    temp_root = Path(__file__).resolve().parents[2] / "temp"
    root = args.resume.resolve() if args.resume else temp_root / uid("live_capability")
    prior = None
    if args.resume:
        if not root.is_relative_to(temp_root.resolve()) or not root.name.startswith('live_capability_'):
            parser.error('--resume 仅接受本工作区 temp 下的隔离验收目录')
        prior = json.loads((root / 'report.json').read_text(encoding='utf-8'))
        if prior.get('provider') != 'qwen_token_plan':
            parser.error('仅续跑 Qwen Token Plan 验收')
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=bool(prior))
    session = Session(root / "state", prior['session_id']) if prior else Session(root / "state", workspace=workspace)
    session.record.grants, session.record.output = [str(workspace)], str(workspace)
    session.commit()
    runtime = ToolRuntime(session, Access([workspace], workspace, session.root, workspace_root=workspace), permission_mode="auto", providers=[])
    settings = load_settings()
    if settings is None or settings.provider != 'qwen_token_plan':
        settings = ProviderSettings(provider='qwen_token_plan', model=get_provider('qwen_token_plan').default_model,
                                    generation={'max_tokens': 8192})
    if args.model or prior:
        from dataclasses import replace
        settings = replace(settings, model=args.model or prior['model'])
    model, _ = build_model(settings=settings)
    report = {"directory": str(root), "session_id": session.id, "provider": model.provider, "model": model.model, "started": time.time()}
    report_path = root / (uid('report_resume') + '.json' if prior else 'report.json')
    if prior:
        report['continued_from'] = str(root / 'report.json')

    def progress(kind, payload):
        if kind == "model":
            print("model_round", payload.get("round"), flush=True)
        elif kind == "tool_result":
            print("tool_result", payload.get("call_id"), payload.get("ok"), (payload.get("error") or {}).get("code"), flush=True)

    try:
        runner = Runner(model, runtime, seconds=args.seconds, max_rounds=40, max_tools=100, token_budget=args.token_budget, on_event=progress)
        instruction = (
            "这是隔离目录中的真实能力构建验收。请创建可复用 Tool，名称 double_integer，接收 n（整数），返回对象 result=2*n。"
            "用 Python 运行器实现，从 stdin 读 JSON，将结果 JSON 写入环境变量 GENESISAI_RESULT_PATH 指向的文件，stdout/stderr 只作日志。源文件先写到工作区，再按 path 引用导入。"
            "创建草稿、冻结版本、实际执行至少两个测试（正数和负数），根据真实结果修复直到通过。"
            "本轮目标仅为测试通过，不申请正式注册。不要联网检索、删除文件或执行 Git 操作。最后说明测试结果。"
        )
        if prior:
            instruction = ('继续构建刚才的可复用 Tool double_integer 并完成验收，仅测试通过，不申请正式注册。'
                           '先检查已有草稿；已有草稿时沿用当前修订和真实测试报告，避免重复创建。'
                           '若尚无草稿，按原任务继续创建、冻结并运行至少正数和负数两例实际测试。'
                           '若冻结版本测试已经通过，不要再次test_capability；完成剩余计划并如实总结结果。'
                           '需要验证的验收项必须引用实际验证调用；若包外辅助源码尚缺检查，可用run_commands执行针对性语法检查并设置verification=static。')
        result = RunControl(progress).execute(runner.resume if prior and session.record.status == "interrupted" else lambda: runner.start(instruction))
        report["outcome"] = result
        drafts = runtime.capabilities.store.list()
        checks = {"completed": result["status"] == "completed", "single_draft": len(drafts) == 1}
        report["checks"] = checks
        if len(drafts) == 1:
            item = drafts[0]
            report["capability"] = runtime.capabilities.summary(item)
            checks.update(frozen=item["frozen"], actual_tests_passed=bool(item["report"] and item["report"]["passed"]),
                          at_least_two_cases=bool(item["report"] and len(item["report"]["cases"]) >= 2), not_registered=item["registration"] is None)
            trial_args = {"id": item["id"], "revision": item["revision"], "digest": item["digest"], "input": {"n": 9}}
            if item["frozen"]:
                report["independent_trial"] = runtime.capabilities.try_draft(trial_args)
                if report['independent_trial'].get('job_id'):
                    job_id = report['independent_trial']['job_id']
                    trial_deadline = time.monotonic() + 60
                    while runtime.jobs.wait(job_id, 5)['status'] in {'queued', 'running', 'stopping'}:
                        print('independent_trial_running', job_id, flush=True)
                        if time.monotonic() >= trial_deadline:
                            runtime.jobs.stop(job_id)
                            raise TimeoutError('独立整数用例超过本验收的60秒预算')
                    report['independent_trial'] = runtime.jobs.operation_result(job_id)
                checks["independent_result"] = report["independent_trial"]["result"]["value_included"] and report["independent_trial"]["result"]["value"] == {"result": 18}
            else:
                checks["independent_result"] = False
            reopened = Session(session.root, session.id)
            checks["restored_session"] = reopened.record.last_run_outcome["status"] == "completed"
        report["checks"] = checks
        report["passed"] = all(checks.values())
    except BaseException as exc:
        report.update(passed=False, error=runtime.logger.journal.clean(str(exc)))
    finally:
        report["finished"] = time.time()
        report["calls"] = [{"tool": (entry.get("call") or {}).get("name"), "state": entry.get("state"),
                            "error": ((entry.get("result") or {}).get("error") or {}).get("code")} for entry in session.record.calls.values()]
        report_path.write_text(json.dumps(runtime.logger.journal.clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
        runtime.close()
        print(json.dumps({"report": str(report_path), "passed": report["passed"], "checks": report.get("checks"), "error": report.get("error")}, ensure_ascii=False), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
