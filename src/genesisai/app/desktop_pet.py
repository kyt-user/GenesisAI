"""CLI 只向桌宠进程发送会话状态；Qt 在子进程中加载。"""

import multiprocessing
import sys


PET_CHOICES = (
    ('bird', '黄色毛球', '跳动、视线跟随、敲电脑与道具提醒'),
    ('ray', '雷伊', '眨眼、左右飞行、第七行思考动作'),
)


def _run_window(channel, character):
    try:
        from genesisai.app.pet_window import run_window
        run_window(channel, character)
    except ImportError as exc:
        channel.send(f'无法加载桌宠依赖，请运行 python -m pip install -e ".[pet]"。{exc}')
    except (OSError, RuntimeError) as exc:
        channel.send(str(exc))
    finally:
        channel.close()


class DesktopPet:
    def __init__(self):
        self.status = 'idle'
        self.process = None
        self.channel = None
        self.character = 'bird'

    def show(self, character):
        if character not in {choice[0] for choice in PET_CHOICES}:
            raise ValueError('未知桌宠角色。')
        if sys.platform != 'win32':
            raise ValueError('/pet 目前仅支持 Windows。')
        if self.process is not None and self.process.is_alive() and character == self.character:
            return
        self.close()
        context = multiprocessing.get_context('spawn')
        self.channel, child = context.Pipe()
        try:
            self.process = context.Process(target=_run_window, args=(child, character), name='GenesisPet', daemon=True)
            self.process.start()
            child.close()
            if not self.channel.poll(20):
                raise RuntimeError('桌宠窗口启动超时。')
            try:
                error = self.channel.recv()
            except EOFError as exc:
                raise RuntimeError('桌宠窗口未能启动。') from exc
            if error:
                raise RuntimeError(error)
            self.character = character
            self.update(self.status)
        except BaseException:
            self.close()
            raise
        finally:
            child.close()

    def update(self, status):
        self.status = status
        if self.process is not None and self.process.is_alive():
            try:
                self.channel.send(status)
            except OSError:
                pass  # 用户可以在 CLI 运行中单独关闭桌宠。

    def close(self):
        if self.channel is not None:
            self.channel.close()
            self.channel = None
        if self.process is not None:
            if self.process.pid is not None:
                self.process.join(timeout=1)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join()
            self.process.close()
            self.process = None
