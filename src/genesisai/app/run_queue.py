"""One worker owns the runner. Only the terminal thread changes the FIFO."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from threading import Lock

from genesisai.shared.run_control import RunControl
from genesisai.shared.messages import ImageAttachment


PREVIEW_ROWS = 4


@dataclass
class QueueItem:
    id: str
    text: str
    run_id: str | None = None
    images: tuple[ImageAttachment, ...] = ()


class CommandPreview:
    """One bounded screen update; the complete stream is captured separately."""

    def __init__(self):
        self.text = ''
        self.lines = 0
        self.omitted = False

    def append(self, text):
        if self.omitted:
            return
        piece = text[:4096 - len(self.text)]
        lines = piece.splitlines(keepends=True)
        piece = ''.join(lines[:32 - self.lines])
        self.text += piece
        self.lines += piece.count('\n')
        self.omitted = len(piece) != len(text)


class RunQueue:
    def __init__(self):
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='genesis-run')
        self.future = None
        self.control = None
        self.pending = deque()
        self.starting = None
        self.paused = False
        self.events = SimpleQueue()
        self._output_lock = Lock()
        self._output = None
        self.stage = ''
        self.preview = ''

    def emit(self, kind, value):
        with self._output_lock:
            if kind == 'command_output':
                if self._output is None:
                    self._output = CommandPreview()
                    self.events.put((kind, self._output))
                self._output.append(value['text'])
            else:
                self._output = None
                self.events.put((kind, value))

    def flush_preview(self, render):
        if self.preview:
            render('stream_token', {'text': self.preview})
            self.preview = ''

    def drain(self, render, *, limit=None):
        count = 0
        while limit is None or count < limit:
            try:
                kind, value = self.events.get_nowait()
            except Empty:
                return
            count += 1
            if kind == 'command_output':
                with self._output_lock:
                    if value is self._output:
                        self._output = None
                    text = value.text
                    if value.omitted:
                        text += '\n… 部分命令输出已省略，/logs 可查看完整记录。\n'
                render(kind, {'text': text})
                continue
            if kind == 'model':
                self.flush_preview(render)
                self.stage = f"等待模型 · 第 {value['round']} 轮"
            elif kind == 'reasoning_token':
                if self.stage == '模型正在规划':
                    continue
                self.stage = '模型正在规划'
            elif kind == 'tool_start':
                self.flush_preview(render)
                self.stage = value['name']
            elif kind == 'stream_token':
                self.stage = '模型正在回复'
                self.preview += value['text']
                if '\n' in value['text'] and self.preview.count('\n') >= PREVIEW_ROWS:
                    complete, newline, tail = self.preview.rpartition('\n')
                    render(kind, {'text': complete + newline})
                    self.preview = tail
                continue
            render(kind, value)

    def start(self, action):
        if self.future is not None:
            raise RuntimeError('当前任务尚未收尾')
        self.stage = '正在启动'
        self.preview = ''
        self.control = RunControl(self.emit)
        self.future = self.worker.submit(self.control.execute, action)

    def finish(self):
        future, self.future = self.future, None
        item, self.starting = self.starting, None
        stopped = self.control.cancelled.is_set()
        self.control = None
        try:
            result = future.result()
        except BaseException:
            if item is not None and item.run_id is None:
                self.pending.appendleft(item)
            self.paused = bool(self.pending)
            raise
        self.paused = bool(self.pending) and (stopped or result['status'] not in {'completed', 'awaiting_confirmation'})
        return result

    def close(self):
        if self.control:
            self.control.cancel()
        self.worker.shutdown(wait=True)
