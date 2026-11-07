"""Execute generated programs without passing host services to their process."""

import json
import os
from pathlib import Path
import sys
import time

from genesisai.core.capabilities.contracts import schema_validator

from genesisai.shared.ids import uid
from genesisai.shared.process_tree import owned_process, safe_environment, mapped_environment, environment_preview
from genesisai.shared.run_control import cancellation_requested
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline, execution_limits


def run_tool(package, bundle, arguments, workspace, logs, *, environment=None, owner, result_max_bytes, redaction_secrets=()):
    from genesisai.core.capabilities.assets import file_identity
    definition = package['definition']
    error = next(schema_validator(definition['parameters']).iter_errors(arguments), None)
    if error:
        raise ToolError('validation_error', f'{error.json_path}: {error.message}')
    if type(result_max_bytes) is not int or result_max_bytes <= 0:
        raise ToolError('capability_config', '结果存储配额必须为正整数')
    logs = (Path(logs) / uid('tool')).absolute()
    logs.mkdir(parents=True)
    request, result_file = logs / 'input.json', logs / 'result.json'
    request.write_text(json.dumps(arguments, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    timeout = definition.get('timeout_seconds')
    deadline = min(time.monotonic() + timeout if timeout is not None else float('inf'), tool_deadline.get() or float('inf'))
    mapped = mapped_environment(definition.get('env_from', {})) if environment is None else environment
    frozen_secrets = frozenset(value for value in (*redaction_secrets, *mapped.values()) if isinstance(value, str) and value)
    environment = {**safe_environment(), **mapped}
    for name in ('PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONPYCACHEPREFIX'):
        environment.pop(name, None)
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', GENESISAI_RESULT_PATH=str(result_file))
    stdout, stderr = logs / 'stdout.log', logs / 'stderr.log'
    log_limit = execution_limits.get().get('log_max_bytes', 128 * 1024**2)

    def check_quotas():
        if stdout.stat().st_size + stderr.stat().st_size > log_limit:
            raise ToolError('log_limit', '程序日志达到作业存储配额，已有日志保留在诊断目录')
        if result_file.exists() and result_file.stat().st_size > result_max_bytes:
            raise ToolError('result_limit', '程序结果达到资产导入配额；请明确调整存储配额或让工具分批返回')

    outcome = None
    try:
        with request.open('rb') as incoming, stdout.open('wb') as outgoing, stderr.open('wb') as errors:
            command = ([sys.executable, '-B', str(Path(bundle) / definition['entry'])] if definition['runner'] == 'python'
                       else [part.replace('{python}', sys.executable).replace('{bundle}', str(bundle)) for part in definition['command']])
            with owned_process(command, cwd=workspace, env=environment, stdin=incoming, stdout=outgoing, stderr=errors) as process:
                while process.poll() is None:
                    if cancellation_requested():
                        raise ToolError('command_cancelled', '能力执行已取消，工作目录可能有部分输出')
                    if time.monotonic() >= deadline:
                        raise ToolError('tool_timeout', '能力执行超时，已终止所属进程树')
                    check_quotas()
                    time.sleep(.02)
                exit_code = process.returncode
        check_quotas()
        if exit_code != 0:
            raise ToolError('external_tool_failed', f'程序退出码 {exit_code}；诊断可通过作业日志引用分页读取')
        try:
            from genesisai.core.capabilities.json_results import validate_result
            value, identity = validate_result(result_file, definition['output_schema'], result_max_bytes)
        except (OSError, ValueError, UnicodeError) as exc:
            raise ToolError('invalid_result', 'GENESISAI_RESULT_PATH 需要有效 UTF-8 JSON，拒绝重复键、NaN 和 Infinity；stdout/stderr 仅作日志') from exc
        if file_identity(result_file) != identity:
            raise ToolError('source_changed', '校验期间结果发生变化')
        outcome = {'owner': owner, 'directory': str(logs), 'files': {'result': {'path': str(result_file), **identity}}}
        return outcome
    finally:
        from genesisai.core.jobs.stream_output import StreamOutput
        from genesisai.core.state.io_journal import IOJournal
        redactor = object.__new__(IOJournal)
        redactor._secrets = frozen_secrets
        channels = {}
        for name, raw in (('stdout', stdout), ('stderr', stderr)):
            if not raw.is_file():
                continue
            original = file_identity(raw)
            target = logs / (name + '.redacted.log')
            writer = StreamOutput(redactor.clean)
            with raw.open('rb') as source, target.open('x', encoding='utf-8', newline='') as output:
                while chunk := source.read(1024 * 1024):
                    output.write(writer.feed(chunk))
                output.write(writer.feed(b'', final=True))
            if file_identity(raw) != original:
                raise ToolError('source_changed', '日志脱敏期间原文发生变化')
            channels[name] = {'path': str(target), **file_identity(target), 'redacted': True,
                              'original_bytes': original['size']}
        if outcome is not None:
            outcome['files'].update(channels)
        (logs / 'channels.json').write_text(json.dumps({'owner': owner, 'directory': str(logs), 'files': channels},
                                                       ensure_ascii=False), encoding='utf-8')



def mcp_spec(package, bundle, workspace=None):
    from genesisai.core.extensions.mcp.bridge import McpServerSpec
    definition = package["definition"]
    variables = definition.get("env_from", {}) if definition["transport"] == "stdio" else definition.get("headers_from", {})
    environment = {}
    for target, source in variables.items():
        if not os.environ.get(source):
            raise ToolError("missing_environment", "MCP 服务需要配置宿主环境变量：" + source)
        environment[target] = os.environ[source]
    common = dict(name=package["name"], transport=definition["transport"], read_only=definition["read_only"], scope=package["scope"], request_timeouts=definition.get("request_timeouts"))
    if definition["transport"] == "streamable_http":
        return McpServerSpec(**common, url=definition["url"], headers=environment)
    command = [part.replace("{python}", sys.executable).replace("{bundle}", str(bundle)) for part in definition["command"]]
    return McpServerSpec(**common, command=command[0], args=tuple(command[1:]), env=environment, cwd=str(workspace or bundle))



class ExternalTool:
    """Trusted adapter; generated Python runs only after the normal approval."""

    def __init__(self, package, bundle, identity, revision, digest):
        self.package, self.bundle = package, str(bundle)
        self.identity, self.revision, self.digest = identity, revision, digest

    def prepare(self, context, args):
        # Integrity is checked on the host before offering and executing tools.
        return {"target": self.package["name"], "capability_id": self.identity,
                "revision": self.revision, "digest": self.digest, "workspace": str(context.store.workspace),
                **environment_preview(self.package['definition'].get('env_from', {}))}

    def execute(self, context, args):
        workspace = context.access.workspace_path(".", must_exist=True, allow_directory=True)
        job = context.runtime.jobs.start_operation('external_tool',
            {'package': self.package, 'bundle': self.bundle, 'arguments': args, 'workspace': str(workspace),
             'environment': mapped_environment(self.package['definition'].get('env_from', {})),
             'owner': {'id': self.identity, 'revision': self.revision, 'digest': self.digest},
             'result_max_bytes': context.runtime.capabilities.result_limit(),
             'redaction_secrets': tuple(context.runtime.logger.journal._secrets)},
            timeout=self.package['definition'].get('timeout_seconds'))
        state = context.runtime.jobs.wait(job['job_id'], 5, cancel_on_interrupt=True)
        if state['status'] not in {'queued', 'running', 'stopping'}:
            try:
                return context.runtime.capabilities.collect_tool_result(job['job_id'])
            except ToolError as exc:
                from genesisai.core.tools.base import PartialToolResult
                diagnostics = context.runtime.capabilities.collect_tool_logs(job['job_id'])
                return PartialToolResult(exc.error_type, str(exc), job_id=job['job_id'], **diagnostics)
        return {**state, 'continuation': '使用 read_job(job_id) 读取最终结构化结果'}
