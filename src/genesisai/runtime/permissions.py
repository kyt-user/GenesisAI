"""根据固定 Spec 与运行选项处理工具确认。"""

import time

from genesisai.shared.security import ToolError, digest


class PermissionPolicy:
    """根据固定 Spec 与运行选项处理工具确认和网络授权。"""
    def __init__(self, *, confirm_writes: bool = True, confirm_search: bool = True, confirm_shell: bool = True, store=None):
        self.confirm_writes = confirm_writes
        self.confirm_search = confirm_search
        self.confirm_shell = confirm_shell
        self.store = store
        # 进程本地状态：不从持久化会话数据恢复运行授权。
        self.network_run_id = None
        if self.store is not None:
            self.store.data['run_runtime']['network_grant_scope'] = None

    def requires_confirmation(self, spec) -> bool:
        if spec.permission == 'network' and self.store is not None and self.network_run_id and self.network_run_id == self.store.data.get('run_id'):
            return False
        return (spec.permission == "write" and self.confirm_writes) or (
            spec.permission == "network" and self.confirm_search
        ) or (spec.permission == "shell" and self.confirm_shell)

    def set_mode(self, capability: str, mode: str) -> None:
        """切换当前 CLI 会话的网络或写入确认模式。"""
        if capability not in {"network", "writes", "shell"}:
            raise ValueError("权限类型必须是 network、writes 或 shell")
        if mode not in {"ask", "allow"}:
            raise ValueError("权限模式必须是 ask 或 allow")
        value = mode == "ask"
        if capability == "network":
            self.confirm_search = value
            self.clear_run_grant()
        elif capability == "writes":
            self.confirm_writes = value
        else:
            self.confirm_shell = value
        if self.store is not None:
            self.store.data['permission_modes'][capability] = mode
            self.store.save()

    def allow_network_run(self):
        if self.store is None or self.store.data['status'] not in {'running', 'awaiting_confirmation'}:
            raise ValueError('本轮授权需要正在运行或等待确认的任务')
        self.network_run_id = self.store.data['run_id']
        self.store.data['run_runtime']['network_grant_scope'] = 'run'

    def clear_run_grant(self):
        self.network_run_id = None
        if self.store is not None:
            self.store.data['run_runtime']['network_grant_scope'] = None

    @property
    def mode(self) -> str:
        values = (self.confirm_writes, self.confirm_search, self.confirm_shell)
        if all(values):
            return "ASK"
        if not any(values):
            return "ALLOW"
        return "MIXED"

    @staticmethod
    def freeze(preview: dict) -> tuple[str, float]:
        return digest(preview), time.time() + 900

    @staticmethod
    def validate(preview: dict, prior: dict | None, approved: str | None) -> None:
        if (
            not prior
            or prior.get("expires", 0) < time.time()
            or digest(preview) != prior.get("digest")
            or approved != prior.get("digest")
        ):
            raise ToolError("confirmation_invalid", "确认过期或参数/资源已变化")

