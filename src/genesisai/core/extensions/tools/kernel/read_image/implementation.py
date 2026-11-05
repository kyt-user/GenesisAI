from genesisai.shared.security import ToolError


class Implementation:
    def prepare(self, context, args):
        return None

    def execute(self, context, args):
        if not getattr(context.runtime, 'supports_images', False):
            raise ToolError('image_unsupported', '当前模型尚未确认支持图片，请通过 /model 选择图片模型')
        try:
            return context.runtime.images.read(**args)
        except ValueError as exc:
            raise ToolError('invalid_image', str(exc)) from exc
