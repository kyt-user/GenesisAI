"""Explicit endpoint/model image support; unknown models fail closed for image input.

Verified documentation (2026-11-05):
https://platform.qianwenai.com/docs/token-plan/best-practices/add-vision-skill
https://help.aliyun.com/en/model-studio/qwen-function-calling
"""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ImageCapability:
    supported: bool | None = None
    reason: str = '尚未确认该模型/端点的图片输入能力'


def image_capability(provider, model, base_url):
    url = urlsplit(str(base_url))
    if url.scheme != 'https' or url.port not in (None, 443):
        return ImageCapability()
    if provider == 'qwen_token_plan' and url.hostname == 'token-plan.maas.qianwenaiapi.com' and url.path.rstrip('/') == '/compatible-mode/v1':
        if model in {'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus'}:
            return ImageCapability(True, '支持文字与图片')
    if provider == 'qwen' and url.hostname in {'dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com', 'dashscope-us.aliyuncs.com'} and url.path.rstrip('/') == '/compatible-mode/v1':
        if model in {'qwen3-vl-plus', 'qwen3-vl-flash', 'qwen3.8-max', 'qwen3.8-flash', 'qwen3.7-plus'}:
            return ImageCapability(True, '支持文字与图片')
    if provider == 'deepseek' and url.hostname == 'api.deepseek.com' and model in {'deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash'}:
        return ImageCapability(False, '当前接入仅支持文字，请选择已确认支持图片的模型')
    return ImageCapability()
