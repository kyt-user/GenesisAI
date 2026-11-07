"""Input authorization reads committed state; no runner Session mutation."""
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace

from genesisai.core.jobs import input_channel
from genesisai.core.jobs.storage import load
from genesisai.core.persistence.database import Database
from genesisai.core.persistence.session import Session
from genesisai.core.memory.tasks import TaskService
from genesisai.core.tools.authorization import AuthorizationService
from genesisai.core.tools.permissions import accepted_tool, current_authorization
from genesisai.shared.process_tree import mapped_environment
from genesisai.shared.security import Access, ToolError, digest


def require_current(info, input_authorization=None):
    state, _ = Session.inspect(info['state_root'], info['session_id'])
    store = SimpleNamespace(id=info['session_id'], root=Path(info['state_root']),
        workspace=Path(state['workspace']), record=SimpleNamespace(**state), database=Database(info['state_root']))
    access = Access([Path(value) for value in state['grants']], Path(state['output']), store.root,
                    workspace_root=store.workspace)
    access.host_access = state['permission_mode'] == 'auto'
    access.workspace_path(info['cwd'], must_exist=False, allow_directory=True)
    tasks = TaskService(store)
    task = tasks.active_task()
    if task and tasks.effective_constraints(task, state['run_id'])['read_only']:
        raise ToolError('permission_denied', '当前任务只读，不能向运行进程输入')
    authorizations = AuthorizationService(store)
    for identity in (info.get('authorization_id'), input_authorization):
        if identity:
            authorizations.require_active(identity)


def owned(service, job_id):
    info = service._info(job_id)
    if (info['session_id'] != service.store.id or Path(info['state_root']).resolve() != service.store.root.resolve()
            or info['kind'] != 'command' or not info.get('interaction')):
        raise ToolError('permission_denied', '该会话没有可交互的命令作业')
    require_current(info)
    if (service._path(job_id) / 'stop.json').exists():
        raise ToolError('job_stopping', '作业正在停止，不接受新输入')
    if info['status'] != 'running' or not info.get('control_ready'):
        raise ToolError('job_not_ready', '作业尚未就绪或已结束；先用 read_job 核对状态')
    return info


def request(service, args, call_id):
    allowed = {'job_id', 'action', 'text', 'secret_from_env', 'append_newline', 'rows', 'columns'}
    if (not isinstance(args, dict) or args.keys() - allowed or not isinstance(args.get('job_id'), str)
            or ('append_newline' in args and type(args['append_newline']) is not bool)):
        raise ToolError('validation_error', '输入字段或类型无效')
    info = owned(service, args['job_id'])
    action = args.get('action', 'write')
    if not isinstance(action, str) or action not in {'write', 'close', 'resize'}:
        raise ToolError('validation_error', '输入操作必须是 write/close/resize')
    text, source = args.get('text'), args.get('secret_from_env')
    secret_chars = None
    preview = {'job_id': info['job_id'], 'target': info['cwd'], 'command': info['command'],
               'interaction': info['interaction'], 'action': action}
    if action == 'write':
        if (text is None) == (source is None):
            raise ToolError('validation_error', 'write 须明确给出 text 或 secret_from_env，不能同时使用')
        if source is not None:
            text = mapped_environment({'STDIN': source})['STDIN']
            secret_chars = len(text)
            preview['env_from'] = {'STDIN': source}
        if not isinstance(text, str):
            raise ToolError('validation_error', '输入必须是文本')
        if args.get('append_newline'):
            text += '\r' if os.name == 'nt' and info['interaction']['mode'] == 'pty' else '\n'
        if source is None:
            preview['text'] = text
    else:
        if text is not None or source is not None or args.get('append_newline'):
            raise ToolError('validation_error', 'close/resize 不接受文本或环境变量')
        text = ''
    rows, columns = args.get('rows'), args.get('columns')
    if action == 'resize':
        if info['interaction']['mode'] != 'pty' or rows is None or columns is None:
            raise ToolError('validation_error', 'resize 需要 PTY 作业和 rows/columns')
        input_channel.interaction({'mode': 'pty', 'rows': rows, 'columns': columns})
        preview.update(rows=rows, columns=columns)
    elif rows is not None or columns is not None:
        raise ToolError('validation_error', '只有 resize 接受终端尺寸')
    if action == 'close' and info['interaction']['mode'] == 'pty':
        raise ToolError('validation_error', 'PTY 请显式输入目标程序使用的 EOF 控制字符或 stop_job；close 仅关闭管道输入')
    size = len(text.encode('utf-8'))
    if size > service._limits()['input_max_bytes']:
        raise ToolError('input_capacity', '本次输入超过配置 input_max_bytes；可分批输入或调整作业输入配额')
    control = load(service._path(info['job_id']) / 'control.json')
    binding = hmac.new(bytes.fromhex(control['key']), text.encode('utf-8'), hashlib.sha256).hexdigest()
    preview.update(input_id=input_channel.identity(call_id), bytes=size, content_binding=binding)
    authorization = current_authorization.get()
    value = {'input_id': preview['input_id'], 'job_id': info['job_id'], 'session_id': service.store.id,
             'action': action, 'text': text, 'secret_chars': secret_chars, 'secret_source': source, 'rows': rows, 'columns': columns,
             'authorization_id': authorization['id'] if authorization else None}
    return info, value, preview


def prepare(service, args, call_id):
    return request(service, args, call_id)[2]


def write(service, args, call_id):
    info, value, preview = request(service, args, call_id)
    acceptance = accepted_tool.get()
    if acceptance is not None and acceptance != {'name': 'write_job_input', 'digest': digest({**preview, 'args': args})}:
        raise ToolError('confirmation_invalid', '输入内容或环境值在确认后发生变化；未发送输入')
    if value['secret_chars'] is not None:
        original_secret = value['text'][:value['secret_chars']]
        for secret in (original_secret, original_secret.rstrip('\r\n')):
            if service.logger:
                service.logger.add_secret(secret)
    control = load(service._path(info['job_id']) / 'control.json')
    if control['job_id'] != info['job_id'] or control['session_id'] != service.store.id:
        raise ToolError('permission_denied', '输入控制通道不属于当前会话作业')
    result = input_channel.submit(control, value)
    if service.logger:
        service.logger.record('job_input_submitted', job_id=info['job_id'], input_id=preview['input_id'],
                              action=value['action'], bytes=preview['bytes'], status=result['status'])
    if result.get('code'):
        raise ToolError(result['code'], result.get('error', '作业输入未发送'))
    return {**result, 'read': {'job_id': info['job_id'], 'input_id': preview['input_id']}}


def receipt(service, job_id, input_id):
    info = service._info(job_id)
    if info['session_id'] != service.store.id:
        raise ToolError('permission_denied', '输入回执不属于当前会话')
    path = input_channel.receipt_path(service._path(job_id), input_id)
    if not path.exists():
        raise ToolError('unknown_input', '此作业没有该输入回执；不得猜测已发送')
    value = load(path)
    if value['job_id'] != job_id:
        raise ToolError('permission_denied', '输入回执所属作业不匹配')
    if value['status'] in {'accepted', 'sending'} and info['status'] not in {'queued', 'running', 'stopping'}:
        return {**value, 'status': 'unknown', 'error': '宿主未确认该输入结果；禁止重发'}
    return value
