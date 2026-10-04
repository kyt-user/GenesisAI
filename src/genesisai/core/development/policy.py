"""开发验证策略：判定何时需要自动验证，并驱动验证与“必须写工具”的恢复流程。

从 `agent/loop.py` 拆出（规格 §6），属 L4 core。agent 只持有端口并传入自身，
不 import 任何 core.*；app 在组装期构造并挂到 `runtime.development`。
"""

from __future__ import annotations

import json

from genesisai.agent.state_machine import StateTransitionError
from genesisai.shared.ids import uid


class DevelopmentPolicy:
    """开发验证策略（从 Runner 拆出，属 L4 core）。"""

    FILE_MODIFY_TOOLS = frozenset({'editor'})
    MAX_VERIFICATION_ROUNDS = 3

    def should_auto_verify(self, state: dict) -> bool:
        """判断当前状态是否满足自动验证条件。"""
        profile = state.get('profile', '')
        protocols = state.get('protocols', [])
        if profile != 'local_files' and 'coding' not in protocols:
            return False
        return state.get('verification_rounds', 0) < self.MAX_VERIFICATION_ROUNDS

    def requires_change_verification(self, tool_name: str, result: dict | None = None, *, run_state: dict | None = None) -> bool:
        """仅对已有项目的编辑操作需要测试门禁。"""
        if tool_name == 'editor':
            data = (result or {}).get('data') or {}
            if 'requires_verification' in data:
                return bool(data.get('requires_verification') and not (data.get('validation') or {}).get('passed'))
        state = run_state or {}
        protocols = state.get('protocols', [])
        return state.get('profile') == 'local_files' or 'coding' in protocols or 'testing' in protocols

    def development_change_required(self, state: dict) -> bool:
        """检查开发任务是否要求模型必须调用写入工具。"""
        return bool(
            state.get('execution_requested')
            and 'coding' in state.get('protocols', [])
            and not state.get('project_changed')
            and not state.get('verification_succeeded')
        )

    def recover_tool_required(self, runner):
        """处理模型未调用写入工具的恢复流程。"""
        state = runner.store.data['run_runtime']
        counts = state.setdefault('recovery_counts', {})
        counts['tool_required'] = counts.get('tool_required', 0) + 1
        runner.store.event(
            'answer_validation_error',
            code='tool_required',
            retry_count=counts['tool_required'],
        )
        if counts['tool_required'] > 1:
            return runner.stop(
                'partial',
                '开发任务尚未产生项目文件变更；模型连续两次没有调用写入工具，已有计划和进度已保存。',
                'tool_required',
            )
        state['phase'] = 'explore'
        state['stop_reason'] = None
        state['response_issue'] = (
            '上一条回答没有产生任何项目文件变更，已拒绝将其作为完成结果。'
            '当前用户已经要求执行；请立即调用 editor 工具写入工作区，'
            '不要再次询问是否开始，也不要把源码作为普通回答输出。'
        )
        if runner.machine.state == 'verify':
            runner.machine.transition('execute', 'tool_required', save=False)
        runner.store.save()
        return None

    def inject_verification_test(self, runner):
        """代码修改后注入自动测试调用进行验证。"""
        d = runner.store.data
        state = d['run_runtime']
        # 仅在 run_commands 已加载到运行时中时才注入。
        loaded = {item['function']['name'] for item in runner.runtime.definitions()}
        if 'run_commands' not in loaded:
            # 无法验证；清除标志位，让回答流程继续。
            runner._needs_verification = False
            return 'skip'
        state['verification_rounds'] = state.get('verification_rounds', 0) + 1
        runner._needs_verification = False
        call_id = uid('call')
        synthetic_call = {
            'id': call_id, 'name': 'run_commands',
            'arguments': json.dumps({'operation': 'run', 'cwd': '.'}),
        }
        d['messages'].append(dict(
            role='assistant',
            content='[自动验证] 检测到代码修改，正在运行测试…',
            tool_calls=[synthetic_call],
        ))
        d['pending'] = [synthetic_call]
        if runner.machine.state not in {'execute'}:
            try:
                runner.machine.transition('execute', 'auto_verification')
            except StateTransitionError:
                pass
        runner.store.save()
        runner.on_event('verification_start', {'round': state['verification_rounds']})
        return None