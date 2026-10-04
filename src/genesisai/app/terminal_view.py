"""GenesisAI 命令行界面的 Rich 展示层。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import re
import time

from rich import box
from rich.console import Console, Group
from rich.json import JSON
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class CliSnapshot:
    """启动时可确定的界面状态；可选字段为后续模块预留。"""

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
    agent_docs_status: str | None = None


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


class CliView:
    """只渲染界面，不参与模型决策、授权或工具执行。"""

    def __init__(self, console: Console):
        self.console = console
        self._running_tools: list[str] = []
        self._streaming = False
        self._stream_chars = 0
        self._reasoning_active = False

    @staticmethod
    def _value(value, pending="待接入") -> str:
        return str(value) if value not in {None, ""} else pending

    @staticmethod
    def _append_badge(line: Text, label: str, value: str, style: str) -> None:
        if line:
            line.append("   ")
        line.append(f" {label} {value} ", style=f"bold {style} on #142235")

    def render_startup(self, snapshot: CliSnapshot) -> None:
        """显示紧凑的会话、能力和路径概览。"""
        title = Text()
        title.append("◆ ", style="bold bright_green")
        title.append("GENESISAI", style="bold white")
        title.append("\n  single-agent developer CLI", style="dim cyan")

        badges = Text()
        self._append_badge(badges, "MODEL", snapshot.model_name, "cyan")
        self._append_badge(badges, "MODE", snapshot.permission_mode, "yellow")
        self._append_badge(badges, "SESSION", snapshot.session_id[-8:], "green")

        capabilities = Text()
        capabilities.append("Skill: ", style="dim")
        capabilities.append(self._value(snapshot.skill_name), style="cyan")
        capabilities.append("   Git: ", style="dim")
        capabilities.append(self._value(snapshot.git_state), style="cyan")
        capabilities.append("   Tools: ", style="dim")
        enabled = snapshot.enabled_tools if snapshot.enabled_tools is not None else snapshot.registered_tools
        capabilities.append(f"{enabled}/{snapshot.registered_tools}", style="cyan")
        capabilities.append("   Agent Docs: ", style="dim")
        capabilities.append(self._value(snapshot.agent_docs_status, "未初始化"), style="cyan")
        if snapshot.context_percent is not None:
            capabilities.append(f"   Context: {snapshot.context_percent}%", style="dim cyan")

        header = Table.grid(expand=True)
        header.add_column(ratio=1)
        header.add_column(justify="right")
        header.add_row(title, badges)
        header.add_row(Text(""), capabilities)

        paths = Text()
        paths.append("工作区  ", style="dim")
        paths.append(snapshot.workspace, style="bright_black")
        paths.append("\n输入    ", style="dim")
        paths.append("、".join(snapshot.input_roots) if snapshot.input_roots else "未指定", style="bright_black")
        paths.append("\n输出    ", style="dim")
        paths.append(snapshot.output, style="bright_black")

        self.console.print(
            Panel(
                Group(header, Text(""), paths),
                border_style="bright_green",
                box=box.ROUNDED,
                padding=(1, 2),
            )
        )
        self.render_command_hint()

    def render_workspace_selection(self, current: str, suggested: str | None = None) -> None:
        body = Text()
        body.append("当前目录  ", style="dim")
        body.append(current, style="white")
        if suggested and suggested != current:
            body.append("\nGit 根目录 ", style="dim")
            body.append(suggested, style="cyan")
        body.append("\n\n输入已有工作区路径；直接回车使用当前目录。", style="yellow")
        self.console.print(Panel(body, title="指定工作区", title_align="left", border_style="cyan"))

    def render_agent_docs_consent(self, path: str) -> None:
        body = Text()
        body.append("GenesisAI 将创建受管理的项目工作记忆：\n", style="white")
        body.append(path, style="cyan")
        body.append("\n\n内容包括项目概览、计划、进度、决策、已验证命令和验收记录。", style="dim")
        body.append("\n不会写入密钥、完整源码或原始命令日志。", style="dim")
        body.append("\n\n输入 allow 初始化；其他输入将跳过，之后可用 /project init。", style="yellow")
        self.console.print(Panel(body, title="初始化 agent_docs", title_align="left", border_style="yellow"))

    def render_command_hint(self) -> None:
        self.console.print(
            "[dim]输入任务，或使用[/dim] [cyan]/status[/cyan] [cyan]/tools[/cyan] "
            "[cyan]/new[/cyan] [cyan]/help[/cyan] [dim]查看当前能力。[/dim]"
        )

    def render_help(self) -> None:
        rows = [
            ("/new", "保存当前会话并创建全新会话"),
            ("/status", "查看会话、轮次、工具调用和 Token"),
            ("/budget", "查看当前 Profile、预算、用量和停止原因"),
            ("/trace", "查看脱敏执行链摘要"),
            ("/compact", "按 cline 策略折叠早期对话，保留 typed 提示与结论性回答"),
            ("/undo", "预览本任务撤销；/undo apply 安全执行"),
            ("/doctor", "检查模型配置、目录、密钥状态和可选依赖"),
            ("/clean", "预览旧日志清理；/clean run 执行"),
            ("/history", "查看本地会话历史"),
            ("/model", "查看模型；交互模式下可选择切换"),
            ("/auth", "重新选择提供商、密钥与模型并写入用户配置"),
            ("/permissions", "查看权限；可用 network|writes|shell ask|allow 切换"),
            ("/tools", "查看启用与禁用的工具"),
            ("/workflow", "列出或展开 .genesis/workflows 中的工作流"),
            ("/resume", "恢复安全检查点"),
            ("/approve", "允许一次；待确认时也可输入 approve、yes 或 y"),
            ("/allow network", "批准当前网络调用并允许本会话后续公开网络请求"),
            ("/allow network run", "批准当前网络调用并允许本轮后续请求；下一轮恢复确认"),
            ("/allow writes", "批准当前写入并允许本会话后续工作区写入"),
            ("/allow shell", "批准当前命令并允许本会话后续受控命令"),
            ("/reject", "拒绝一次；待确认时也可输入 reject、no 或 n"),
            ("/cancel", "取消当前运行"),
            ("/exit", "保存状态并退出"),
            ("/skills", "Skill 列表、检索、查看、加载与卸载"),
            ("/project", "查看 agent_docs 活动任务；/project init 初始化"),
            ("/diff", "查看本任务记录的文件变更"),
        ]
        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="white")
        for command, description in rows:
            table.add_row(command, description)
        footer = Text(
            "\n普通输入继续当前会话；/new 创建全新会话。"
            "写入和公开网络请求默认需要确认；/exit 会保留待确认状态。"
            "恢复已有会话使用 --session ID。",
            style="dim",
        )
        self.console.print(
            Panel(Group(table, footer), title="命令", title_align="left", border_style="cyan", box=box.ROUNDED)
        )

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
        body.append(settings.masked_key(), style="green")
        body.append("\n配置    ", style="dim")
        body.append(str(path or settings_path()), style="dim")
        self.console.print(Panel(body, title="模型配置", title_align="left", border_style="green"))

    def render_permissions(self, *, confirm_writes: bool, confirm_search: bool, confirm_shell: bool = True) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("文件写入", "逐次确认" if confirm_writes else "本次会话已允许")
        table.add_row("公开网络", "逐次确认" if confirm_search else "本次会话已允许")
        table.add_row("命令执行", "逐次确认" if confirm_shell else "本次会话已允许")
        table.add_row("工作区外", "禁止")
        footer = Text("\n切换：/permissions network|writes|shell ask|allow", style="dim")
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

    def render_pending_feature(self, command: str, description: str) -> None:
        body = Text()
        body.append(f"{command} ", style="bold cyan")
        body.append("接口已预留，当前版本尚未接入。\n", style="yellow")
        body.append(description, style="dim")
        self.console.print(Panel(body, title="功能预留", title_align="left", border_style="bright_black"))

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
        table.add_row("权限", snapshot.permission_mode)
        table.add_row("轮次", str(data.get("rounds", 0)))
        table.add_row("工具", str(data.get("tool_count", 0)))
        table.add_row("Token", str(usage.get("total_tokens", "未知")))
        runtime = data.get("run_runtime") or {}
        modes = data.get('permission_modes', {})
        scope = '本轮' if runtime.get('network_grant_scope') == 'run' else ('本会话' if modes.get('network') == 'allow' else '逐次确认')
        table.add_row('网络授权', scope)
        now = runtime.get('confirmation_paused_at') or time.time()
        table.add_row('剩余执行时间', f"{max(0, data.get('deadline', 0) - now):.0f} 秒")
        table.add_row("Profile", str(runtime.get("profile", "—")))
        table.add_row("阶段", str(runtime.get("phase", "—")))
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
        for key in ("model_calls", "search_queries", "search_fetches", "token_budget"):
            label = {"model_calls": "模型调用", "search_queries": "网络检索", "search_fetches": "网页抓取", "token_budget": "Token"}[key]
            used = usage.get(key if key != "token_budget" else "total_tokens", 0)
            table.add_row(label, f"{used} / {budget.get(key, '—')}（余 {max(0, budget.get(key, 0) - used) if isinstance(budget.get(key), int) else '—'}）")
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
                value = __import__("json").loads(line)
                entries.append({key: value[key] for key in ("event", "run_id", "model_call_id", "tool_call_id", "tool", "profile", "phase", "status", "code", "finish_reason", "stop_reason", "elapsed_ms", "diagnostic") if key in value})
            except ValueError:
                continue
        self.console.print(Panel(JSON.from_data(entries, ensure_ascii=False, indent=2), title="Trace · 脱敏", border_style="cyan"))

    def render_history(self, messages: list[dict]) -> None:
        visible = [{key: value for key, value in message.items() if key != "reasoning"} for message in messages]
        self.console.print(
            Panel(
                JSON.from_data(visible, ensure_ascii=False, indent=2),
                title=f"历史 · {len(visible)} 条",
                title_align="left",
                border_style="cyan",
            )
        )

    def render_data(self, title: str, value) -> None:
        self.console.print(Panel(JSON.from_data(value, ensure_ascii=False, indent=2), title=title, title_align="left", border_style="cyan"))

    def render_progress(self, kind: str, value: dict) -> None:
        if kind == "reasoning_token":
            if not self._reasoning_active:
                self._reasoning_active = True
                self.console.print(Text("💭 正在规划…", style="dim"))
            return
        if kind == "stream_token":
            # 如果需要，退出推理模式。
            if self._reasoning_active:
                self._reasoning_active = False
                self.console.print()  # newline after reasoning
                self.console.print("[dim]─── 思考结束 ───[/dim]")
            self._streaming = True
            text = value.get("text", "")
            self._stream_chars += len(text)
            self.console.print(Text(text), end="", highlight=False)
            return
        if kind == "model":
            # 重置新一轮的推理状态。
            self._reasoning_active = False
            self.console.print(f"[cyan]◇[/cyan] [dim]模型推理 · 第 {value['round']} 轮[/dim]")
            return
        if kind == "tool_start":
            # 如果正在流式输出，先干净地结束再显示工具。
            if self._streaming:
                self.console.print()  # ensure newline
                self._streaming = False
            name = value["name"]
            self._running_tools.append(name)
            self.console.print(f"[cyan]●[/cyan] {name} [dim]运行中[/dim]")
            return
        if kind == "tool_result":
            name = self._running_tools.pop(0) if self._running_tools else "tool"
            if value.get("ok"):
                suffix = " · 输出已截断" if value.get("truncated") else ""
                self.console.print(f"[green]✓[/green] {name} [dim]完成{suffix}[/dim]")
            else:
                message = (value.get("error") or {}).get("message", "未知错误")
                code = (value.get("error") or {}).get("code")
                detail = f"[{code}] {message}" if code else message
                self.console.print(Text.assemble(("× ", "red"), (name + " ", "white"), (detail, "dim red")))
            return
        if kind == "verification_start":
            self.console.print(f"[yellow]⟳[/yellow] [dim]自动验证 · 第 {value.get('round', 1)} 轮[/dim]")
            return

    def render_result(self, result: dict, data: dict) -> None:
        # 如果流式传输已经显示了答案，就渲染一个紧凑的元数据栏。
        if self._streaming:
            self._streaming = False
            self._stream_chars = 0
            self.console.print()  # newline after streamed content
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
        self.console.print(
            Panel(
                Group(Markdown(str(answer)), Text(""), footer),
                title=label,
                title_align="left",
                border_style=style,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def render_confirmation(self, tool_name: str, preview: dict) -> None:
        body = Text()
        body.append("工具    ", style="dim")
        body.append(tool_name, style="bold yellow")
        body.append("\n目标    ", style="dim")
        body.append(str(preview.get("target", "未知")), style="white")
        args = preview.get("args") or {}
        operation = preview.get("operation")
        if tool_name == "editor" and operation == "create":
            content = str(args.get("content", ""))
            body.append(f"\n内容    {len(content)} 字符", style="dim")
            if content:
                body.append("\n\n")
                body.append(content[:800], style="bright_black")
                if len(content) > 800:
                    body.append("\n…预览已截断", style="dim")
        elif tool_name == "editor" and operation == "copy":
            body.append("\n来源    ", style="dim")
            body.append(str(args.get("source", "未知")), style="white")
        body.append("\n\n")
        body.append(" /approve ", style="bold white on #266a52")
        body.append(" approve/yes/y ", style="dim")
        body.append("  ")
        body.append(" /reject ", style="bold white on #67333d")
        body.append(" reject/no/n ", style="dim")
        body.append("  ")
        body.append(" /exit 暂缓 ", style="white on #26354a")
        body.append("    仅本次 · 参数已冻结", style="dim")
        body.append('\n中文：确认/同意；拒绝/不同意（完整匹配）', style='dim')
        if tool_name == 'fetch_web_content':
            body.append('\n/allow network run  允许本轮后续网络请求', style='cyan')
            body.append('\n/allow network      允许本会话后续网络请求', style='cyan')
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

    def render_remote_consent(self, input_roots: list[str]) -> None:
        body = Text()
        body.append("远程模型将接收当前会话内容。", style="white")
        body.append("\n读取本地资料后，授权内容也可能发送给模型；搜索词可能包含会话或资料信息。", style="dim")
        body.append("\n授权输入：", style="dim")
        body.append("、".join(input_roots) if input_roots else "未指定", style="white")
        body.append("\n\n输入 allow 允许本次会话。", style="yellow")
        self.console.print(Panel(body, title="远程数据确认", title_align="left", border_style="yellow"))

    def render_error(self, message: str, *, title="错误") -> None:
        self.console.print(Panel(Text(message), title=title, title_align="left", border_style="red"))

    def render_goodbye(self) -> None:
        self.console.print("\n[dim]会话状态已保存。[/dim] [bright_green]再见。[/bright_green]")

    def prompt(self) -> str:
        return self.console.input("\n[bold bright_green]❯[/bold bright_green] ").strip()

    @contextmanager
    def working(self, message="GenesisAI 正在处理..."):
        with self.console.status(f"[dim cyan]{message}[/dim cyan]", spinner="dots"):
            yield
