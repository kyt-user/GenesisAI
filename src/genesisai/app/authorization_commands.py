"""Explicit user CLI controls; intentionally absent from the model tool registry."""
import json

from genesisai.shared.security import ToolError


def execute_authorization_command(runtime, text):
    command, _, argument = text.strip().partition(' ')
    if command == '/authorizations':
        if argument.strip():
            raise ToolError('validation_error', '/authorizations 不接受参数')
        return {'authorizations': runtime.authorizations.list()}
    if command == '/authorize':
        payload = json.loads(argument)
        names = payload.get('tools') if isinstance(payload, dict) else None
        if isinstance(names, list) and all(isinstance(name, str) for name in names):
            unknown = [name for name in names if name not in runtime.registry]
            if unknown:
                raise ToolError('unknown_tool', '授权工具不存在：' + ', '.join(map(str, unknown)))
        result = runtime.authorizations.grant(payload, source='cli')
        if result['command_prefixes']:
            result['shell_scope'] = '命令前缀与工作目录授权；子进程不是 OS 文件或网络沙箱'
        return result
    if command == '/revoke':
        identity = argument.strip()
        result = runtime.authorizations.revoke(identity)
        return {'authorization': result, 'jobs': runtime.jobs.stop_authorization(identity)}
    if command == '/constraints':
        if not argument.strip():
            task = runtime.tasks.active_task()
            return {'task_id': task['id'] if task else None, 'constraints':
                    runtime.tasks.effective_constraints(task, runtime.store.record.run_id) if task else {}}
        payload = json.loads(argument)
        if not isinstance(payload, dict) or payload.keys() - {'read_only', 'network_forbidden'}:
            raise ToolError('validation_error', '/constraints 接受 read_only/network_forbidden')
        return runtime.tasks.update_constraints(payload, source='cli')
    raise ToolError('validation_error', '未知授权命令')
