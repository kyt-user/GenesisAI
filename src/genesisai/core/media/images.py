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


def prepare_image(data: bytes, name: str, config: ContextConfig, *, mime_type=None, crop=None, max_side=None) -> ImageAttachment:
    if not data or len(data) > config.max_image_bytes:
        raise ValueError('图片为空或超过单张文件大小限制')
    max_side = config.image_max_side if max_side is None else max_side
    if type(max_side) is not int or max_side < 1:
        raise ValueError('max_side 必须为正整数')
    if crop is not None and (not isinstance(crop, dict) or set(crop) != {'x', 'y', 'width', 'height'}
                            or any(type(v) is not int for v in crop.values())
                            or min(crop['x'], crop['y']) < 0 or min(crop['width'], crop['height']) < 1):
        raise ValueError('crop 需要非负整数 x/y 和正整数 width/height')
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
                original_width, original_height = normalized.size
                region = None
                if crop is not None:
                    x, y, width, height = (crop[k] for k in ('x', 'y', 'width', 'height'))
                    if x + width > normalized.width or y + height > normalized.height:
                        raise ValueError('crop 超出按 EXIF 方向校正后的原图范围')
                    region = (x, y, width, height)
                    normalized = normalized.crop((x, y, x + width, y + height))
                normalized.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                normalized.info.clear()
                output = io.BytesIO()
                normalized.save(output, format='PNG')
                payload = output.getvalue()
                if len(payload) > config.max_image_bytes:
                    raise ValueError('规范化后的图片超过大小限制，请缩小图片')
                return ImageAttachment(Path(name).name[:200] or 'image.png', normalized.width, normalized.height, payload,
                                       data, FORMATS[original.format], original_width, original_height, region, max_side)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('图片损坏或无法安全解码') from exc


def prepare_path(path, access, config, *, crop=None, max_side=None) -> ImageAttachment:
    path = access.read(path)
    if not path.is_file():
        raise ValueError('图片路径必须是文件')
    with path.open('rb') as stream:
        raw = stream.read(config.max_image_bytes + 1)
    return prepare_image(raw, path.name, config, crop=crop, max_side=max_side)


class ImageStore:
    def __init__(self, store, access):
        self.store, self.access = store, access
        self.config = ContextConfig()

    @property
    def root(self):
        return self.store.root / 'assets' / 'images'

    def import_image(self, image: ImageAttachment) -> dict:
        # Revalidate public input; callers cannot smuggle arbitrary bytes/metadata.
        crop = dict(zip(('x', 'y', 'width', 'height'), image.region)) if image.region is not None else None
        image = prepare_image(image.original, image.name, self.config, mime_type=image.original_mime_type,
                              crop=crop, max_side=image.max_side)
        digest = hashlib.sha256(image.data).hexdigest()
        original_digest = hashlib.sha256(image.original).hexdigest()
        self._save_snapshot(self.root / 'originals' / original_digest, image.original, original_digest)
        self._save_snapshot(self.root / (digest + '.png'), image.data, digest)
        ref = self.store.source(kind='image', sha256=digest, name=image.name, mime_type='image/png',
                                width=image.width, height=image.height, byte_size=len(image.data),
                                original_sha256=original_digest, original_byte_size=len(image.original),
                                original_mime_type=image.original_mime_type, original_width=image.original_width,
                                original_height=image.original_height, region=list(image.region) if image.region is not None else None,
                                max_side=image.max_side)
        return self.part(ref)

    def _save_snapshot(self, path, data, digest):
        if path.parent.resolve() != path.parent:
            raise ValueError('图片资产目录不能通过链接重定向')
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('图片快照不能是链接')
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError('已有图片快照损坏，未覆盖原文件')
        else:
            with path.open('xb') as stream:
                stream.write(data)

    def part(self, ref):
        item = self.store.record.sources.get(ref)
        if item is None:
            # Exact references may be reloaded across sessions in the same workspace.
            import json
            rows = self.store.database.rows('SELECT r.payload FROM resources r JOIN sessions s ON r.session_id=s.id WHERE r.id=? AND r.collection=? AND s.project_id=? AND s.workspace_id=? LIMIT 1',
                (ref, 'sources', self.store.record.project_id, self.store.record.workspace_id))
            if rows:
                candidate = json.loads(rows[0]['payload'])
                if candidate.get('kind') == 'image':
                    item = candidate
                    self.store.record.sources[ref] = item
                    self.store.commit()
        if not item or item.get('kind') != 'image':
            raise ValueError('图片引用不存在于当前会话')
        part = {'type': 'image', 'image_ref': ref, **{k: item[k] for k in ('name', 'mime_type', 'width', 'height', 'byte_size')}}
        validate_content([part])
        return part

    def data_url(self, part):
        canonical = self.part(part['image_ref'])
        if part != canonical:
            raise ValueError('图片引用元数据与快照不一致')
        if (part['width'] * part['height'] > self.config.max_image_pixels
                or part['byte_size'] > self.config.max_image_bytes):
            raise ValueError('图片视图超过当前资源预算，请用 read_image 的 crop/max_side 创建更小视图')
        item = self.store.record.sources[part['image_ref']]
        path = self.root / (item['sha256'] + '.png')
        if not path.is_file() or path.is_symlink() or path.resolve() != path:
            raise ValueError(f"图片快照缺失：{part['name']}；请重新附加原图")
        with path.open('rb') as stream:
            payload = stream.read(self.config.max_image_bytes + 1)
        if len(payload) != part['byte_size'] or hashlib.sha256(payload).hexdigest() != item['sha256']:
            raise ValueError(f"图片快照已损坏或超限：{part['name']}；请重新附加原图")
        return 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')

    def read(self, *, path=None, image_ref=None, crop=None, max_side=None):
        if bool(path) == bool(image_ref):
            raise ToolError('validation_error', '必须且只能提供 path 或 image_ref')
        if image_ref:
            part = self.part(image_ref)
            if crop is None and max_side is None:
                self.data_url(part)
            else:
                item = self.store.record.sources[image_ref]
                raw_path = self.root / 'originals' / item['original_sha256']
                if raw_path.is_symlink() or raw_path.resolve() != raw_path or not raw_path.is_file():
                    raise ValueError('原图快照缺失或路径无效')
                with raw_path.open('rb') as stream:
                    raw = stream.read(self.config.max_image_bytes + 1)
                if len(raw) != item['original_byte_size'] or hashlib.sha256(raw).hexdigest() != item['original_sha256']:
                    raise ValueError('原图快照损坏或超过当前资源预算')
                # Coordinates always address the oriented original, including
                # when the reference itself names an earlier cropped view.
                part = self.import_image(prepare_image(raw, item['name'], self.config,
                    mime_type=item['original_mime_type'], crop=crop, max_side=max_side))
        else:
            part = self.import_image(prepare_path(path, self.access, self.config, crop=crop, max_side=max_side))
        item = self.store.record.sources[part['image_ref']]
        return {'images': [part], 'source_refs': [part['image_ref']],
                'original': {key: item['original_' + key] for key in ('width', 'height', 'sha256', 'byte_size', 'mime_type')},
                'region': item['region'], 'max_side': item['max_side']}

    def from_mcp(self, block):
        data = block.get('data')
        if not isinstance(data, str) or len(data) > ((self.config.max_image_bytes + 2) // 3) * 4:
            raise ValueError('MCP 图片数据无效或超过大小限制')
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError as exc:
            raise ValueError('MCP 图片 Base64 无效') from exc
        return self.import_image(prepare_image(raw, 'mcp-image.png', self.config, mime_type=block.get('mimeType', '')))
