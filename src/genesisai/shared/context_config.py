"""Explicit request-capacity configuration; defaults are application policy."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ContextConfig:
    # Conservative application defaults, NOT claims about a provider's model capacity.
    max_input_tokens: int = 400000
    context_window: int = 400000
    output_reserve_tokens: int = 4096
    safety_margin_tokens: int = 1024
    history_tokens: int = 32000
    preserve_recent_tokens: int = 20000
    summary_max_tokens: int = 2048
    project_context_tokens: int = 8000
    strategy: str = 'agentic'
    max_images: int | None = None
    max_image_bytes: int = 10485760
    max_image_pixels: int = 32000000
    image_max_side: int = 1536
    max_request_image_bytes: int = 12582912
    max_pending_image_bytes: int = 134217728

    @classmethod
    def parse(cls, value=None):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - cls.__dataclass_fields__.keys():
            raise ValueError('context 包含未知字段或不是对象')
        config = cls(**value)
        for key in cls.__dataclass_fields__:
            item = getattr(config, key)
            if key == 'max_images' and item is None:
                continue
            if key != 'strategy' and (type(item) is not int or item < 1):
                raise ValueError(f'context.{key} 必须为正整数')
        if config.strategy not in {'basic', 'agentic'}:
            raise ValueError('context.strategy 必须为 basic 或 agentic')
        if config.input_limit <= 0:
            raise ValueError('context 输入预算不足以预留输出和安全余量')
        return config

    @property
    def input_limit(self):
        return min(self.max_input_tokens, self.context_window - self.output_reserve_tokens) - self.safety_margin_tokens
