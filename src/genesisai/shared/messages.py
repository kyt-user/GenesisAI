"""模型对话与工具调用使用的数据对象。"""

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict
import re


class TextPart(TypedDict):
    type: Literal['text']
    text: str


class ImagePart(TypedDict):
    type: Literal['image']
    image_ref: str
    name: str
    mime_type: str
    width: int
    height: int
    byte_size: int


Content = str | list[TextPart | ImagePart] | None


def validate_content(content: Content) -> None:
    if content is None or isinstance(content, str):
        return
    if not isinstance(content, list) or not content:
        raise ValueError('消息内容必须是文字或非空文字/图片块列表')
    for part in content:
        if not isinstance(part, dict):
            raise ValueError('消息内容块必须是对象')
        if part.get('type') == 'text':
            if set(part) != {'type', 'text'} or not isinstance(part['text'], str):
                raise ValueError('文字块无效')
        elif part.get('type') == 'image':
            if set(part) != {'type', 'image_ref', 'name', 'mime_type', 'width', 'height', 'byte_size'}:
                raise ValueError('图片块字段无效')
            if not isinstance(part['image_ref'], str) or not re.fullmatch(r'src_[a-f0-9]{16}', part['image_ref']):
                raise ValueError('图片引用无效')
            if part['mime_type'] != 'image/png' or not isinstance(part['name'], str) or not part['name'] or len(part['name']) > 200:
                raise ValueError('图片格式或名称无效')
            if any(type(part[k]) is not int or part[k] < 1 for k in ('width', 'height', 'byte_size')):
                raise ValueError('图片尺寸或大小无效')
        else:
            raise ValueError('当前仅支持文字和图片输入')


def content_text(content: Content, *, images: bool = False) -> str:
    validate_content(content)
    if not isinstance(content, list):
        return content or ''
    return '\n'.join(p['text'] if p['type'] == 'text' else
                     f"[图片：{p['name']} · {p['image_ref']}]"
                     for p in content if p['type'] == 'text' or images)


def image_parts(content: Content) -> list[ImagePart]:
    validate_content(content)
    return [p for p in content if p['type'] == 'image'] if isinstance(content, list) else []


def image_tokens(part: ImagePart) -> int:
    # Conservative Qwen patch estimate, calibrated against actual provider usage.
    return ((part['width'] + 27) // 28) * ((part['height'] + 27) // 28) + 64


@dataclass(frozen=True)
class ImageAttachment:
    """Validated, normalized bytes frozen before enqueueing; never persisted inline."""
    name: str
    width: int
    height: int
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class UserInput:
    text: str = ''
    images: tuple[ImageAttachment, ...] = ()

    def __post_init__(self):
        if not isinstance(self.text, str) or not isinstance(self.images, tuple) or any(not isinstance(p, ImageAttachment) for p in self.images):
            raise ValueError('UserInput 需要文字和不可变的图片附件元组')

    @property
    def preview(self) -> str:
        return '\n'.join([self.text, *[f'[图片：{p.name}]' for p in self.images]]).strip()


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(slots=True)
class Message:
    role: str
    content: Content = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class Response:
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
