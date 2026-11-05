"""根据固定 Spec 与运行选项处理工具确认。"""

import time

from genesisai.shared.security import ToolError, digest


class PermissionPolicy:
    def __init__(self, mode='user'):
        if mode not in {'user', 'auto'}:
            raise ValueError('权限模式必须是 user 或 auto')
        self.mode = mode

    def requires_confirmation(self, descriptor, args) -> bool:
        if descriptor.category == 'kernel' and descriptor.name == 'run_commands' and args.get('operation') == 'inspect':
            return False
        spec = descriptor.spec
        return self.mode == 'user' and (spec.launches_service or spec.network or spec.side_effect or spec.permission not in {'read', 'core'})

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

