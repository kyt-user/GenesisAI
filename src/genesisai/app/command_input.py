"""Terminal input and the shared command catalogue."""
import asyncio
from collections import deque
from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.cells import chop_cells

from genesisai.app.run_queue import PREVIEW_ROWS
from genesisai.app.terminal_text import terminal_text


COMMANDS = [
    ('/sessions', '继续历史会话', '会话'),
    ('/new', '返回工作区首页', '会话'),
    ('/pet', '选择或切换 Windows 桌宠', '会话'),
    ('/workspace', '查看或切换工作区', '项目'),
    ('/auth', '切换模型提供商和配置密钥', '模型与权限'),
    ('/model', '切换当前提供商的模型', '模型与权限'),
    ('/permissions', '查看操作权限', '模型与权限'),
    ('/compact', '压缩当前上下文', '会话'),
    ('/help', '查看全部操作', '帮助'),
    ('/status', '查看会话、工作区和运行状态', '诊断'),
    ('/budget', '查看时限和上下文用量', '诊断'),
    ('/history', '查看当前对话历史', '会话'),
    ('/logs', '查看完整日志路径', '诊断'),
    ('/trace', '查看执行摘要', '诊断'),
    ('/details', '展开或收起工具输出', '诊断'),
    ('/diff', '查看本轮文件变更', '项目'),
    ('/project', '查看项目任务记录', '项目'),
    ('/project rebuild', '重建项目结构索引', '项目'),
    ('/memory', '查看知识库；支持 search/read/write/delete/export/rebuild/diagnostic/backup', '项目'),
    ('/tools', '查看可用工具', '项目'),
    ('/skills', '列出可加载的 Skill', '项目'),
    ('/workflow', '列出工作区工作流', '项目'),
    ('/permissions user', '用户审批，写入与对外调用逐次确认', '模型与权限'),
    ('/permissions auto', '自动审批，允许读写宿主文件', '模型与权限'),
    ('/answer', '回答当前问题，可输入选项编号或自由文本', '运行'),
    ('/approve', '允许当前操作一次', '运行'),
    ('/reject', '拒绝当前操作', '运行'),
    ('/resume', '继续中断的运行', '运行'),
    ('/cancel', '取消当前任务', '运行'),
    ('/queue', '查看排队消息', '运行'),
    ('/queue resume', '继续已暂停的消息队列', '运行'),
    ('/queue clear', '移除尚未开始的排队消息', '运行'),
    ('/exit', '保存会话并退出', '会话'),
    ('/attach', '添加本地图片附件：/attach 路径', '图片'),
    ('/attachments', '查看待发送图片', '图片'),
    ('/attachments clear', '移除待发图片，保留原文件', '图片'),
    ('/send', '发送已附加图片，可不带文字', '图片'),
]


class CommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith('/') or '\n' in text or document.text_after_cursor:
            return
        query = text[1:].casefold()
        for command, description, group in COMMANDS:
            if query in f'{command[1:]} {description} {group}'.casefold():
                yield Completion(command, start_position=-len(text),
                                 display_meta=f'{group} · {description}')


def input_bindings(palette=False, recent=False):
    bindings = KeyBindings()

    if recent:
        from prompt_toolkit.application.current import get_app

        @bindings.add('up', filter=Condition(lambda: not get_app().current_buffer.text))
        def select_recent(event):
            event.app.exit(result='/recent')

    @bindings.add('enter')
    def submit(event):
        buffer = event.current_buffer
        if buffer.complete_state:
            completion = buffer.complete_state.current_completion
            if completion is None and buffer.complete_state.completions:
                completion = buffer.complete_state.completions[0]
            if completion is not None:
                buffer.apply_completion(completion)
        buffer.validate_and_handle()

    @bindings.add('escape', 'enter')
    @bindings.add('c-j')
    def newline(event):
        event.current_buffer.insert_text('\n')

    @bindings.add('tab')
    def complete(event):
        buffer = event.current_buffer
        if buffer.complete_state:
            selected = buffer.complete_state.current_completion or buffer.complete_state.completions[0]
            buffer.apply_completion(selected)
        else:
            buffer.start_completion(select_first=True)

    @bindings.add('escape')
    def dismiss(event):
        if palette:
            event.app.exit(result='')
        else:
            event.current_buffer.cancel_completion()

    @bindings.add('c-c')
    def clear_or_exit(event):
        if event.current_buffer.text:
            event.current_buffer.reset()
        else:
            event.app.exit(exception=KeyboardInterrupt())

    return bindings


class TaskInput:
    def __init__(self, history=(), *, input=None, output=None):
        self.session = PromptSession(
            history=InMemoryHistory(history), completer=CommandCompleter(),
            complete_while_typing=True, multiline=True,
            key_bindings=input_bindings(), reserve_space_for_menu=7,
            input=input, output=output,
            style=Style.from_dict({
                'prompt': 'ansicyan bold',
                'bottom-toolbar': 'noreverse',
                'completion-menu.completion.current': 'reverse bold',
            }),
        )

    def read_running(self, runs, render, draft):
        app = self.session.app
        output = deque()

        def write_output():
            while output:
                render(*output.popleft())

        async def refresh():
            while not app.is_done:
                if not runs.events.empty():
                    previous = runs.stage, runs.preview
                    runs.drain(lambda kind, value: output.append((kind, value)), limit=128)
                    if output:
                        await run_in_terminal(write_output)
                    elif previous != (runs.stage, runs.preview):
                        app.invalidate()
                if runs.future.done():
                    if not app.is_done:
                        app.exit(result=None)
                    return
                await asyncio.sleep(.05)

        def prompt():
            width = max(1, app.output.get_size().columns - 1)
            rows = []
            for line in terminal_text(runs.preview).split('\n'):
                rows.extend(chop_cells(line.expandtabs(), width) or [''])
            rows = rows[-PREVIEW_ROWS:]
            rows.extend([''] * (PREVIEW_ROWS - len(rows)))
            return [('', '\n'.join(rows) + '\n'), ('class:prompt', '› ')]

        app.erase_when_done = True
        try:
            result = self.session.prompt(
                prompt, default=draft, key_bindings=input_bindings(),
                bottom_toolbar=lambda: f'{runs.stage} · 排队 {len(runs.pending)} · Enter 排队 · /cancel 取消',
                pre_run=lambda: app.create_background_task(refresh()),
            )
        finally:
            app.erase_when_done = False
            # Enter or Ctrl+C can close the prompt while a scrollback write is pending.
            write_output()
        return result, self.session.default_buffer.text if result is None else ''

    def read(self, status='', *, default='', recent=False):
        return self.session.prompt(
            [('class:prompt', '› ')], default=default,
            prompt_continuation='  ',
            key_bindings=input_bindings(palette=default == '/', recent=recent),
            bottom_toolbar=lambda: status + '\nEnter 发送 · Alt+Enter 换行 · / 操作 · Ctrl+C 清空或退出',
            pre_run=(lambda: self.session.default_buffer.start_completion()) if default == '/' else None,
        )
