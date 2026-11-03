"""Terminal input and the shared command catalogue."""
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style


COMMANDS = [
    ('/sessions', '继续历史会话', '会话'),
    ('/new', '开始新会话', '会话'),
    ('/model', '选择模型', '模型与权限'),
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
    ('/project init', '初始化项目任务记录', '项目'),
    ('/tools', '查看可用工具', '项目'),
    ('/skills', '列出可加载的 Skill', '项目'),
    ('/workflow', '列出工作区工作流', '项目'),
    ('/auth', '配置提供商和密钥', '模型与权限'),
    ('/allow writes', '允许本会话的文件写入', '模型与权限'),
    ('/allow shell', '允许本会话的命令执行', '模型与权限'),
    ('/allow network', '允许本会话的网络请求', '模型与权限'),
    ('/allow network run', '允许本轮的网络请求', '模型与权限'),
    ('/permissions writes ask', '恢复逐次确认文件写入', '模型与权限'),
    ('/permissions shell ask', '恢复逐次确认命令执行', '模型与权限'),
    ('/permissions network ask', '恢复逐次确认网络请求', '模型与权限'),
    ('/approve', '允许当前操作一次', '运行'),
    ('/reject', '拒绝当前操作', '运行'),
    ('/resume', '继续中断的运行', '运行'),
    ('/cancel', '取消当前任务', '运行'),
    ('/exit', '保存会话并退出', '会话'),
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


def input_bindings(palette=False):
    bindings = KeyBindings()

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

    def read(self, status='', *, default=''):
        return self.session.prompt(
            [('class:prompt', '› ')], default=default,
            prompt_continuation='  ',
            key_bindings=input_bindings(palette=default == '/'),
            bottom_toolbar=lambda: status + '\nEnter 发送 · Alt+Enter 换行 · / 操作 · Ctrl+C 清空或退出',
            pre_run=(lambda: self.session.default_buffer.start_completion()) if default == '/' else None,
        )
