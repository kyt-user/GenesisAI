"""根据固定 Spec 与运行选项处理工具确认。"""

from contextvars import ContextVar

from genesisai.shared.security import ToolError, digest
from genesisai.core.tools.authorization import destructive


accepted_tool = ContextVar("accepted_tool", default=None)
current_authorization = ContextVar('current_authorization', default=None)


class PermissionPolicy:
    def __init__(self, mode='user', authorization=None):
        if mode not in {'user', 'auto'}:
            raise ValueError('权限模式必须是 user 或 auto')
        self.mode = mode
        self.authorization = authorization

    def authorization_for(self, descriptor, args, preview=None):
        return (self.authorization.match(descriptor, args, preview)
                if (self.mode == 'user' or descriptor.spec.network_boundary == 'private')
                and self.authorization is not None else None)

    def requires_confirmation(self, descriptor, args, preview=None) -> bool:
        if descriptor.spec.requires_acceptance:
            return True
        if destructive(args):
            return True
        spec = descriptor.spec
        if spec.network_boundary == 'private':
            return self.authorization_for(descriptor, args, preview) is None
        needed = self.mode == 'user' and (spec.launches_service or spec.network or spec.side_effect or spec.permission not in {'read', 'core'})
        return needed and self.authorization_for(descriptor, args, preview) is None

    @staticmethod
    def freeze(preview: dict) -> str:
        return digest(preview)

    @staticmethod
    def validate(preview: dict, prior: dict | None, approved: str | None) -> None:
        if (
            not prior
            or digest(preview) != prior.get("digest")
            or approved != prior.get("digest")
        ):
            raise ToolError("confirmation_invalid", "确认不匹配或参数/资源已变化")

