"""Bounded image ingestion and immutable session assets, shared by CLI/tools/MCP."""
import base64
import hashlib
import io
from pathlib import Path
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from genesisai.shared.context_config import ContextConfig
from genesisai.shared.messages import ImageAttachment, validate_content
from genesisai.shared.security import ToolError


FORMATS = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp'}


def prepare_image(data: bytes, name: str, config: ContextConfig, *, mime_type=None) -> ImageAttachment:
    if not data or len(data) > config.max_image_bytes:
        raise ValueError('图片为空或超过单张文件大小限制')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in FORMATS:
                    raise ValueError('仅支持静态 PNG、JPEG 和 WebP 图片')
                if mime_type is not None and mime_type != FORMATS[original.format]:
                    raise ValueError('图片 MIME 与实际内容不一致')
                if original.width * original.height > config.max_image_pixels:
                    raise ValueError('图片像素超过解码上限')
                if getattr(original, 'n_frames', 1) != 1:
                    raise ValueError('当前不支持动画图片或视频')
                original.load()
                normalized = ImageOps.exif_transpose(original).convert('RGBA' if 'A' in original.getbands() or 'transparency' in original.info else 'RGB')
                normalized.thumbnail((config.image_max_side, config.image_max_side), Image.Resampling.LANCZOS)
                normalized.info.clear()
                output = io.BytesIO()
                normalized.save(output, format='PNG')
                payload = output.getvalue()
                if len(payload) > config.max_image_bytes:
                    raise ValueError('规范化后的图片超过大小限制，请缩小图片')
                return ImageAttachment(Path(name).name[:200] or 'image.png', normalized.width, normalized.height, payload)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('图片损坏或无法安全解码') from exc


def prepare_path(path, access, config) -> ImageAttachment:
    path = access.read(path)
    if not path.is_file():
        raise ValueError('图片路径必须是文件')
    with path.open('rb') as stream:
        raw = stream.read(config.max_image_bytes + 1)
    return prepare_image(raw, path.name, config)


class ImageStore:
    def __init__(self, store, access):
        self.store, self.access = store, access
        self.config = ContextConfig()

    @property
    def root(self):
        return self.store.root / 'images' / self.store.id

    def import_image(self, image: ImageAttachment) -> dict:
        # Revalidate public input; callers cannot smuggle arbitrary bytes/metadata.
        image = prepare_image(image.data, image.name, self.config, mime_type='image/png')
        digest = hashlib.sha256(image.data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (digest + '.png')
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('已有图片快照损坏，未覆盖原文件')
        else:
            with path.open('xb') as stream:
                stream.write(image.data)
        ref = self.store.source(kind='image', sha256=digest, name=image.name, mime_type='image/png',
                                width=image.width, height=image.height, byte_size=len(image.data))
        return self.part(ref)

    def part(self, ref):
        item = self.store.data['sources'].get(ref)
        if not item or item.get('kind') != 'image':
            raise ValueError('图片引用不存在于当前会话')
        part = {'type': 'image', 'image_ref': ref, **{k: item[k] for k in ('name', 'mime_type', 'width', 'height', 'byte_size')}}
        validate_content([part])
        return part

    def data_url(self, part):
        canonical = self.part(part['image_ref'])
        if part != canonical:
            raise ValueError('图片引用元数据与快照不一致')
        if (max(part['width'], part['height']) > self.config.image_max_side
                or part['width'] * part['height'] > self.config.max_image_pixels
                or part['byte_size'] > self.config.max_image_bytes):
            raise ValueError('历史图片超过当前图片限制，请重新附加以按当前配置缩放')
        item = self.store.data['sources'][part['image_ref']]
        path = self.root / (item['sha256'] + '.png')
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError(f"图片快照缺失：{part['name']}；请重新附加原图")
        with path.open('rb') as stream:
            payload = stream.read(self.config.max_image_bytes + 1)
        if len(payload) != part['byte_size'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
            raise ValueError(f"图片快照已损坏或超限：{part['name']}；请重新附加原图")
        return 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')

    def read(self, *, path=None, image_ref=None):
        if bool(path) == bool(image_ref):
            raise ToolError('validation_error', '必须且只能提供 path 或 image_ref')
        if image_ref:
            part = self.part(image_ref)
            self.data_url(part)
        else:
            part = self.import_image(prepare_path(path, self.access, self.config))
        return {'images': [part], 'source_refs': [part['image_ref']]}

    def from_mcp(self, block):
        data = block.get('data')
        if not isinstance(data, str) or len(data) > ((self.config.max_image_bytes + 2) // 3) * 4:
            raise ValueError('MCP 图片数据无效或超过大小限制')
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError('MCP 图片 Base64 无效') from exc
        return self.import_image(prepare_image(raw, 'mcp-image.png', self.config, mime_type=block.get('mimeType', '')))
