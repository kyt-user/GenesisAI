"""Observe persisted grant lifetime independently of execution and heartbeat."""
import threading

from genesisai.core.tools.authorization import verify_job_authorization
from genesisai.shared.security import ToolError


class AuthorizationWatch:
    def __init__(self, info):
        self.info = dict(info)
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.failure = None
        self.thread = None

    def _watch(self):
        while not self.closed.wait(.5):
            try:
                verify_job_authorization(self.info)
            except Exception as exc:
                code = ('authorization_revoked' if isinstance(exc, ToolError)
                        and exc.error_type == 'authorization_revoked' else 'authorization_unavailable')
                self.failure = {'code': code, 'error': '范围授权已失效或无法核验，已请求取消；已有副作用请核对作业结果'}
                self.cancelled.set()
                return

    def start(self):
        if self.info.get('authorization_id'):
            self.thread = threading.Thread(target=self._watch, daemon=True)
            self.thread.start()
        return self

    def close(self):
        self.closed.set()
        if self.thread:
            # A blocked read must not block result publication. This daemon only
            # reads state; it cannot release slots or publish an execution result.
            self.thread.join(.2)
