"""可持久化的单智能体循环；研究、上下文和完成策略由独立确定性模块约束。"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from dataclasses import asdict

from genesisai.agent.completion import CompletionValidator
from genesisai.prompt.context_budgeter import ContextBudgeter, is_recall_request, select_initial_profile
from genesisai.agent.evidence import EvidenceBundle
from genesisai.shared.messages import ToolCall
from genesisai.state.trace import TraceRecorder
from genesisai.prompt.composer import PromptComposer
from genesisai.agent.research import ResearchController
from genesisai.state.store import uid
from genesisai.agent.state_machine import RunStateMachine, StateTransitionError
from genesisai.project_docs import AgentDocsManager


SOURCE_REF = re.compile(r"\b(src_[0-9a-f]{16})\b")
PRICE_CLAIM = re.compile(
    r"(?:(CNY|RMB|USD|EUR|GBP|JPY|HKD|AUD|CAD|人民币|美元|欧元|英镑|日元|港元|[¥￥$€£])\s*([0-9][0-9,.]*))"
    r"|(?:([0-9][0-9,.]*)\s*(CNY|RMB|USD|EUR|GBP|JPY|HKD|AUD|CAD|人民币|美元|欧元|英镑|日元|港元|元))",
    re.I,
)
CURRENCY_ALIASES = {
    'cny': 'CNY', 'rmb': 'CNY', '人民币': 'CNY', '元': 'CNY', '¥': 'CNY', '￥': 'CNY',
    'usd': 'USD', '美元': 'USD', '$': 'USD', 'eur': 'EUR', '欧元': 'EUR', '€': 'EUR',
    'gbp': 'GBP', '英镑': 'GBP', '£': 'GBP', 'jpy': 'JPY', '日元': 'JPY',
    'hkd': 'HKD', '港元': 'HKD', 'aud': 'AUD', 'cad': 'CAD',
}


class ContextLimitError(ValueError):
    """当前与上一轮必要上下文无法安全放入模型请求。"""


class Runner:
    """可持久化的单智能体 ReAct 循环。

    研究和完成策略由独立确定性模块约束；
    工具调用通过 ToolRuntime 执行，结果写回会话存储。
    """

    def __init__(self, model, runtime, *, max_rounds=20, max_tools=40, max_failures=3, seconds=300, on_event=None):
        self.model, self.runtime, self.store = model, runtime, runtime.store
        self.max_rounds, self.max_tools, self.max_failures, self.seconds = max_rounds, max_tools, max_failures, seconds
        self.on_event = on_event or (lambda *args: None)
        self.cancel_requested = False
        self._needs_verification = False
        self.composer = PromptComposer()
        self.context_budgeter = ContextBudgeter(self.store, self.composer)
        self.completion = CompletionValidator()
        self.trace = TraceRecorder(self.store)
        self.machine = RunStateMachine(self.store)

    @property
    def research(self) -> ResearchController:
        return self.runtime.research

    def status(self, value, answer=None):
        """更新并持久化运行状态，同时同步状态机转换。"""
        self.store.data['status'] = value
        if answer is not None:
            self.store.data['answer'] = answer
        target = {
            'completed': 'complete', 'partial': 'partial', 'limit_reached': 'partial',
            'failed': 'failed', 'cancelled': 'cancelled',
            'awaiting_confirmation': 'await_confirmation', 'interrupted': 'interrupted',
        }.get(value)
        if target and self.machine.state != target:
            try:
                self.machine.transition(target, 'status_' + value, save=False)
            except StateTransitionError:
                self.store.data['run_runtime']['lifecycle'] = target
        if value in {'completed', 'partial', 'limit_reached', 'failed', 'cancelled'}:
            self.store.data['run_runtime']['phase'] = 'done'
            self.runtime.policy.clear_run_grant()
            self.store.data['last_run_outcome'] = {
                'run_id': self.store.data['run_id'], 'status': value,
                'stop_reason': self.store.data['run_runtime'].get('stop_reason'),
                'diagnostic': self.store.data['run_runtime'].get('last_failure'),
                'tool_activity': self.store.data['run_runtime'].get('tool_activity', []),
            }
            self._finish_project_docs(value, answer or '')
        if value == 'awaiting_confirmation':
            self.store.data['run_runtime'].setdefault('confirmation_paused_at', None)
            if self.store.data['run_runtime']['confirmation_paused_at'] is None:
                self.store.data['run_runtime']['confirmation_paused_at'] = time.time()
        self.store.save()
        state = self.store.data['run_runtime']
        self.store.event('status', status=value, profile=state['profile'], phase=state['phase'], stop_reason=state.get('stop_reason'), evidence_count=len(state['evidence_refs']))
        return dict(status=value, answer=self.store.data.get('answer', ''), pending=self.store.data['pending'])

    def ensure_available(self):
        """检查是否可以开始新运行；存在未完成运行时抛出异常。"""
        d = self.store.data
        if d['status'] in {'running', 'awaiting_confirmation', 'interrupted'}:
            raise ValueError('先 /resume、处理确认或 /cancel，再提交新输入')
        if any(call.get('state') in {'started', 'unknown'} and not call.get('acknowledged') for call in d['calls'].values()):
            raise ValueError('存在执行结果不确定的调用；请检查输出并 /cancel，禁止开始新一轮')

    def start(self, text):
        """根据用户输入初始化并启动一轮新运行。"""
        d = self.store.data
        self.ensure_available()
        if not text.strip() or len(text) > 16000:
            raise ValueError('输入不能为空或超过16000字符')
        self.store.verify_sources()
        self.store.verify_artifacts()
        previous_runtime = d.get('run_runtime') or {}
        previous_answer = d.get('answer', '')
        previous_status = d.get('status')
        if d['run_id']:
            d['history'].append({
                'run_id': d['run_id'], 'status': d['status'], 'answer': d.get('answer', ''),
                'evidence_refs': list(previous_runtime.get('evidence_refs', [])),
                'rounds': d.get('rounds', 0), 'tool_count': d.get('tool_count', 0),
                'usage': json.loads(json.dumps(d.get('usage', {}))),
                'runtime_usage': json.loads(json.dumps(previous_runtime.get('usage', {}))),
                'stop_reason': previous_runtime.get('stop_reason'),
                'tool_activity': json.loads(json.dumps(previous_runtime.get('tool_activity', []))),
                'context_report': json.loads(json.dumps(previous_runtime.get('context_report', {}))),
            })
        project_manager = None
        active_task = None
        if AgentDocsManager.exists(Path(d['workspace'])):
            try:
                project_manager = AgentDocsManager(Path(d['workspace']))
                active_task = project_manager.active_task()
            except Exception:
                project_manager = None
                active_task = None
        continuation = self._is_development_continuation(text, active_task)
        profile = 'local_files' if continuation else select_initial_profile(text)
        d['run_runtime'] = ResearchController.empty_state(profile)
        if profile.startswith('web'):
            # 这些是确定性意图规则选中的内置工具描述符，加载不会产生网络或外部副作用。
            self.runtime.load_tools(['search_query', 'search_fetch'])
        protocols = self._protocols_for(text)
        if continuation:
            inherited = previous_runtime.get('protocols') or []
            protocols = list(dict.fromkeys([*inherited, *protocols, 'coding']))
            d['run_runtime']['pending_intent'] = {
                'kind': 'continue_development',
                'task_id': active_task['id'],
                'objective': active_task['title'],
                'task_status': active_task['status'],
                'current_phase': active_task['status'],
                'default_plan_confirmed': self._accepts_default_plan(text),
                'next_action': (
                    'write_plan_and_implement'
                    if not active_task.get('plan_ready')
                    else ('verify_changes' if active_task['status'] == 'needs_verification' else 'implement_next_step')
                ),
                'awaiting_confirmation': False,
                'instruction': text.strip(),
            }
        d['run_runtime']['protocols'] = protocols
        # 开发运行开始时预加载规划和验证工具。
        if 'coding' in d['run_runtime']['protocols'] or 'testing' in d['run_runtime']['protocols']:
            names = ['python_project', 'test_run']
            if AgentDocsManager.exists(Path(d['workspace'])):
                names.append('project_plan')
            self.runtime.load_tools(names)
        if previous_runtime.get('evidence_refs'):
            # 普通追问建立新 Run，但从上一 Run 搬运不可变候选、来源和证据索引。
            for key in ('candidates', 'candidate_count', 'content_hashes', 'evidence', 'evidence_refs'):
                if key in previous_runtime:
                    d['run_runtime'][key] = json.loads(json.dumps(previous_runtime[key], ensure_ascii=False))
            d['run_runtime']['reuse_evidence'] = True
        self.runtime.policy.clear_run_grant()
        d['run_runtime']['execution_seconds_limit'] = self.seconds
        d['run_runtime']['objective'] = active_task['title'] if continuation else text
        d['run_runtime']['execution_requested'] = continuation or self._requests_development_execution(text, protocols)
        d['run_runtime']['project_changed'] = False
        d['run_runtime']['verification_succeeded'] = False
        if is_recall_request(text):
            d['run_runtime']['phase'] = 'answer'
        d.update(run_id=uid('run'), status='running', rounds=0, tool_count=0, failures=0,
                 deadline=time.time() + min(self.seconds, d['run_runtime']['budget']['seconds']), pending=[], answer='', usage={},
                 limits=dict(rounds=self.max_rounds, tools=self.max_tools, failures=self.max_failures))
        if 'coding' in d['run_runtime']['protocols'] and project_manager is not None:
            try:
                task = project_manager.begin_task(d['run_runtime']['objective'], run_id=d['run_id'])
                d['run_runtime']['project_task_id'] = task['id']
                d['run_runtime']['project_task_status'] = task['status']
            except Exception as exc:
                d['run_runtime']['agent_docs_error'] = f'{type(exc).__name__}: {exc}'
        d['messages'].append(dict(role='user', content=text))
        self.cancel_requested = False
        self.machine.restart()
        self.machine.transition('execute', 'prepared')
        self.research.register_user_urls(text)
        self.store.save()
        self.store.event('run_start', profile=profile, phase='explore')
        if is_recall_request(text) and previous_answer and self._can_reuse_answer(text, previous_answer):
            # 纯文本追问不应让随机模型修改已通过来源和价格校验的事实。
            d['messages'].append(dict(role='assistant', content=previous_answer, tool_calls=None))
            recall_status = 'completed' if previous_status == 'completed' else 'partial'
            d['run_runtime']['stop_reason'] = 'model_answer' if recall_status == 'completed' else 'evidence_gap'
            self.store.save()
            return self.status(recall_status, previous_answer)
        return self.drive()

    def _can_reuse_answer(self, request, answer):
        value = request.casefold()
        if any(word in value for word in ('价格', '售价', '多少钱', '美元', '欧元', '人民币', 'price', 'cost')):
            return bool(self._price_amounts(answer))
        if any(word in value for word in ('日期', '时间', '哪天', '年份', '月份', 'date', 'time')):
            return bool(re.search(r'\b(?:19|20)\d{2}(?:[-年/.]\d{1,2})?', answer))
        if any(word in value for word in ('标题', '名称', '规格', '编号', 'title', 'name')):
            return False
        return True

    def context(self, *, recovery=False, final=False):
        """构建当前轮次的模型上下文。"""
        try:
            self.store.data['run_runtime']['capabilities'] = {
                layer: [item['name'] for item in entries]
                for layer, entries in self.runtime.snapshot().items()
            }
            return self.context_budgeter.build(self.store.data['run_runtime']['profile'], recovery=recovery, final=final)
        except ValueError as exc:
            raise ContextLimitError(str(exc)) from exc

    def append_result(self, call, result):
        """记录工具执行结果到会话消息和工具活动日志。"""
        d = self.store.data
        d['run_runtime'].setdefault('tool_activity', []).append({
            'call_id': call['id'], 'tool': call['name'], 'ok': result.get('ok'),
            'code': (result.get('error') or {}).get('code'),
            'executed': d['calls'].get(call['id'], {}).get('executed', False),
        })
        d['messages'].append(dict(role='tool', tool_call_id=call['id'], content=json.dumps(result, ensure_ascii=False)))
        d['failures'] = 0 if result['ok'] else d['failures'] + 1
        d['pending'].pop(0)
        if AgentDocsManager.exists(Path(d['workspace'])):
            try:
                arguments = json.loads(call.get('arguments') or '{}')
                manager = AgentDocsManager(Path(d['workspace']))
                manager.record_tool(call['name'], result, arguments if isinstance(arguments, dict) else {})
                task = manager.active_task()
                if task:
                    d['run_runtime']['project_task_id'] = task['id']
                    d['run_runtime']['project_task_status'] = task['status']
            except Exception as exc:
                d['run_runtime']['agent_docs_error'] = f'{type(exc).__name__}: {exc}'
        self.store.save()
        self.on_event('tool_result', result)

    def close_pending(self, code):
        """以指定错误码关闭所有待处理调用。"""
        while self.store.data['pending']:
            call = self.store.data['pending'][0]
            self.append_result(call, self.runtime.error(call, code, '本次运行已停止，工具未执行；原因：' + code))

    def stop(self, status, message, reason=None):
        """以指定状态和消息终止当前运行。"""
        state = self.store.data['run_runtime']
        state['stop_reason'] = reason or state.get('stop_reason') or status
        self.close_pending(state['stop_reason'])
        return self.status(status, message)

    def fail(self, exc, *, reason='runtime_error'):
        """记录异常诊断并终止运行。"""
        diagnostic = {
            'id': uid('diagnostic'), 'type': type(exc).__name__,
            # 不持久化异常参数、局部变量、源码文本或 HTTP 响应体。
            'message': '模型服务调用失败' if reason == 'model_error' else '运行组件发生内部异常',
            'frames': [{'file': Path(frame.filename).name, 'function': frame.name, 'line': frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-12:]],
        }
        self.store.data['run_runtime']['last_failure'] = diagnostic
        pending = self.store.data['pending']
        self.store.event('failure', code=type(exc).__name__, diagnostic=diagnostic,
                         tool_call_id=pending[0]['id'] if pending else None)
        message = self.model_error(exc) + ' 诊断编号：' + diagnostic['id'] + '（/trace 查看）'
        return self.stop('failed', message, reason)

    def cancel(self):
        """标记取消请求并终止运行。"""
        self.cancel_requested = True
        for call in self.store.data['calls'].values():
            if call.get('state') in {'started', 'unknown'}:
                call['acknowledged'] = True
        return self.stop('cancelled', '运行已取消；不确定写操作不会自动重做，请检查输出文件。', 'user_cancelled')

    def resume(self):
        """从中断或确认等待状态恢复运行。"""
        d = self.store.data
        if any(c['state'] in {'started', 'unknown'} and not c.get('acknowledged') for c in d['calls'].values()):
            return self.status('interrupted', '存在不确定调用。请检查输出并 /cancel；不会自动重放。')
        if d['status'] not in {'interrupted', 'awaiting_confirmation'}:
            raise ValueError('没有可恢复的运行')
        self.store.verify_sources(); self.store.verify_artifacts()
        if d['status'] == 'awaiting_confirmation':
            return self.status('awaiting_confirmation')
        self.machine.transition('execute', 'resume')
        return self.drive()

    def confirm(self, approve):
        """处理用户对待确认调用的批准或拒绝。"""
        d = self.store.data
        if d['status'] != 'awaiting_confirmation' or not d['pending']:
            raise ValueError('没有待确认调用')
        try:
            return self._confirm(approve)
        except KeyboardInterrupt:
            return self.cancel()
        except Exception as exc:
            return self.fail(exc)

    def _confirm(self, approve):
        """执行确认或拒绝操作，处理过期确认的重新预览。"""
        d = self.store.data
        state = d['run_runtime']
        paused_at = state.get('confirmation_paused_at')
        if paused_at is not None:
            duration = max(0, time.time() - paused_at)
            d['deadline'] += duration
            state['paused_seconds'] = state.get('paused_seconds', 0) + duration
            state['confirmation_paused_at'] = None
        call = d['pending'][0]
        if time.time() >= d['deadline']:
            return self.stop('limit_reached', '运行已超时；确认没有执行', 'deadline')
        if approve:
            prior = d['calls'].get(call['id'], {})
            if prior.get('expires', 0) < time.time():
                # 重建预览，但绝不用过期的确认执行。
                d['calls'].pop(call['id'], None)
                policy = self.runtime.policy
                modes = policy.confirm_search, policy.confirm_writes, policy.confirm_shell
                policy.clear_run_grant()
                policy.confirm_search = policy.confirm_writes = policy.confirm_shell = True
                try:
                    result = self.runtime.execute(call)
                finally:
                    policy.confirm_search, policy.confirm_writes, policy.confirm_shell = modes
                if result.get('pending'):
                    return self.status('awaiting_confirmation', '确认已过期；目标已重新预览，请再次确认。')
                self.append_result(call, result)
                self.machine.transition('execute', 'confirmation_expired')
                return self.drive()
            result = self.runtime.execute(call, approved=prior.get('digest', 'invalid'))
        else:
            result = self.research.after_execute(call, self.runtime.reject(call))
        self.machine.transition('execute', 'confirmation_resolved')
        if result.get('error') and result['error']['code'] == 'unknown_execution':
            return self.status('interrupted', result['error']['message'])
        self.append_result(call, result)
        if not approve:
            self.research.force_answer('user_rejected')
            self.close_pending('user_rejected')
        return self.drive()

    def drive(self):
        """执行一轮完整的 ReAct 循环。

        按 思考→行动→观察 迭代，直到产出最终回答或触发限制。
        返回最终状态字典或回答文本；异常由调用方捕获。
        """
        d = self.store.data
        self.status('running')
        try:
            while True:
                if self.cancel_requested: return self.cancel()
                state = d['run_runtime']
                limits = d.get('limits', dict(rounds=self.max_rounds, tools=self.max_tools, failures=self.max_failures))
                if time.time() >= d['deadline']:
                    if state['evidence_refs']: self.research.force_answer('deadline')
                    else: return self.stop('limit_reached', '达到运行时间上限，且没有取得可用证据', 'deadline')
                if d['failures'] >= limits['failures']:
                    if state['evidence_refs']: self.research.force_answer('failure_limit')
                    else: return self.stop('limit_reached', '达到连续失败上限，且没有取得可用证据', 'failure_limit')
                if d['pending']:
                    outcome = self._execute_pending(limits)
                    if outcome is not None: return outcome
                    continue
                decision = self.completion.decide(state)
                final, recovery = decision == 'answer', decision == 'recover'
                if final and self.machine.state not in {'verify', 'recover'}:
                    self.machine.transition('verify', 'answer_ready')
                if d['rounds'] >= limits['rounds']:
                    if state['evidence_refs'] or state.get('partial_response'):
                        return self.status('partial', state.get('partial_response') or '已达到模型轮次上限；已保存来源，请缩小问题后继续。')
                    return self.stop('limit_reached', '达到模型轮次上限', 'model_budget')
                if not final and not recovery and state['profile'].startswith('web') and state['usage']['model_calls'] >= max(1, state['budget']['model_calls'] - 1):
                    self.research.force_answer('model_answer_reserve'); continue
                self.research.note_progress_round()
                context = self.context(recovery=recovery, final=final)
                tool_definitions = [] if final or recovery else self.runtime.definitions()
                response = self._call_model(context, tool_definitions)
                if isinstance(response, dict): return response
                calls = [asdict(call) for call in response.tool_calls or []]
                if not calls and re.search(r'<[^>\n]{0,40}DSML[^>\n]{0,40}(?:calls|invoke|parameter)', response.content or '', re.I):
                    outcome = self._recover_serialized_tool_output()
                    if outcome is not None:
                        return outcome
                    continue
                if response.finish_reason == 'length':
                    outcome = self._handle_length(response.content or '')
                    if outcome is not None: return outcome
                    continue
                if response.finish_reason not in {None, 'stop', 'tool_calls'}:
                    return self.stop('failed', '模型输出未正常结束：' + str(response.finish_reason))
                if not response.content and not calls:
                    outcome = self._handle_empty()
                    if outcome is not None: return outcome
                    continue
                invalid = self._invalid_calls(calls, tool_definitions)
                if invalid:
                    outcome = self._recover_tool_protocol(invalid)
                    if outcome is not None: return outcome
                    continue
                if state['phase'] == 'answer' and calls: continue
                if calls:
                    d['messages'].append(dict(role='assistant', content=response.content, tool_calls=[dict(call) for call in calls]))
                    d['pending'] = [dict(call) for call in calls]
                    self.store.save()
                    continue
                if not response.content: continue
                if self._development_change_required(state):
                    outcome = self._recover_tool_required()
                    if outcome is not None:
                        return outcome
                    continue
                # 自验证：文件修改后自动运行测试。
                if (self._needs_verification or state.get('verification_required')) and self._should_auto_verify(state):
                    outcome = self._inject_verification_test()
                    if outcome == 'skip':
                        pass  # test_run not loaded; fall through to answer
                    elif outcome is not None:
                        return outcome
                    else:
                        continue
                if self.machine.state in {'execute', 'recover'}:
                    self.machine.transition('verify', 'model_answer')
                if not self._answer_refs_valid(response.content):
                    outcome = self._recover_bad_refs()
                    if outcome is not None: return outcome
                    continue
                unsupported_prices = self._unsupported_price_claims(response.content)
                if unsupported_prices:
                    outcome = self._recover_unsupported_prices(unsupported_prices)
                    if outcome is not None: return outcome
                    continue
                self.store.verify_artifacts(); EvidenceBundle(self.store).verify()
                d['messages'].append(dict(role='assistant', content=response.content, tool_calls=None))
                self.store.save()
                partial = response.content.lstrip().startswith('[[PARTIAL]]')
                answer = response.content.lstrip().removeprefix('[[PARTIAL]]').lstrip() if partial else response.content
                price_request = bool(re.search(r'价格|售价|多少钱|price|cost', str(state.get('objective') or ''), re.I))
                verified_price = price_request and bool(self._price_amounts(response.content)) and not unsupported_prices and bool(SOURCE_REF.findall(response.content))
                if verified_price and not partial:
                    state['stop_reason'] = 'model_answer'
                verification_incomplete = bool(state.get('verification_required'))
                if verification_incomplete:
                    state['stop_reason'] = 'verification_failed'
                forced = state.get('stop_reason') not in {None, 'model_answer', 'finish_reason_length', 'empty_response', 'invalid_source_reference'}
                state['stop_reason'] = state.get('stop_reason') or ('evidence_gap' if partial else 'model_answer')
                return self.status('partial' if partial or forced or verification_incomplete else 'completed', answer)
        except KeyboardInterrupt: return self.cancel()
        except ContextLimitError as exc: return self.stop('failed', str(exc), 'context_error')
        except Exception as exc:
            return self.fail(exc)

    def _execute_pending(self, limits):
        """执行单个待处理工具调用并更新验证状态。"""
        d = self.store.data; call = d['pending'][0]; prior = d['calls'].get(call['id'])
        if prior and prior['state'] in {'unknown', 'started'}: return self.status('interrupted', '存在不确定调用，禁止自动重放')
        if not prior:
            if d['tool_count'] >= limits['tools']:
                if not d['run_runtime']['evidence_refs'] and (d['run_runtime']['profile'] in {'direct_answer', 'local_files'} or d['run_runtime']['usage'].get('search_queries', 0) == 0):
                    return self.stop('limit_reached', '达到工具调用上限', 'tool_budget')
                self.research.force_answer('tool_budget'); self.close_pending('budget_exhausted'); return None
            d['tool_count'] += 1; self.store.save()
        self.on_event('tool_start', dict(name=call['name'])); started = time.monotonic()
        result = self.runtime.execute(call)
        if result.get('pending'):
            return self.status('awaiting_confirmation', self.runtime.confirmation_message(call['name']))
        self.store.event('tool_end', tool_call_id=call['id'], tool=call['name'], ok=result.get('ok'), code=(result.get('error') or {}).get('code'), elapsed_ms=round((time.monotonic() - started) * 1000), profile=d['run_runtime']['profile'], phase=d['run_runtime']['phase'])
        if result.get('error') and result['error']['code'] == 'unknown_execution': return self.status('interrupted', result['error']['message'])
        self.append_result(call, result)
        if call['name'] in self._FILE_MODIFY_TOOLS and result.get('ok'):
            d['run_runtime']['project_changed'] = True
            validation = ((result.get('data') or {}).get('validation') or {})
            if validation.get('passed') is True:
                d['run_runtime']['verification_succeeded'] = True
                d['run_runtime']['verification_required'] = False
                self._needs_verification = False
            if self._requires_change_verification(call['name'], result):
                self._needs_verification = True
                d['run_runtime']['verification_required'] = True
            self.store.save()
        elif call['name'] == 'test_run' and result.get('ok'):
            passed = bool((result.get('data') or {}).get('passed'))
            self._needs_verification = not passed
            d['run_runtime']['verification_required'] = not passed
            d['run_runtime']['verification_succeeded'] = passed
            self.store.save()
        if self.research.should_force_answer(): self.close_pending('research_complete')
        return None

    def _call_model(self, context, tools):
        """调用模型并记录用量，支持可重试错误的自动重试。"""
        d = self.store.data; d['rounds'] += 1; self.store.save(); self.on_event('model', dict(round=d['rounds']))
        state = d['run_runtime']; call_id = self.trace.model_start(state); started = time.monotonic(); attempts = 0
        while True:
            try:
                options = {}
                if tools == []:
                    profile = state.get('profile')
                    if profile == 'web_quick':
                        options['max_tokens'] = 900
                    elif profile == 'direct_answer':
                        options['max_tokens'] = 600
                    # 流式输出纯文本轮次，让用户实时看到 token。
                    collected: list[str] = []
                    reasoning_collected: list[str] = []
                    final_response = None
                    try:
                        stream = self.model.stream_chat(context, tools=[], **options)
                    except TypeError as exc:
                        # 兼容仍暴露旧版流式签名的第三方 ModelClient 实现。
                        if options and 'unexpected keyword argument' in str(exc):
                            stream = self.model.stream_chat(context, tools=[])
                        else:
                            raise
                    for chunk in stream:
                        if isinstance(chunk, tuple) and chunk[0] == 'reasoning':
                            reasoning_collected.append(chunk[1])
                            self.on_event('reasoning_token', {})
                        elif isinstance(chunk, str):
                            collected.append(chunk)
                            self.on_event('stream_token', {'text': chunk})
                        else:
                            final_response = chunk
                    if final_response is None:
                        from genesisai.shared.messages import Response
                        final_response = Response(content=''.join(collected))
                    # 如果最终响应缺少推理内容，则补充已收集的推理。
                    if reasoning_collected and not final_response.reasoning:
                        final_response.reasoning = ''.join(reasoning_collected)
                    response = final_response; break
                response = self.model.chat(context, tools=tools, **options); break
            except Exception as exc:
                retryable = type(exc).__name__ in {'APIConnectionError', 'APITimeoutError', 'RateLimitError', 'InternalServerError'}
                if retryable and attempts < 1:
                    attempts += 1; state['recovery_counts']['provider'] += 1
                    self.store.event('model_retry', model_call_id=call_id, code=type(exc).__name__, retry_count=attempts); continue
                return self.fail(exc, reason='model_error' if type(exc).__name__ in {'APIConnectionError', 'APITimeoutError', 'RateLimitError', 'InternalServerError', 'AuthenticationError', 'PermissionDeniedError', 'BadRequestError'} else 'runtime_error')
        usage = response.usage or {}
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if isinstance(usage.get(key), int): d['usage'][key] = d['usage'].get(key, 0) + usage[key]
        self.research.note_model(usage.get('total_tokens', 0) if isinstance(usage.get('total_tokens'), int) else 0)
        self.trace.model_end(call_id, elapsed_ms=round((time.monotonic() - started) * 1000), finish_reason=response.finish_reason, usage={k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens') if isinstance(usage.get(k), int)}, state=state)
        if time.time() >= d['deadline'] and not state['evidence_refs']: return self.stop('limit_reached', '模型返回时已超过运行截止时间')
        return response

    def _handle_length(self, content):
        """处理模型输出达到长度上限的情况。"""
        state = self.store.data['run_runtime']
        if content: state['partial_response'] = (state.get('partial_response', '') + '\n' + content).strip()
        state['recovery_counts']['length'] += 1; self.store.save()
        if state['recovery_counts']['length'] > 1:
            state['stop_reason'] = 'second_length'; return self.status('partial', state['partial_response'] or '模型输出两次达到长度上限。')
        state['phase'] = 'recover'; state['stop_reason'] = 'finish_reason_length'; self.machine.transition('recover', 'finish_reason_length', save=False); self.store.save(); return None

    def _recover_serialized_tool_output(self):
        """模型输出的工具协议文本不可执行，也不可作为回答交付。"""
        state = self.store.data['run_runtime']
        counts = state['recovery_counts']
        counts['serialized_tool_output'] = counts.get('serialized_tool_output', 0) + 1
        self.store.event('tool_protocol_error', code='serialized_tool_output', retry_count=counts['serialized_tool_output'])
        state['response_issue'] = '上一条输出是错误的工具协议文本，已丢弃且未执行。请用一至三句自然语言回答；没有取得所问信息时说明缺口，不输出任何工具标记。'
        if counts['serialized_tool_output'] > 1 or self.store.data['rounds'] >= self.store.data['limits']['rounds']:
            return self.stop(
                'partial' if state['evidence_refs'] else 'failed',
                self._evidence_fallback('模型连续返回了不可用的工具协议文本。'),
                'invalid_model_output',
            )
        state['phase'] = 'recover'
        self.machine.transition('recover', 'serialized_tool_output')
        return None

    def _handle_empty(self):
        """处理模型返回空回答的情况。"""
        state = self.store.data['run_runtime']; state['recovery_counts']['empty'] += 1
        if state['recovery_counts']['empty'] > 1: return self.stop('failed', '模型连续返回空回答；已有运行进度已经保存。')
        state['phase'] = 'recover'; state['stop_reason'] = 'empty_response'; self.machine.transition('recover', 'empty_response', save=False); self.store.save(); return None

    def _invalid_calls(self, calls, definitions):
        """检查工具调用 ID 是否重复或调用了未提供的工具。"""
        ids = [call['id'] for call in calls]
        if len(ids) != len(set(ids)) or any(not value or value in self.store.data['calls'] for value in ids): return '模型返回重复或无效工具调用 ID'
        offered = {item['function']['name'] for item in definitions}
        if any(call['name'] not in offered for call in calls): return '模型调用了当前请求未提供的工具'
        return None

    def _recover_tool_protocol(self, message):
        """处理无效工具调用的恢复流程。"""
        state = self.store.data['run_runtime']; state['recovery_counts']['tool_protocol'] += 1
        if state['recovery_counts']['tool_protocol'] > 1:
            if state['evidence_refs']: self.research.force_answer('tool_protocol_repeated'); self.store.save(); return None
            return self.stop('failed', message)
        state['stop_reason'] = None; self.store.event('tool_protocol_error', code='invalid_tool_call', retry_count=1); self.store.save(); return None

    def _answer_refs_valid(self, content):
        """检查回答中的来源引用是否都已登记且可引用。"""
        refs = SOURCE_REF.findall(content)
        evidence = set(self.store.data['run_runtime']['evidence_refs'])
        sources = self.store.data['sources']
        return not refs or all(
            ref in sources and (ref in evidence or sources[ref].get('kind') == 'file')
            for ref in refs
        )

    @staticmethod
    def _price_claims(content):
        """提取文本中所有币种+金额声明。"""
        values = set()
        for match in PRICE_CLAIM.finditer(content or ''):
            currency = match.group(1) or match.group(4)
            raw = (match.group(2) or match.group(3)).replace(',', '')
            try:
                amount = str(float(raw)).rstrip('0').rstrip('.')
            except ValueError:
                continue
            normalized_currency = CURRENCY_ALIASES.get(currency.casefold())
            if normalized_currency:
                values.add((normalized_currency, amount))
        return values

    @classmethod
    def _price_amounts(cls, content):
        return {amount for _, amount in cls._price_claims(content)}

    def _unsupported_price_claims(self, content):
        """找出回答中未被已读正文支持的价格声明。"""
        claimed = self._price_claims(content)
        if not claimed:
            return []
        refs = SOURCE_REF.findall(content)
        if not refs:
            return sorted(claimed)
        supported = set()
        for ref in refs:
            path = self.store.root / 'sources' / (ref + '.json')
            try:
                source = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                continue
            supported.update(self._price_claims(str(source.get('text', ''))))
        return [f'{currency} {amount}' for currency, amount in sorted(claimed - supported)]

    def _recover_bad_refs(self):
        """处理回答中引用了不存在来源的情况。"""
        state = self.store.data['run_runtime']
        counts = state['recovery_counts']
        counts['source_reference'] = counts.get('source_reference', 0) + 1
        self.store.event('answer_validation_error', code='invalid_source_reference', retry_count=counts['source_reference'])
        if counts['source_reference'] > 1:
            return self.stop(
                'partial' if state['evidence_refs'] else 'failed',
                self._evidence_fallback('模型连续生成了无法验证的来源引用。'),
                'invalid_source_reference',
            )
        allowed = ', '.join(state['evidence_refs']) or '无'
        state['response_issue'] = (
            '上一条回答引用了不存在或尚未读取的来源，已丢弃且不会进入会话历史。'
            f'只可引用以下 source_ref：{allowed}。没有有效来源时不要生成 source_ref。'
        )
        state['phase'] = 'recover'; state['stop_reason'] = 'invalid_source_reference'; self.machine.transition('recover', 'invalid_source_reference', save=False); self.store.save(); return None

    def _evidence_fallback(self, reason):
        """为终止消息补充已读取来源的信息。"""
        state = self.store.data['run_runtime']
        refs = [ref for ref in state.get('evidence_refs', []) if ref in self.store.data.get('sources', {})]
        if not refs:
            return reason + ' 本轮没有取得可引用正文。'
        sources = '、'.join(f'[{ref}]' for ref in refs[-3:])
        return reason + f' 已读取的来源为 {sources}，但当前问题尚未形成可验证答案。'

    def _recover_unsupported_prices(self, amounts):
        """处理回答中包含未支持价格的情况。"""
        state = self.store.data['run_runtime']
        counts = state['recovery_counts']
        counts['evidence_claim'] = counts.get('evidence_claim', 0) + 1
        self.store.event('answer_validation_error', code='unsupported_price_claim', retry_count=counts['evidence_claim'])
        if counts['evidence_claim'] > 1:
            return self.stop(
                'partial' if state['evidence_refs'] else 'failed',
                '未能生成由已读取正文支持的价格答复；已有资料已经保存。',
                'unsupported_evidence_claim',
            )
        state['phase'] = 'recover'
        state['stop_reason'] = 'unsupported_evidence_claim'
        state['response_issue'] = (
            '上一条回答包含已读取正文不支持的价格数字，已丢弃且不会进入会话历史。'
            '请只使用正文中与币种或价格单位相邻的金额；没有受支持金额时，以 [[PARTIAL]] 开头并明确尚未取得价格。'
        )
        self.machine.transition('recover', 'unsupported_price_claim', save=False)
        self.store.save()
        return None

    _FILE_MODIFY_TOOLS = frozenset({'file_patch', 'file_create', 'file_delete'})
    _MAX_VERIFICATION_ROUNDS = 3

    def _should_auto_verify(self, state: dict) -> bool:
        """判断当前状态是否满足自动验证条件。"""
        profile = state.get('profile', '')
        protocols = state.get('protocols', [])
        if profile != 'local_files' and 'coding' not in protocols:
            return False
        return state.get('verification_rounds', 0) < self._MAX_VERIFICATION_ROUNDS

    def _requires_change_verification(self, tool_name: str, result: dict | None = None) -> bool:
        """仅对已有项目的编辑操作需要测试门禁。"""
        if tool_name == 'file_create':
            data = (result or {}).get('data') or {}
            return bool(data.get('requires_verification') and not (data.get('validation') or {}).get('passed'))
        state = self.store.data.get('run_runtime') or {}
        protocols = state.get('protocols', [])
        return state.get('profile') == 'local_files' or 'coding' in protocols or 'testing' in protocols

    def _development_change_required(self, state: dict) -> bool:
        """检查开发任务是否要求模型必须调用写入工具。"""
        return bool(
            state.get('execution_requested')
            and 'coding' in state.get('protocols', [])
            and not state.get('project_changed')
            and not state.get('verification_succeeded')
        )

    def _recover_tool_required(self):
        """处理模型未调用写入工具的恢复流程。"""
        state = self.store.data['run_runtime']
        counts = state.setdefault('recovery_counts', {})
        counts['tool_required'] = counts.get('tool_required', 0) + 1
        self.store.event(
            'answer_validation_error',
            code='tool_required',
            retry_count=counts['tool_required'],
        )
        if counts['tool_required'] > 1:
            return self.stop(
                'partial',
                '开发任务尚未产生项目文件变更；模型连续两次没有调用写入工具，已有计划和进度已保存。',
                'tool_required',
            )
        state['phase'] = 'explore'
        state['stop_reason'] = None
        state['response_issue'] = (
            '上一条回答没有产生任何项目文件变更，已拒绝将其作为完成结果。'
            '当前用户已经要求执行；请立即调用 file_create 或 file_patch 写入工作区，'
            '不要再次询问是否开始，也不要把源码作为普通回答输出。'
        )
        if self.machine.state == 'verify':
            self.machine.transition('execute', 'tool_required', save=False)
        self.store.save()
        return None

    def _inject_verification_test(self):
        """代码修改后注入自动测试调用进行验证。"""
        d = self.store.data
        state = d['run_runtime']
        # 仅在 test_run 已加载到运行时中时才注入。
        loaded = {item['function']['name'] for item in self.runtime.definitions()}
        if 'test_run' not in loaded:
            # 无法验证；清除标志位，让回答流程继续。
            self._needs_verification = False
            return 'skip'
        state['verification_rounds'] = state.get('verification_rounds', 0) + 1
        self._needs_verification = False
        call_id = uid('call')
        synthetic_call = {
            'id': call_id, 'name': 'test_run',
            'arguments': json.dumps({'cwd': '.'}),
        }
        d['messages'].append(dict(
            role='assistant',
            content='[自动验证] 检测到代码修改，正在运行测试…',
            tool_calls=[synthetic_call],
        ))
        d['pending'] = [synthetic_call]
        if self.machine.state not in {'execute'}:
            try:
                self.machine.transition('execute', 'auto_verification')
            except StateTransitionError:
                pass
        self.store.save()
        self.on_event('verification_start', {'round': state['verification_rounds']})
        return None

    def _finish_project_docs(self, status, answer):
        """运行结束时更新 agent_docs 任务状态。"""
        workspace = Path(self.store.data.get('workspace') or self.store.workspace)
        if not AgentDocsManager.exists(workspace):
            return
        try:
            task = AgentDocsManager(workspace).finish_run(status, answer)
            if task:
                self.store.data['run_runtime']['project_task_id'] = task['id']
                self.store.data['run_runtime']['project_task_status'] = task['status']
        except Exception as exc:
            self.store.data['run_runtime']['agent_docs_error'] = f'{type(exc).__name__}: {exc}'

    @staticmethod
    def _is_development_continuation(text, active_task) -> bool:
        """判断短文本是否为开发任务的继续指令。"""
        if not active_task or active_task.get('status') in {'verified', 'cancelled'}:
            return False
        value = re.sub(r'[\s，。！？!,.；;：:]+', '', text.casefold())
        if len(value) > 24:
            return False
        exact = {
            '开始', '开始吧', '继续', '继续吧', '继续实现', '继续执行', '开始实现', '开始执行',
            '开始写', '动手吧', '就这样', '按这个来',
            '按默认来', '按你的默认来', '就按默认来', '就按你的默认来', '照默认来',
            '可以', '可以开始', '同意', '确认', '没问题', '照这个方案做', '按这个方案做',
        }
        return value in exact

    @staticmethod
    def _accepts_default_plan(text) -> bool:
        """判断文本是否表示接受默认计划。"""
        value = re.sub(r'[\s，。！？!,.；;：:]+', '', text.casefold())
        return value in {
            '就这样', '按这个来', '按默认来', '按你的默认来', '就按默认来',
            '就按你的默认来', '照默认来', '照这个方案做', '按这个方案做', '可以', '同意', '确认',
        }

    @staticmethod
    def _requests_development_execution(text, protocols) -> bool:
        """判断用户输入是否明确要求执行开发操作。"""
        if 'coding' not in protocols:
            return False
        value = text.casefold()
        if any(phrase in value for phrase in ('先讨论', '先规划', '有什么要问', '暂时不要写', '不要修改')):
            return False
        return any(word in value for word in ('实现', '修复', '编写', '创建', '构建', '开发', '修改'))

    @staticmethod
    def _protocols_for(text):
        """根据用户输入关键词推断所需的协议列表。"""
        value = text.casefold()
        result = []
        if any(word in value for word in ('代码', '仓库', '修复', 'bug', '重构', '实现', '报错', '编写', '开发', '函数', '类', 'python', 'pyproject', 'pip', 'ruff', 'mypy')):
            result.append('coding')
        if any(word in value for word in ('测试', 'pytest', 'unittest', 'npm test', 'cargo test', '跑测试', '跑一下测试')):
            result.append('testing')
        if any(word in value for word in ('git', 'diff', '提交记录', '分支')):
            result.append('git_read')
        if any(word in value for word in ('文件', '目录', '移动', '重命名', '删除', '整理')):
            result.append('file_management')
        if any(word in value for word in ('docx', 'xlsx', 'pptx', 'pdf', 'word', 'excel', 'powerpoint', '办公文档')):
            result.append('office')
        if any(word in value for word in ('调试', 'debug', '排查', '为什么失败', '异常', 'traceback', '错误信息')):
            result.append('debugging')
        if any(word in value for word in ('写小说', '续写', '扩写', '章节', '故事', '剧情', '角色', '世界观', '创作', '写作')):
            result.append('creative_writing')
        if any(word in value for word in ('修改', '润色', '改写', '修订', '审稿')):
            result.append('revision')
        return result

    @staticmethod
    def model_error(exc):
        """将模型异常类型映射为用户可读的中文错误消息。"""
        messages = {'APIConnectionError': '无法连接模型服务。请检查服务地址和网络连接。', 'APITimeoutError': '模型服务响应超时，请稍后重试或调整超时配置。', 'AuthenticationError': '模型认证失败，请检查工作区根目录 .env 中的 API Key。', 'PermissionDeniedError': '模型服务拒绝访问，请检查账号与模型权限。', 'RateLimitError': '模型服务达到限额或请求频率限制，请稍后重试。', 'BadRequestError': '模型服务拒绝了请求，请检查模型名称和工具调用兼容性。', 'InternalServerError': '模型服务内部错误，请稍后重试；会话状态已经保存。', 'StopIteration': '模型调用了当前请求未提供的工具，模型请求已终止。'}
        return messages.get(type(exc).__name__, '运行失败：' + type(exc).__name__ + '。内部执行异常，进度已保存。')

