"""Thread-safe drafts: frozen pixels survive queueing and registration failures."""
from threading import Lock

from genesisai.core.media.images import prepare_path
from genesisai.shared.messages import UserInput


class Attachments:
    def __init__(self):
        self._images = []
        self._lock = Lock()

    def add(self, path, access, config):
        image = prepare_path(path, access, config)
        with self._lock:
            if sum(p.storage_bytes for p in self._images) + image.storage_bytes > config.max_pending_image_bytes:
                raise ValueError('待发送原图与预览超过附件内存配额；可分次发送或调整 max_pending_image_bytes')
            self._images.append(image)
        return image

    def request(self, text):
        with self._lock:
            return UserInput(text, tuple(self._images))

    def consume(self, images):
        with self._lock:
            identities = {id(p) for p in images}
            self._images = [p for p in self._images if id(p) not in identities]

    def clear(self):
        with self._lock:
            self._images.clear()

    def summary(self):
        return [{'name': p.name, 'width': p.width, 'height': p.height, 'bytes': len(p.data)}
                for p in self.request('').images]
