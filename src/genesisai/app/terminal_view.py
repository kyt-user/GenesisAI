"""GenesisAI 命令行界面的 Rich 展示层。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import json
import re
import time

from rich import box
from rich.console import Console, Group
from rich.highlighter import JSONHighlighter
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from genesisai.app.command_input import COMMANDS, TaskInput
from genesisai.shared.messages import content_text
from genesisai.app.select import is_interactive, select
from genesisai.app.terminal_text import terminal_text


@dataclass(frozen=True)
class CliSnapshot:
    """启动时可确定的会话和工作区状态。"""

    session_id: str
    model_name: str
    workspace: str
    input_roots: list[str]
    output: str
    permission_mode: str
    registered_tools: int
    enabled_tools: int | None = None
    skill_name: str | None = None
    git_state: str | None = None
    context_percent: int | None = None
    memory_status: str | None = None
    history_root: str | None = None


STATUS_STYLE = {
    "partial": ("! 部分完成", "yellow"),
    "completed": ("✓ 任务完成", "green"),
    "awaiting_confirmation": ("◆ 等待确认", "yellow"),
    "running": ("● 运行中", "cyan"),
    "failed": ("× 运行失败", "red"),
    "cancelled": ("■ 已取消", "yellow"),
    "interrupted": ("! 执行中断", "red"),
    "limit_reached": ("! 达到限制", "yellow"),
    "idle": ("○ 就绪", "cyan"),
}

TOOL_LABELS = {
    'read_files': '读取文件', 'list_files': '查看目录', 'editor': '修改文件',
    'run_commands': '执行命令', 'search_codebase': '搜索代码',
    'plan': '记录任务计划', 'fetch_web_content': '查询网页',
    'use_skill': '加载 Skill', 'attempt_completion': '提交结果',
}


class CliView:
    """只渲染界面，不参与模型决策、授权或工具执行。"""

    def __init__(self, console: Console):
        self.console = console
        self._running_tools: list[str] = []
        self._streaming = False
        self._stream_chars = 0
        self._streamed_text = ''
        self._working_status = None
        self._reasoning_active = False
        self.show_details = False
        self._last_tool_result = None

    @staticmethod
    def _value(value, pending="未启用") -> str:
        return str(value) if value not in {None, ""} else pending

    def render_help(self) -> None:
        table = Table(box=box.SIMPLE, show_header=True)
        table.add_column('分类')
        table.add_column('命令', style='cyan')
        table.add_column('操作')
        for command, description, group in COMMANDS:
            table.add_row(group, command, description)
        self.console.print(table)

    def toggle_details(self):
        self.show_details = not self.show_details
        self.console.print('工具详情已展开' if self.show_details else '工具详情已收起')
        if self.show_details and self._last_tool_result is not None:
            self.render_data('最近工具输出', self._last_tool_result)

    def choose_command(self):
        if is_interactive(self.console):
            return self._get_input().read('选择操作 · 输入名称或中文说明搜索', default='/').strip()
        picked = select(self.console, '选择操作', [(cmd, group + ' / ' + desc) for cmd, desc, group in COMMANDS], searchable=True)
        return COMMANDS[picked][0] if picked is not None else None

    def bind_session(self, snapshot, data):
        if getattr(self, 'session_id', None) != snapshot.session_id:
            self._input = None
            self._confirmation_seen = None
            self._last_tool_result = None
            self._running_tools.clear()
        self.session_id = snapshot.session_id
        self.snapshot, self.session_data = snapshot, data

    def _get_input(self):
        if getattr(self, '_input', None) is None:
            history = [content_text(m.get('content')) for m in getattr(self, 'session_data', {}).get('messages', [])
                       if m.get('role') == 'user' and content_text(m.get('content'))]
            self._input = TaskInput(history)
        return self._input

    def request_consent(self, purpose, title):
        if is_interactive(self.console):
            index = select(self.console, title, [(purpose, ''), ('取消', '')], index=1)
            return 'allow' if index == 0 else ''
        return self.console.input('allow > ').strip()

    def render_model(self, snapshot: CliSnapshot) -> None:
        body = Text()
        body.append("模型  ", style="dim")
        body.append(snapshot.model_name, style="bold cyan")
        body.append("\n协议  ", style="dim")
        body.append("OpenAI compatible", style="white")
        self.console.print(Panel(body, title="模型", title_align="left", border_style="cyan"))

    def render_model_settings(self, settings, *, path: str | None = None) -> None:
        """展示 CLI 当前生效的提供商、模型、密钥与写入位置。"""
        from genesisai.model.providers.catalog import get_provider
        from genesisai.model.settings import settings_path
        from genesisai.model.credentials import environment_key, resolve_api_key

        provider = get_provider(settings.provider)
        label = provider.label if provider else settings.provider
        body = Text()
        body.append("提供商  ", style="dim")
        body.append(label, style="bold cyan")
        body.append("\n模型    ", style="dim")
        body.append(settings.model, style="bold white")
        body.append("\n接口    ", style="dim")
        body.append(settings.base_url or "默认", style="white")
        body.append("\n密钥    ", style="dim")
        effective = resolve_api_key(settings.provider, settings.api_key)
        body.append(replace(settings, api_key=effective).masked_key(), style="green")
        body.append("\n密钥来源 ", style="dim")
        body.append("环境变量 / .env" if environment_key(settings.provider) else "终端配置", style="white")
        body.append("\n配置    ", style="dim")
        body.append(str(path or settings_path()), style="dim")
        self.console.print(Panel(body, title="模型配置", title_align="left", border_style="green"))

    def render_permissions(self, mode: str) -> None:
        auto = mode == 'auto'
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("当前模式", "自动审批" if auto else "用户审批")
        table.add_row("本地只读", "直接执行")
        table.add_row("写入、公开网络、命令、MCP", "自动执行" if auto else "逐次确认")
        table.add_row("内置文件工具", "可访问工作区外的宿主文件" if auto else "限已授权路径")
        footer = Text("\n/permissions user    /permissions auto", style="dim")
        self.console.print(Panel(Group(table, footer), title="权限", title_align="left", border_style="yellow"))

    def render_tools(self, snapshot: dict[str, list[dict]]) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold", no_wrap=True)
        table.add_column(style="white")
        styles = {"enabled": "bright_green", "disabled": "red"}
        labels = {"enabled": "Enabled", "disabled": "Disabled"}
        total = 0
        for group in ("enabled", "disabled"):
            entries = snapshot.get(group, [])
            total += len(entries)
            if entries:
                value = ", ".join(
                    item["name"] + (f"（{item['reason']}）" if item.get("reason") else "")
                    for item in entries
                )
            else:
                value = "—"
            table.add_row(Text(labels[group], style=styles[group]), value)
        footer = Text("\n所有已启用工具在每次模型请求中全量下发；不可用工具按平台/依赖自动剔除。", style="dim")
        self.console.print(Panel(Group(table, footer), title=f"Tools · {total}", title_align="left", border_style="cyan"))

    def render_changes(self, changes: list[dict], run_id: str | None) -> None:
        current = [item for item in changes if not run_id or item.get("run_id") == run_id]
        if not current:
            self.console.print(Panel("当前运行还没有文件变更。", title="Diff", border_style="cyan"))
            return
        table = Table(show_header=True, header_style="bold cyan", box=box.SIMPLE)
        table.add_column("操作")
        table.add_column("文件")
        table.add_column("变更")
        for item in current:
            before = (item.get("before_sha256") or "-")[:10]
            after = (item.get("after_sha256") or "-")[:10]
            target = item.get("destination") or item.get("path", "")
            table.add_row(str(item.get("operation")), str(target), f"{before} → {after}")
        self.console.print(Panel(table, title=f"Diff · {len(current)} 项", border_style="cyan"))

    def render_status(self, data: dict, snapshot: CliSnapshot) -> None:
        label, style = STATUS_STYLE.get(data.get("status"), (str(data.get("status", "unknown")), "white"))
        usage = data.get("usage") or {}
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(style="white")
        table.add_row("状态", Text(label, style=f"bold {style}"))
        table.add_row("会话", data.get("id") or snapshot.session_id)
        table.add_row("运行", data.get("run_id") or "—")
        table.add_row("模型", snapshot.model_name)
        table.add_row('工作区', snapshot.workspace)
        table.add_row('授权输入', '、'.join(snapshot.input_roots))
        table.add_row('输出目录', snapshot.output)
        table.add_row('历史目录', snapshot.history_root or '—')
        table.add_row("权限", "自动审批" if snapshot.permission_mode == "auto" else "用户审批")
        table.add_row("轮次", str(data.get("rounds", 0)))
        table.add_row("工具", str(data.get("tool_count", 0)))
        table.add_row("Token", str(usage.get("total_tokens", "未知")))
        runtime = data.get("run_runtime") or {}

        now = runtime.get('confirmation_paused_at') or time.time()
        table.add_row('剩余执行时间', f"{max(0, data.get('deadline', 0) - now):.0f} 秒")
        table.add_row("Profile", str(runtime.get("profile", "—")))
        table.add_row("阶段", str(runtime.get("phase", "—")))
        table.add_row('任务时限', f"{runtime.get('execution_seconds_limit', '—')} 秒")
        table.add_row('单次请求超时', f"{runtime.get('request_timeout_seconds', '—')} 秒")
        table.add_row("停止原因", str(runtime.get("stop_reason") or "—"))
        table.add_row("Skill", self._value(snapshot.skill_name))
        table.add_row("Git", self._value(snapshot.git_state))
        self.console.print(Panel(table, title="状态", title_align="left", border_style=style, box=box.ROUNDED))

    def render_budget(self, data: dict) -> None:
        runtime = data.get("run_runtime") or {}
        budget = runtime.get("budget") or {}
        usage = runtime.get("usage") or {}
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", no_wrap=True)
        table.add_column(style="white")
        table.add_row("Profile", str(runtime.get("profile", "—")))
        table.add_row("阶段", str(runtime.get("phase", "—")))
        for key in ("model_calls", "search_queries", "search_fetches"):
            label = {"model_calls": "模型调用", "search_queries": "网络检索", "search_fetches": "网页抓取"}[key]
            used = usage.get(key, 0)
            table.add_row(label, f"{used} / {budget.get(key, '—')}（余 {max(0, budget.get(key, 0) - used) if isinstance(budget.get(key), int) else '—'}）")
        table.add_row("累计 Token（用量）", str(usage.get('total_tokens', 0)))
        context = runtime.get('context_report') or {}
        table.add_row('上下文窗口', str(context.get('context_window', 400000)))
        table.add_row("当前请求（估算 Token）", f"{context.get('tokens_after', '—')} / {context.get('input_limit', '—')}")
        table.add_row('图片（数量 / 估算 Token / 字节）', f"{context.get('image_count', 0)} / {context.get('image_tokens', 0)} / {context.get('image_bytes', 0)}")
        table.add_row("候选 / Evidence", f"{runtime.get('candidate_count', 0)} / {len(runtime.get('evidence_refs', []))}")
        table.add_row("停止原因", str(runtime.get("stop_reason") or "—"))
        self.console.print(Panel(table, title="预算", title_align="left", border_style="cyan", box=box.ROUNDED))

    def render_trace(self, store) -> None:
        path = store.root / "traces" / f"{store.id}.jsonl"
        if not path.is_file():
            self.console.print(Panel("当前会话还没有 Trace。", title="Trace", border_style="cyan"))
            return
        lines = path.read_text(encoding="utf-8").splitlines()[-20:]
        entries = []
        for line in lines:
            try:
                value = json.loads(line)
                entries.append({key: value[key] for key in ("event", "run_id", "model_call_id", "tool_call_id", "tool", "profile", "phase", "status", "code", "finish_reason", "stop_reason", "elapsed_ms", "diagnostic") if key in value})
            except ValueError:
                continue
        self.render_data("Trace · 脱敏", entries)

    def render_home(self, workspace, recent):
        self.console.print(Text('GenesisAI', style='bold'))
        self.console.print(Text(f'工作区  {workspace}'))
        if recent:
            self.console.print('最近对话')
            for _, title, detail in recent:
                self.console.print(Text(f'  {title}  {detail.split(" · ")[0]}'))
            self.console.print('↑ 选择最近对话 · /sessions 浏览全部')
        self.console.print('直接输入开始新对话，输入 / 查看操作。')
        self.console.print(Text('/auth 切换模型提供商 · /model 切换模型', style='cyan'))

    def render_history(self, data, *, recent_turns=None):
        # Some answers (errors, cancellation, and attempt_completion) only exist in the run outcome.
        turns = []
        for message in data.get('messages', []):
            content = content_text(message.get('content'), images=True)
            if message['role'] == 'user':
                turns.append([content or '', None])
            elif turns and message['role'] == 'assistant' and not message.get('tool_calls') and content:
                turns[-1][1] = content.removeprefix('[[PARTIAL]]').lstrip()
        outcomes = [*data.get('history', []), data]
        if len(outcomes) == len(turns):
            for turn, outcome in zip(turns, outcomes):
                if outcome.get('answer'):
                    turn[1] = outcome['answer']
        if recent_turns and len(turns) > recent_turns:
            self.console.print(f'还有 {len(turns) - recent_turns} 轮更早对话，输入 /history 查看。')
            turns = turns[-recent_turns:]
        for question, answer in turns:
            self.console.print(Text('你', style='bold cyan'))
            self.console.print(Text(terminal_text(question)))
            if answer:
                self.console.print(Text('GenesisAI', style='bold cyan'))
                self.console.print(Markdown(terminal_text(answer)))

    def render_opened(self, store):
        self.console.print(Text(f'已打开历史会话 · {store.id} · {store.workspace}', style='bold'))
        self.render_history(store.snapshot(), recent_turns=3)
        self.console.print(Text('/auth 切换模型提供商 · /model 切换模型', style='cyan'))
        if store.record.status == 'awaiting_confirmation' and store.record.pending:
            call = store.record.pending[0]
            self.render_confirmation(call['name'], store.record.calls[call['id']].get('preview') or {})
        elif store.record.status == 'interrupted':
            self.console.print('上次运行已中断。输入 /resume 继续，或 /cancel 取消。')

    def render_data(self, title: str, value) -> None:
        serialized = terminal_text(json.dumps(value, ensure_ascii=False, indent=2))
        self.console.print(Panel(JSONHighlighter()(Text(serialized)), title=title, title_align="left", border_style="cyan"))

    def render_logs(self, path: str) -> None:
        self.console.print(Text('完整输入输出日志（JSONL）', style='bold cyan'))
        self.console.print(Text(path, overflow='fold'))
        self.console.print(Text('每行一个事件；正文不截断，认证信息脱敏。', style='dim'))

    def render_progress(self, kind: str, value: dict) -> None:
        if kind == 'command_output':
            self.console.print(Text(terminal_text(value['text'])), end='')
            return
        if kind == "reasoning_token":
            if not self._reasoning_active:
                self._reasoning_active = True
                self.console.print(Text("💭 正在规划…", style="dim"))
            return
        if kind == "stream_token":
            # Live 动画会清除当前行，必须先停止再输出未换行的正文片段。
            if self._working_status is not None:
                self._working_status.stop()
                self._working_status = None
            # 如果需要，退出推理模式。
            if self._reasoning_active:
                self._reasoning_active = False
                self.console.print()  # newline after reasoning
                self.console.print("[dim]─── 思考结束 ───[/dim]")
            self._streaming = True
            text = value.get("text", "")
            self._stream_chars += len(text)
            self._streamed_text += text
            self.console.print(Text(terminal_text(text)), end="", highlight=False)
            return
        if kind == "model":
            if self._streaming:
                self.console.print()
                if value.get("retry"):
                    self.console.print("响应中断，正在重试…", style="dim")
            self._streaming = False
            self._streamed_text = ''
            # 重置新一轮的推理状态。
            self._reasoning_active = False
            if self._working_status is not None:
                self._working_status.update('正在分析任务 · Ctrl+C 停止')
            return
        if kind == "tool_start":
            # 如果正在流式输出，先干净地结束再显示工具。
            if self._streaming:
                self.console.print()  # ensure newline
                self._streaming = False
            name = value["name"]
            self._running_tools.append(name)
            label = TOOL_LABELS.get(name, name)
            if self._working_status is not None:
                self._working_status.update(f'{label} · Ctrl+C 停止')
            else:
                self.console.print(Text('● ' + terminal_text(label) + ' · 运行中'))
            return
        if kind == "tool_result":
            self._last_tool_result = value
            name = self._running_tools.pop(0) if self._running_tools else "tool"
            label = TOOL_LABELS.get(name, name)
            data = value.get('data') or {}
            target = data.get('path') or data.get('cwd') or ''
            if not target and data.get('files'):
                target = '、'.join(str(item.get('path', '')) for item in data['files'][:3])
            if value.get("ok"):
                detail = f' · {target}' if target else ''
                if name == 'run_commands' and 'exit_code' in data:
                    detail += f" · 退出码 {data['exit_code']}"
                self.console.print(Text.assemble(('✓ ', 'green'), (terminal_text(label + '完成' + detail), '')))
            else:
                message = (value.get("error") or {}).get("message", "未知错误")
                code = (value.get("error") or {}).get("code")
                detail = f"[{code}] {message}" if code else message
                self.console.print(Text.assemble(("× ", "red"), (terminal_text(label) + '失败 ', ''), (terminal_text(detail), '')))
                self.console.print('使用 /logs 查看完整输入输出。', style='dim')
            if self.show_details:
                self.render_data('工具输出', value)
            return
        if kind == "verification_start":
            self.console.print(f"[yellow]⟳[/yellow] [dim]自动验证 · 第 {value.get('round', 1)} 轮[/dim]")
            return

    def render_result(self, result: dict, data: dict) -> None:
        # 如果流式传输已经显示了答案，就渲染一个紧凑的元数据栏。
        streamed_same_answer = self._streaming and self._streamed_text == (result.get('answer') or '')
        if self._streaming:
            self._streaming = False
            self._stream_chars = 0
            self._streamed_text = ''
            self.console.print()  # newline after streamed content
        if streamed_same_answer:
            status = result.get("status", "failed")
            label, style = STATUS_STYLE.get(status, (status, "white"))
            footer = Text()
            footer.append(f"{label}", style=f"bold {style}")
            footer.append(f"  ·  轮次 {data.get('rounds', 0)}", style="dim")
            footer.append(f"  ·  工具 {data.get('tool_count', 0)}", style="dim")
            total_tokens = (data.get("usage") or {}).get("total_tokens")
            if total_tokens is not None:
                footer.append(f"  ·  Token {total_tokens}", style="dim")
            self.console.print(footer)
            return
        status = result.get("status", "failed")
        label, style = STATUS_STYLE.get(status, (status, "white"))
        answer = result.get("answer") or "(无内容)"
        evidence = {item['source_ref']: item for item in (data.get('run_runtime') or {}).get('evidence', {}).values()}
        def readable_source(match):
            ref = match.group(1)
            item = evidence.get(ref)
            if not item:
                return match.group(0)
            title = str(item.get('title') or '来源').replace('[', '').replace(']', '').replace('\n', ' ')
            url = str(item.get('canonical_url') or '').replace('(', '%28').replace(')', '%29')
            return f'[{title}]({url})（{url}）'
        answer = re.sub(r'\[?(src_[0-9a-f]{16})\]?', readable_source, str(answer))
        if self.console.width < 100:
            answer = self._narrow_tables(answer)
        footer = Text()
        footer.append(f"轮次 {data.get('rounds', 0)}", style="dim")
        footer.append(f"   工具 {data.get('tool_count', 0)}", style="dim")
        total_tokens = (data.get("usage") or {}).get("total_tokens")
        if total_tokens is not None:
            footer.append(f"   Token {total_tokens}", style="dim")
        self.console.print(Text(label, style=style))
        self.console.print(Markdown(terminal_text(answer)))
        self.console.print(footer)

    def render_confirmation(self, tool_name: str, preview: dict) -> None:
        body = Text()
        body.append("工具    ", style="dim")
        body.append(terminal_text(tool_name), style="bold yellow")
        body.append("\n目标    ", style="dim")
        body.append(terminal_text(preview.get("target", "未知")), style="white")
        args = preview.get("args") or {}
        operation = preview.get("operation")
        if operation:
            body.append("\n操作    " + terminal_text(operation))
        if tool_name == "editor" and operation == "create":
            content = str(args.get("content", ""))
            body.append(f"\n内容    {len(content)} 字符", style="dim")
            if content:
                body.append('\n\n' + terminal_text(content[:800]))
                if len(content) > 800:
                    body.append('\n…预览已截断', style='dim')
        elif operation == 'patch':
            body.append('\n\n' + terminal_text(str(args.get('patch', ''))[:1200]))
        elif args.get('source'):
            body.append('\n来源  ' + terminal_text(args['source']))
        elif args and not preview.get('command') and tool_name != 'fetch_web_content':
            details = json.dumps(args, ensure_ascii=False)
            body.append('\n参数    ' + terminal_text(details[:1200]))
            if len(details) > 1200:
                body.append('\n…预览已截断', style='dim')
        if tool_name.startswith('mcp_'):
            body.append('\n发送参数    ' + terminal_text(json.dumps(args, ensure_ascii=False)))
        if not is_interactive(self.console):
            body.append("\n\n/approve  /reject  /cancel  /exit", style="cyan")
        body.append("\n仅本次操作 · 参数已冻结", style="dim")
        if preview.get('command'):
            body.append('\n执行命令  ' + terminal_text(json.dumps(preview['command'], ensure_ascii=False)))
        self.console.print(
            Panel(body, title="◆ 操作确认", title_align="left", border_style="yellow", box=box.ROUNDED)
        )

    @staticmethod
    def _narrow_tables(answer):
        """在窄终端上将普通 Markdown 表格渲染为字段列表。"""
        lines = answer.splitlines()
        output, index = [], 0
        while index < len(lines):
            if index + 1 < len(lines) and lines[index].strip().startswith('|') and re.fullmatch(r'[\s|:\-]+', lines[index + 1]) and '-' in lines[index + 1]:
                headers = [cell.strip() for cell in lines[index].strip().strip('|').split('|')]
                index += 2
                while index < len(lines) and lines[index].strip().startswith('|'):
                    cells = [cell.strip() for cell in lines[index].strip().strip('|').split('|')]
                    output.extend(['', '- ' + '；'.join(f'{headers[i] if i < len(headers) else "信息"}：{cell}' for i, cell in enumerate(cells))])
                    index += 1
                output.append('')
            else:
                output.append(lines[index])
                index += 1
        return '\n'.join(output)

    def render_error(self, message: str, *, title="错误") -> None:
        self.console.print(Text.assemble(('× ' + title + '\n', 'red bold'), (message, '')))

    def render_goodbye(self) -> None:
        self.console.print('再见。', style='bright_green')

    def prompt_running(self, runs, render):
        raw, self.draft = self._get_input().read_running(runs, render, self.draft)
        return raw

    def prepare_run(self, snapshot, data):
        self.bind_session(snapshot, data)
        # Materialize the editor history while the session is still idle.
        self._get_input()

    def prompt(self) -> str:
        data = getattr(self, 'session_data', {})
        if is_interactive(self.console):
            pending = data.get('pending') or []
            if data.get('status') == 'awaiting_confirmation' and pending:
                call_id = pending[0]['id']
                if getattr(self, '_confirmation_seen', None) != call_id:
                    self._confirmation_seen = call_id
                    options = [('允许本次操作', ''), ('拒绝本次操作', ''), ('取消任务', ''), ('保存并退出', '')]
                    picked = select(self.console, '选择如何处理', options, index=1)
                    if picked is not None:
                        self.last_input_raw = ['/approve', '/reject', '/cancel', '/exit'][picked]
                        return self.last_input_raw
            snapshot = getattr(self, 'snapshot', None)
            status = snapshot.model_name if snapshot else 'GenesisAI'
            context = (data.get('run_runtime') or {}).get('context_report') or {}
            if context.get('input_limit'):
                status += f" · 上下文约 {context.get('tokens_after', 0) / context['input_limit']:.0%}"
            self.console.rule(style='dim')
            self.last_input_raw = self._get_input().read(status, default=getattr(self, 'draft', ''),
                                                          recent=bool(getattr(self, 'recent', [])))
            self.draft = ''
        else:
            if getattr(self, 'draft', ''):
                self.console.print(Text('草稿（回车重试）：' + self.draft))
            self.last_input_raw = self.console.input('\n> ') or getattr(self, 'draft', '')
            self.draft = ''
        return self.last_input_raw.strip()

    @contextmanager
    def working(self, message="GenesisAI 正在处理..."):
        status = self.console.status(Text(message + ' · Ctrl+C 停止'), spinner="dots")
        self._working_status = status
        status.start()
        try:
            yield
        finally:
            if self._working_status is not None:
                status.stop()
            self._working_status = None
