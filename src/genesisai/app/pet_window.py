"""桌宠透明窗口，以及黄色毛球、雷伊各自的动画规则。"""

import math
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtGui import QCursor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget


SHEET = Path(__file__).with_name('assets') / 'pet' / 'bird-atlas.png'
STATUS_LABELS = {
    'idle': '', 'running': '正在工作',
    'awaiting_input': '等待回答', 'awaiting_confirmation': '等待确认',
    'completed': '已完成', 'failed': '执行失败', 'partial': '部分完成',
    'interrupted': '已中断', 'limit_reached': '已达上限', 'cancelled': '已取消',
}
# 图集依次为待机、向左跳、向右跳、八方向视线、敲电脑，每行八帧。
FRAME_COUNT = 8
ROW_COUNT = 5
SPRITE_WIDTH = 120
BASELINE = 115
JUMP_HEIGHT = 24
IDLE_PERIOD = 8100
IDLE_JUMP_START = 1700
PROP_ROWS = {
    'failed': 5, 'interrupted': 5, 'limit_reached': 5,
    'awaiting_input': 6, 'awaiting_confirmation': 7, 'partial': 7, 'completed': 8,
}
# 跳跃期间保持均匀节奏；待机停顿在落地姿势上单独安排。
FRAME_DELAYS = {
    0: (105,) * FRAME_COUNT,
    1: (105,) * FRAME_COUNT,
    2: (105,) * FRAME_COUNT,
    4: (180,) * FRAME_COUNT,
}
JUMP_PERIOD = sum(FRAME_DELAYS[0])


def frame_index(row, elapsed):
    phase = elapsed % sum(FRAME_DELAYS[row])
    for index, duration in enumerate(FRAME_DELAYS[row]):
        if phase < duration:
            return index
        phase -= duration


def align_frame(frame):
    """把图集自身的左右漂移和行间高度差归一到同一落脚位置。"""
    image = frame.toImage().convertToFormat(QImage.Format_RGBA8888)
    visible = [i for i, alpha in enumerate(bytes(image.constBits())[3::4]) if alpha > 128]
    if not visible:
        raise RuntimeError('桌宠图集中存在空白角色。')
    width = image.width()
    left = min(i % width for i in visible)
    right = max(i % width for i in visible)
    bottom = visible[-1] // width
    canvas = QPixmap(SPRITE_WIDTH, SPRITE_WIDTH)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.drawPixmap(round((SPRITE_WIDTH - 1 - left - right) / 2),
                       BASELINE - bottom, frame)
    painter.end()
    return canvas


def load_props():
    sheet = QPixmap(str(SHEET.with_name('bird-props.png')))
    if sheet.isNull() or not sheet.hasAlphaChannel():
        raise RuntimeError('无法读取桌宠道具图集。')
    frames = []
    for row in range(2):
        for column in range(2):
            tile = sheet.copy(column * sheet.width() // 2, row * sheet.height() // 2,
                              sheet.width() // 2, sheet.height() // 2)
            image = tile.toImage().convertToFormat(QImage.Format_RGBA8888)
            # 按可见内容确定画面范围，避免 AI 输出的不同宽高比压扁角色。
            visible = [i for i, alpha in enumerate(bytes(image.constBits())[3::4]) if alpha > 64]
            if not visible:
                raise RuntimeError('桌宠道具图集中存在空白角色。')
            width = image.width()
            left = max(0, min(i % width for i in visible) - 3)
            right = min(width, max(i % width for i in visible) + 4)
            top = max(0, visible[0] // width - 3)
            bottom = min(image.height(), visible[-1] // width + 4)
            sprite = tile.copy(left, top, right - left, bottom - top).scaled(
                94, 96, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            canvas = QPixmap(SPRITE_WIDTH, SPRITE_WIDTH)
            canvas.fill(Qt.transparent)
            painter = QPainter(canvas)
            painter.drawPixmap((SPRITE_WIDTH - sprite.width()) // 2, 116 - sprite.height(), sprite)
            painter.end()
            frames.append([canvas])
    return frames


class PetWindow(QWidget):
    sprite_height = SPRITE_WIDTH
    completion_duration = 4000

    def __init__(self):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_QuitOnClose)
        self.setWindowTitle('Genesis 桌宠')
        self.setToolTip('左键拖动 · 右键关闭桌宠')
        self.frames = self.load_frames()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.label = QLabel(alignment=Qt.AlignCenter)
        self.label.setStyleSheet('color: white; background: #30343b; border-radius: 8px; padding: 2px 8px;')
        self.sprite = QLabel()
        self.sprite.setFixedSize(SPRITE_WIDTH, self.sprite_height)
        layout.addWidget(self.label, alignment=Qt.AlignCenter)
        layout.addWidget(self.sprite, alignment=Qt.AlignBottom)
        self.setFixedSize(SPRITE_WIDTH, self.sprite_height + 32)
        for widget in (self.sprite, self.label):
            widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.drag_offset = None
        self.drag_direction = 1
        self.gaze = None
        self.pose = None
        self.lift = 0.0
        self.last_paint = 0
        self.elapsed = QElapsedTimer()
        self.elapsed.start()
        self.completion = QTimer(self)
        self.completion.setSingleShot(True)
        self.completion.timeout.connect(lambda: self.set_status('idle'))
        self.set_status('idle')
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.advance)
        self.clock.setTimerType(Qt.PreciseTimer)
        self.clock.start(40)

    def load_frames(self):
        sheet = QPixmap(str(SHEET))
        if sheet.isNull() or not sheet.hasAlphaChannel():
            raise RuntimeError('无法读取桌宠透明图集。')
        frames_by_row = []
        for row in range(ROW_COUNT):
            y0 = round(row * sheet.height() / ROW_COUNT)
            y1 = round((row + 1) * sheet.height() / ROW_COUNT)
            frames = []
            for column in range(FRAME_COUNT):
                x0 = round(column * sheet.width() / FRAME_COUNT)
                x1 = round((column + 1) * sheet.width() / FRAME_COUNT)
                tile = sheet.copy(x0, y0, x1 - x0, y1 - y0)
                frames.append(align_frame(tile.scaled(SPRITE_WIDTH, SPRITE_WIDTH,
                                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)))
            frames_by_row.append(frames)
        frames_by_row.extend(load_props())
        return frames_by_row

    def track_cursor(self):
        center = self.sprite.mapToGlobal(self.sprite.rect().center())
        offset = QCursor.pos() - center
        dx, dy = offset.x(), offset.y()
        radius = 3 * SPRITE_WIDTH
        if (dx == 0 and dy == 0) or dx * dx + dy * dy > radius * radius:
            self.gaze = None
        else:
            # 从右侧起逆时针，依次是右、右上、上、左上、左、左下、下、右下。
            self.gaze = round(math.atan2(-dy, dx) / (math.pi / 4)) % FRAME_COUNT

    def current_pose(self, elapsed):
        if self.drag_offset is not None:
            row = 1 if self.drag_direction < 0 else 2
            return row, frame_index(row, elapsed)
        if self.status == 'running':
            return 4, frame_index(4, elapsed)
        if self.status in PROP_ROWS:
            return PROP_ROWS[self.status], 0
        if self.gaze is not None:
            return 3, self.gaze
        # 待机轻跳之间穿插短暂的左右张望。
        phase = elapsed % IDLE_PERIOD
        if 4900 <= phase < 5700:
            return 3, 4
        if 6500 <= phase < 7300:
            return 3, 0
        if IDLE_JUMP_START <= phase < IDLE_JUMP_START + JUMP_PERIOD:
            return 0, frame_index(0, phase - IDLE_JUMP_START)
        return 0, 0

    def jump_height(self, row, elapsed):
        if row in (1, 2):
            phase = (elapsed % JUMP_PERIOD) / JUMP_PERIOD
        elif row == 0:
            phase = (elapsed % IDLE_PERIOD - IDLE_JUMP_START) / JUMP_PERIOD
            if not 0 <= phase <= 1:
                return 0.0
        else:
            return 0.0
        return 4 * JUMP_HEIGHT * phase * (1 - phase)

    def set_status(self, status):
        self.status = status if status in STATUS_LABELS else 'idle'
        self.status_started = self.elapsed.elapsed()
        self.label.setText(STATUS_LABELS[self.status])
        self.label.setVisible(bool(self.label.text()))
        self.completion.stop()
        if self.status == 'completed':
            self.completion.start(self.completion_duration)
        self.draw_frame()

    def draw_frame(self):
        now = self.elapsed.elapsed()
        pose = self.current_pose(now)
        target = self.jump_height(pose[0], now)
        # 鼠标中途移入或松开拖拽时，用单张角色平移落地，避免瞬移。
        step = .15 * max(0, now - self.last_paint)
        lift = self.lift + max(-step, min(step, target - self.lift))
        self.last_paint = now
        if pose != self.pose or round(lift) != round(self.lift):
            self.pose = pose
            frame = self.frames[pose[0]][pose[1]]
            canvas = QPixmap(self.sprite.size())
            canvas.fill(Qt.transparent)
            painter = QPainter(canvas)
            painter.drawPixmap(0, -round(lift), frame)
            painter.end()
            self.sprite.setPixmap(canvas)
        self.lift = lift

    def advance(self):
        self.track_cursor()
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
                self.drag_direction = dx
            self.move(target)
            self.draw_frame()

    def mouseReleaseEvent(self, event):
        self.drag_offset = None
        self.advance()



class RayPetWindow(PetWindow):
    """原版雷伊图集，每种动作保留自身的帧数和停留时间。"""

    sprite_height = 130
    completion_duration = 1130
    delays = {
        0: (1900, 110, 180, 2400, 110, 200),
        1: (105,) * 8,
        2: (105,) * 8,
        3: (160, 340, 500, 450),
        4: (180, 190, 260, 300, 200),
        5: (180, 180, 240, 1300),
        6: (320, 170, 190, 340, 170, 300),
    }
    status_rows = {
        'running': 6, 'awaiting_input': 3, 'awaiting_confirmation': 3,
        'completed': 4, 'failed': 5, 'partial': 5,
        'interrupted': 5, 'limit_reached': 5,
    }

    def load_frames(self):
        sheet = QPixmap(str(SHEET.with_name('spritesheet.webp')))
        if sheet.isNull() or not sheet.hasAlphaChannel() or (sheet.width(), sheet.height()) != (1536, 1872):
            raise RuntimeError('无法读取雷伊透明图集。')
        return [
            [sheet.copy(column * 192, row * 208, 192, 208).scaled(
                SPRITE_WIDTH, self.sprite_height, Qt.KeepAspectRatio, Qt.SmoothTransformation)
             for column in range(len(delays))]
            for row, delays in self.delays.items()
        ]

    def current_pose(self, elapsed):
        if self.drag_offset is not None:
            row = 2 if self.drag_direction < 0 else 1
        elif self.gaze is not None and self.gaze not in (2, 6):
            row = 2 if self.gaze in (3, 4, 5) else 1
        else:
            row = self.status_rows.get(self.status, 0)
        phase = max(0, elapsed - self.status_started) % sum(self.delays[row])
        for column, duration in enumerate(self.delays[row]):
            if phase < duration:
                return row, column
            phase -= duration

    def jump_height(self, row, elapsed):
        return 0.0


def run_window(channel, character='bird'):
    app = QApplication([])
    window = RayPetWindow() if character == 'ray' else PetWindow()
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
