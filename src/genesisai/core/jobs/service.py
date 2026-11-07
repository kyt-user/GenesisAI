"""Job submission and observation; execution outlives any attached CLI session."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import codecs

from genesisai.core.extensions.tools.shell.commands import process_invocation
from genesisai.core.jobs.storage import ACTIVE, Slot, SlotBusy, log_size, read_log, save, load
from genesisai.shared.ids import uid
from genesisai.shared.process_tree import safe_environment
from genesisai.shared.run_control import cancellation_requested, current_run
from genesisai.shared.security import ToolError
from genesisai.shared.time_limits import tool_deadline


class JobService:
    def __init__(self, store, clean, logger=None):
        self.store = store
        self.root = store.root / "jobs" / store.id
        self.settings = store.root / "jobs" / "limits.json"
        self.clean = clean
        self.logger = logger
        self.children = {}

    def _limits(self):
        values = {"max_concurrent": 4, "log_max_bytes": 128 * 1024**2, 'result_max_bytes': 128 * 1024**2, 'input_max_bytes': 1024**2}
        if self.settings.exists():
            supplied = json.loads(self.settings.read_text(encoding="utf-8"))
            if not isinstance(supplied, dict) or supplied.keys() - values.keys():
                raise ToolError("job_config", "作业 limits.json 包含未知设置")
            values.update(supplied)
        if any(type(v) is not int or v < 1 for v in values.values()):
            raise ToolError("job_config", "作业资源配置必须为正整数")
        return values

    def _path(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"job_[a-zA-Z0-9_-]+", job_id):
            raise ToolError("unknown_job", "作业编号无效")
        return self.root / job_id

    def _info(self, job_id):
        try:
            info = load(self._path(job_id) / "job.json")
        except FileNotFoundError as exc:
            raise ToolError("unknown_job", "本会话没有此作业") from exc
        except (PermissionError, ValueError) as exc:
            raise ToolError('unknown_execution', f'作业 {job_id} 状态暂不可读；请用 read_job 核对，禁止重发原操作') from exc
        if info["status"] in ACTIVE and time.time() - info["heartbeat"] > 15:
            try:
                lease = Slot(self._path(job_id) / 'host.lock')
            except SlotBusy:
                info['heartbeat_stale'] = True
            except OSError as exc:
                raise ToolError('unknown_execution', f'作业 {job_id} 的宿主存活状态暂不可核实；禁止重发') from exc
            else:
                lease.close()
                info.update(status="unknown", error="独立宿主已失联；未重发操作，请核对实际结果")
        process = self.children.get(job_id)
        if process and process.poll() is not None:
            self.children.pop(job_id, None)
        return info

    def start(self, command, cwd, timeout=None, framework=None, task_id=None, *, env_from=None, interaction=None, write_paths=None, verification=None):
        from genesisai.shared.process_tree import mapped_environment
        from genesisai.core.jobs.input_channel import interaction as normalize_interaction
        interaction = normalize_interaction(interaction)
        invocation, env = process_invocation(command, cwd, env_from=env_from)
        secrets = list(mapped_environment(env_from or {}).values())
        for value in secrets:
            if self.logger:
                self.logger.add_secret(value)
        return self._start('command', {'invocation': invocation, 'env': env, 'secrets': secrets},
                           {'cwd': str(cwd), 'command': self.clean(command), 'framework': framework,
                            'interaction': interaction, 'control_ready': False, 'write_paths': write_paths or [], 'verification': verification},
                           timeout=timeout, task_id=task_id)

    def prepare_input(self, args, call_id):
        from genesisai.core.jobs.job_input import prepare
        return prepare(self, args, call_id)

    def write_input(self, args, call_id):
        from genesisai.core.jobs.job_input import write
        return write(self, args, call_id)

    def input_receipt(self, job_id, input_id):
        from genesisai.core.jobs.job_input import receipt
        return receipt(self, job_id, input_id)

    def start_operation(self, operation, data, *, timeout=None, task_id=None):
        if operation not in {'external_tool', 'mcp_request', 'capability_test', 'capability_try', 'builtin_tool'}:
            raise ToolError('job_operation', '未知的独立作业操作')
        snapshot = data.get('runtime', {})
        secrets = [*snapshot.get('environment', {}).values(),
                   *(data.get('environment', {}).values() if operation == 'external_tool' else ()),
                   *data.get('server', {}).get('env', {}).values(),
                   *data.get('server', {}).get('headers', {}).values()]
        if (snapshot.get('model') or {}).get('kind') == 'provider':
            secrets.append(snapshot['model']['api_key'])
        secrets = [value for value in secrets if isinstance(value, str) and value]
        if self.logger:
            for secret in secrets:
                self.logger.add_secret(secret)
        metadata = {'origin_call': data['origin_call'], 'side_effect': data['side_effect']} if operation == 'builtin_tool' else {}
        if operation == 'builtin_tool' and data['side_effect']:
            from genesisai.core.jobs.write_coordination import prepared_paths
            metadata['write_paths'] = prepared_paths(data['preview'], data['store']['workspace'])
        elif operation == 'external_tool':
            metadata['write_paths'] = [data['workspace']]
        return self._start(operation, {'operation': operation, 'data': data, 'secrets': secrets}, metadata, timeout=timeout, task_id=task_id)

    def _start(self, kind, payload, metadata, *, timeout, task_id):
        if timeout is not None and (type(timeout) not in (int, float) or timeout <= 0):
            raise ToolError("validation_error", "作业超时须为正数或不设置")
        limits = self._limits()
        job_id = uid("job")
        root = self._path(job_id)
        root.mkdir(parents=True)
        from genesisai.core.tools.permissions import current_authorization
        authorization = current_authorization.get()
        info = {"job_id": job_id, "kind": kind, **metadata,
                'authorization_id': authorization['id'] if authorization else None,
                'state_root': str(self.store.root), 'session_id': self.store.id,
                "started": time.time(), "heartbeat": time.time(), "status": "queued", "exit_code": None,
                "timeout_seconds": timeout, "task_id": task_id, "run_id": self.store.record.run_id,
                "output_ref": str(root / "output"), "offset_unit": "utf8_bytes"}
        if kind == 'command' and info.get('interaction'):
            from genesisai.core.jobs.input_channel import create_control
            payload['control'] = create_control(root, self.store.id, job_id)
        save(root / "job.json", info)
        if kind != 'command':
            self._pending(job_id, True)
        payload = {**payload, "root": str(root), "limits": limits,
                   "secrets": [*payload.get('secrets', []), *getattr(getattr(self.clean, "__self__", None), "_secrets", ())]}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        try:
            process = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "-m", "genesisai.core.jobs.worker"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=safe_environment(), **options)
            self.children[job_id] = process
            process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            process.stdin.close()
            if self.logger:
                self.logger.record('job_submitted', **info)
        except BaseException as exc:
            # A submitted command is never replayed based on a missing reply.
            if "process" not in locals():
                save(root / "job.json", {**info, "status": "failed", "error": self.clean(str(exc)), "finished": time.time()})
                if kind != 'command':
                    self._pending(job_id, False)
            else:
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if isinstance(exc, Exception):
                    raise ToolError('unknown_execution', f'作业 {job_id} 已提交但响应丢失；请用 read_job 核对，禁止重发原操作') from exc
            raise
        return info

    def _pending(self, job_id, present):
        state = self.store.record.run_runtime
        pending = list(state.get('pending_execution_jobs', []))
        state['pending_execution_jobs'] = list(dict.fromkeys([*pending, job_id])) if present else [item for item in pending if item != job_id]
        self.store.commit()

    def operation_result(self, job_id, *, acknowledge=True):
        info = self._info(job_id)
        if info['status'] in ACTIVE:
            return None
        result_path = self._path(job_id) / 'result.json'
        if info['status'] != 'succeeded':
            if info['status'] == 'unknown':
                raise ToolError('unknown_execution', f"作业 {job_id} 执行结果未知；禁止重发，请核对实际结果：{info.get('error', '')}")
            self._pending(job_id, False)
            raise ToolError(info.get('code', 'job_' + info['status']), info.get('error', '作业没有完成结果；请核对作业状态'), bool(info.get('retryable', False)))
        try:
            value = load(result_path)
        except (OSError, ValueError) as exc:
            raise ToolError('unknown_execution', f'作业 {job_id} 的完成结果不可读；禁止重发，请核对结果文件') from exc
        if acknowledge:
            self._pending(job_id, False)
        return value

    def status(self, job_id=None, offset=0, limit=50):
        if job_id:
            return self._info(job_id)
        jobs = sorted(self.root.glob("job_*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        page = jobs[offset:offset + limit]
        return {"jobs": [self._info(path.parent.name) for path in page], "total": len(jobs),
                "next_offset": offset + len(page), "more": offset + len(page) < len(jobs)}

    def output(self, job_id, offset=0, limit=4000):
        info = self._info(job_id)
        root = self._path(job_id) / "output"
        size = log_size(root)
        # Read enough extra bytes to finish one UTF-8 codepoint at the page edge.
        raw = read_log(root, offset, limit + 3) if limit else b""
        length = min(limit, len(raw))
        while length < len(raw) and raw[length] & 0xc0 == 0x80:
            length += 1
        text = raw[:length].decode("utf-8", errors="replace")
        result = {**info, "text": text, "next_offset": offset + length, "more": offset + length < size,
                  "output_bytes": size}
        progress = self._path(job_id) / 'progress.json'
        if progress.is_file():
            result['progress'] = load(progress)
        return result

    def wait(self, job_id, seconds=0, cancel_on_interrupt=False, emit_output=False):
        deadline = min(time.monotonic() + seconds, tool_deadline.get() or float("inf"))
        control = current_run.get()
        offsets = {'stdout': 0, 'stderr': 0}
        decoders = {name: codecs.getincrementaldecoder('utf-8')(errors='replace') for name in offsets}
        while True:
            info = self._info(job_id)
            if emit_output and control and info['kind'] == 'command':
                for name in offsets:
                    path = self._path(job_id) / name
                    # Drain only bytes already published in this iteration.
                    size = log_size(path)
                    while offsets[name] < size:
                        raw = read_log(path, offsets[name], min(64 * 1024, size - offsets[name]))
                        if not raw:
                            break
                        offsets[name] += len(raw)
                        text = decoders[name].decode(raw)
                        if text:
                            control.emit('command_output', {'job_id': job_id, 'stream': name, 'text': text})
                    if info['status'] not in ACTIVE:
                        tail = decoders[name].decode(b'', final=True)
                        if tail:
                            control.emit('command_output', {'job_id': job_id, 'stream': name, 'text': tail})
            if cancel_on_interrupt and cancellation_requested() and info["status"] in ACTIVE:
                save(self._path(job_id) / "stop.json", {"requested": time.time()})
            if info["status"] not in ACTIVE or time.monotonic() >= deadline or cancellation_requested():
                return info
            time.sleep(min(.05, max(0, deadline - time.monotonic())))

    def result(self, job_id, limit=12000):
        info = self._info(job_id)
        root = self._path(job_id)
        result = {**info, "passed": info.get("passed", info["status"] == "succeeded"),
                  "elapsed_ms": round(((info.get("finished") or time.time()) - info["started"]) * 1000),
                  "truncated": False, "output_refs": {}}
        for name in ("stdout", "stderr"):
            size = log_size(root / name)
            result[name] = read_log(root / name, 0, limit).decode("utf-8", errors="replace")
            result["truncated"] |= size > limit
            result["output_refs"][name] = {"path": str(root / name), "size": size, "offset_unit": "utf8_bytes"}
        if self.logger:
            self.logger.record('job_output', job_id=job_id, status=info['status'], output_refs=result['output_refs'])
        return result

    def _request_stop(self, job_id):
        info = self._info(job_id)
        if info["status"] not in ACTIVE and info['status'] != 'unknown':
            return info
        save(self._path(job_id) / "stop.json", {"requested": time.time()})
        # Lost heartbeat is uncertainty, not proof that the child stopped.
        return {**info, 'cancel_requested': True}

    def stop(self, job_id):
        info = self._request_stop(job_id)
        if info['status'] not in ACTIVE:
            return info
        return self.wait(job_id, 5)

    def stop_all(self):
        return self._stop_many([path.parent.name for path in self.root.glob('job_*/job.json')])

    def stop_authorization(self, authorization_id):
        return self._stop_many([path.parent.name for path in self.root.glob('job_*/job.json')
                               if self._info(path.parent.name).get('authorization_id') == authorization_id])

    def stop_run(self, run_id):
        return self._stop_many([path.parent.name for path in self.root.glob('job_*/job.json')
                               if self._info(path.parent.name).get('run_id') == run_id])

    def _stop_many(self, identities):
        # Signal every host before waiting; cancelling N long jobs must not take
        # N times the observation window or leave later jobs executing meanwhile.
        results = [self._request_stop(identity) for identity in identities]
        deadline = time.monotonic() + 5
        while any(item['status'] in ACTIVE for item in results) and time.monotonic() < deadline:
            if cancellation_requested():
                break
            time.sleep(.05)
            results = [self._info(identity) for identity in identities]
        return results

    def close(self):
        # Detach from independent hosts. Explicit cancellation uses stop/stop_all.
        for job_id, process in list(self.children.items()):
            if process.poll() is not None:
                del self.children[job_id]
