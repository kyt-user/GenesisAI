"""单 Agent 的 ReAct 循环骨架：只持有端口，不 import 任何 core 实现。"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from dataclasses import asdict

from genesisai.agent.completion import CompletionValidator
from genesisai.agent.evidence import EvidenceBundle
from genesisai.agent.recovery import RecoveryMixin, SOURCE_REF
from genesisai.agent.state_machine import RunStateMachine, StateTransitionError
from genesisai.shared.budgets import initial_run_runtime, is_recall_request, select_initial_profile
from genesisai.shared.ids import uid


class ContextLimitError(ValueError):
    """当前与上一轮必要上下文无法安全放入模型请求。"""


class Runner(RecoveryMixin):
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
        self.composer = runtime.composer
        self.context_budgeter = runtime.context_budgeter
        self.completion = CompletionValidator()
        self.trace = runtime.trace
        self.machine = RunStateMachine(self.store)
        self.docs = runtime.docs

    @property
    def research(self):
        return self.runtime.research

    @property
    def development(self):
        """开发验证策略端口（core/development 实现，由 app 注入）。"""
        return self.runtime.development

    @property
    def prompt(self):
        """任务协议选择端口（core/prompt 实现，由 app 注入）。"""
        return self.runtime.prompt

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
            self.docs.finish(value, answer or '')
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
        if self.docs.exists():
            try:
                project_manager = self.docs.manager()
                active_task = project_manager.active_task()
            except Exception:
                project_manager = None
                active_task = None
        continuation = self.prompt.is_development_continuation(text, active_task)
        profile = 'local_files' if continuation else select_initial_profile(text)
        d['run_runtime'] = initial_run_runtime(profile)
        protocols = self.prompt.protocols_for(text)
        if continuation:
            inherited = previous_runtime.get('protocols') or []
            protocols = list(dict.fromkeys([*inherited, *protocols, 'coding']))
            d['run_runtime']['pending_intent'] = {
                'kind': 'continue_development',
                'task_id': active_task['id'],
                'objective': active_task['title'],
                'task_status': active_task['status'],
                'current_phase': active_task['status'],
                'default_plan_confirmed': self.prompt.accepts_default_plan(text),
                'next_action': (
                    'write_plan_and_implement'
                    if not active_task.get('plan_ready')
                    else ('verify_changes' if active_task['status'] == 'needs_verification' else 'implement_next_step')
                ),
                'awaiting_confirmation': False,
                'instruction': text.strip(),
            }
        d['run_runtime']['protocols'] = protocols
        if previous_runtime.get('evidence_refs'):
            # 普通追问建立新 Run，但从上一 Run 搬运不可变候选、来源和证据索引。
            for key in ('candidates', 'candidate_count', 'content_hashes', 'evidence', 'evidence_refs'):
                if key in previous_runtime:
                    d['run_runtime'][key] = json.loads(json.dumps(previous_runtime[key], ensure_ascii=False))
            d['run_runtime']['reuse_evidence'] = True
        self.runtime.policy.clear_run_grant()
        d['run_runtime']['execution_seconds_limit'] = self.seconds
        d['run_runtime']['objective'] = active_task['title'] if continuation else text
        d['run_runtime']['execution_requested'] = continuation or self.prompt.requests_development_execution(text, protocols)
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

    def context(self, *, recovery=False, final=False):
        """构建当前轮次的模型上下文。"""
        try:
            self.store.data['run_runtime']['capabilities'] = {
                'enabled': [item['function']['name'] for item in self.runtime.definitions()],
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
        if self.docs.exists():
            try:
                arguments = json.loads(call.get('arguments') or '{}')
                manager = self.docs.manager()
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
                if self.development.development_change_required(state):
                    outcome = self.development.recover_tool_required(self)
                    if outcome is not None:
                        return outcome
                    continue
                # 自验证：文件修改后自动运行测试。
                if (self._needs_verification or state.get('verification_required')) and self.development.should_auto_verify(state):
                    outcome = self.development.inject_verification_test(self)
                    if outcome == 'skip':
                        pass  # run_commands not available; fall through to answer
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
        if call['name'] == 'attempt_completion' and result.get('ok'):
            answer = (result.get('data') or {}).get('result')
            if answer:
                return self.stop('completed', answer, 'attempt_completion')
        if call['name'] in self.development.FILE_MODIFY_TOOLS and result.get('ok'):
            d['run_runtime']['project_changed'] = True
            validation = ((result.get('data') or {}).get('validation') or {})
            if validation.get('passed') is True:
                d['run_runtime']['verification_succeeded'] = True
                d['run_runtime']['verification_required'] = False
                self._needs_verification = False
            if self.development.requires_change_verification(call['name'], result, run_state=d['run_runtime']):
                self._needs_verification = True
                d['run_runtime']['verification_required'] = True
            self.store.save()
        elif call['name'] == 'run_commands' and result.get('ok') and isinstance((result.get('data') or {}).get('summary'), dict):
            passed = bool((result.get('data') or {}).get('passed'))
            self._needs_verification = not passed
            d['run_runtime']['verification_required'] = not passed
            d['run_runtime']['verification_succeeded'] = passed
            self.store.save()
        if self.research.should_force_answer(): self.close_pending('research_complete')
        return None
