"""Windows 桌宠的透明窗口、动作和播放节奏。"""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


SHEET = Path(__file__).with_name('assets') / 'pet' / 'spritesheet.webp'
# 图集行、每帧停留毫秒数、标签。停顿放在动作里的关键姿势上。
ANIMATIONS = {
    'idle': (0, (1900, 110, 180, 2400, 110, 200), ''),
    'running': (6, (320, 170, 190, 340, 170, 300), '正在思考'),
    'awaiting_confirmation': (3, (160, 340, 500, 450), '等待确认'),
    'completed': (4, (180, 190, 260, 300, 200), '已完成'),
    'failed': (5, (180, 180, 240, 1300), '执行失败'),
    'partial': (5, (180, 180, 240, 1300), '部分完成'),
    'interrupted': (5, (180, 180, 240, 1300), '已中断'),
    'limit_reached': (5, (180, 180, 240, 1300), '已达上限'),
    'cancelled': (0, (1900, 110, 180, 2400, 110, 200), '已取消'),
}


class PetWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_QuitOnClose)
        self.setWindowTitle('Genesis 桌宠')
        self.setToolTip('左键拖动 · 右键关闭桌宠')
        self.sheet = QPixmap(str(SHEET))
        if self.sheet.width() != 1536 or self.sheet.height() != 1872 or not self.sheet.hasAlphaChannel():
            raise RuntimeError('无法读取桌宠透明图集。')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.label = QLabel(alignment=Qt.AlignCenter)
        self.label.setStyleSheet('color: white; background: #30343b; border-radius: 8px; padding: 2px 8px;')
        self.sprite = QLabel()
        layout.addWidget(self.label, alignment=Qt.AlignCenter)
        layout.addWidget(self.sprite, alignment=Qt.AlignBottom)
        self.setFixedSize(120, 162)
        for widget in (self.sprite, self.label):
            widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.drag_offset = None
        self.flight_row = None
        self.completion = QTimer(self)
        self.completion.setSingleShot(True)
        self.completion.timeout.connect(lambda: self.set_status('idle'))
        self.clock = QTimer(self)
        self.clock.setSingleShot(True)
        self.clock.timeout.connect(self.advance)
        self.set_status('idle')
        self.mouse_clock = QTimer(self)
        self.mouse_clock.timeout.connect(self.track_cursor)
        self.mouse_clock.start(40)

    def track_cursor(self):
        if self.drag_offset is not None:
            return
        center = self.sprite.mapToGlobal(self.sprite.rect().center())
        offset = QCursor.pos() - center
        dx, dy = offset.x(), offset.y()
        radius = 5 * self.sprite.width()
        if dx == 0 or dx * dx + dy * dy > radius * radius:
            self.set_flight(None)
        else:
            self.set_flight(1 if dx > 0 else 2)

    def set_flight(self, row):
        if row != self.flight_row:
            self.flight_row = row
            self.frame = 0
            self.draw_frame()

    def set_status(self, status):
        self.status = status if status in ANIMATIONS else 'idle'
        self.frame = 0
        self.label.setText(ANIMATIONS[self.status][2])
        self.label.setVisible(bool(self.label.text()))
        self.completion.stop()
        if self.status == 'completed':
            self.completion.start(sum(ANIMATIONS['completed'][1]))
        self.draw_frame()

    def draw_frame(self):
        row, delays, _ = ANIMATIONS[self.status]
        if self.flight_row is not None:
            row, delays = self.flight_row, (105,) * 8
        tile = self.sheet.copy(self.frame * 192, row * 208, 192, 208)
        self.sprite.setPixmap(tile.scaled(120, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.clock.start(delays[self.frame])

    def advance(self):
        count = 8 if self.flight_row is not None else len(ANIMATIONS[self.status][1])
        self.frame = (self.frame + 1) % count
        self.draw_frame()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.close()
        elif event.button() == Qt.LeftButton:
            self.drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self.drag_offset is not None and event.buttons() & Qt.LeftButton:
            target = event.globalPosition().toPoint() - self.drag_offset
            dx = target.x() - self.x()
            if dx:
                self.set_flight(1 if dx > 0 else 2)
            self.move(target)

    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        self.track_cursor()


def run_window(channel):
    app = QApplication([])
    window = PetWindow()
    area = window.screen().availableGeometry()
    window.move(area.right() - window.width() - 24, area.bottom() - window.height() - 24)

    def receive():
        try:
            while channel.poll():
                window.set_status(channel.recv())
        except (EOFError, OSError):
            app.quit()

    timer = QTimer(window)
    timer.timeout.connect(receive)
    timer.start(80)
    window.show()
    channel.send(None)
    app.exec()
