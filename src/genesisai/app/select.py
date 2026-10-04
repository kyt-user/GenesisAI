"""终端交互原语：方向键选择与密钥输入。

设计目标：不引入第三方依赖即可提供接近 cline 的选择体验；当 stdin/stdout
不是 TTY（管道、测试、CI）时自动回退到逐行序号输入，保证可脚本化。
Windows 使用 ``msvcrt``，POSIX 使用 ``termios``/``tty``。
"""

from __future__ import annotations

import os
import sys

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover - 平台分支
    import msvcrt
else:  # pragma: no cover - 平台分支
    import termios
    import tty

DEFAULT_FOOTER = "↑/↓ 选择 · Enter 确认 · Esc 取消"


def is_interactive(console: Console | None = None) -> bool:
    """两端均为 TTY 时才启用可视化交互。"""
    stream = console.file if console is not None else sys.stdout
    try:
        return bool(sys.stdin.isatty() and stream.isatty())
    except (ValueError, AttributeError):
        return False


def _read_key() -> str:
    """读取一次按键，返回语义化标记或可打印字符。"""
    if _WINDOWS:
        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            extra = msvcrt.getwch()
            return {
                "H": "up", "P": "down", "K": "left", "M": "right",
                "G": "home", "O": "end",
            }.get(extra, "")
        if char in ("\r", "\n"):
            return "enter"
        if char == "\x1b":
            return "esc"
        if char == "\x03":
            return "interrupt"
        if char in ("\x08", "\x7f"):
            return "backspace"
        if char == "\t":
            return "tab"
        return char
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = os.read(fd, 1)
        if char == b"\x1b":
            import select as _select

            sequence = b""
            while True:
                ready, _, _ = _select.select([fd], [], [], 0.02)
                if not ready:
                    break
                sequence += os.read(fd, 1)
            return {
                b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
                b"[H": "home", b"[F": "end", b"[1~": "home", b"[4~": "end",
            }.get(sequence, "esc")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if char in (b"\r", b"\n"):
        return "enter"
    if char == b"\x03":
        return "interrupt"
    if char in (b"\x08", b"\x7f"):
        return "backspace"
    if char == b"\t":
        return "tab"
    try:
        return char.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _normalize(options) -> list[tuple[str, str]]:
    rows = []
    for item in options:
        if isinstance(item, (tuple, list)):
            label = str(item[0])
            detail = str(item[1]) if len(item) > 1 else ""
        else:
            label, detail = str(item), ""
        rows.append((label, detail))
    return rows


def _menu_panel(title, rows, selected, footer, query=None) -> Panel:
    body = Text()
    if query is not None:
        body.append("搜索  ", style="dim")
        body.append(query, style="cyan")
        body.append("\n")
    for position, (label, detail, _origin) in enumerate(rows):
        active = position == selected
        body.append("❯ " if active else "  ", style="bold cyan" if active else "grey35")
        body.append(label, style="bold cyan" if active else "white")
        if detail:
            body.append("  " + detail, style="dim")
        body.append("\n")
    if footer:
        body.append(footer, style="dim")
    return Panel(body, title=title, title_align="left", border_style="cyan", box=box.ROUNDED)


def _arrow_select(console, title, rows_all, index, footer):
    rows = [(label, detail, pos) for pos, (label, detail) in enumerate(rows_all)]
    index = max(0, min(index, len(rows) - 1))

    def render():
        return _menu_panel(title, rows, index, footer)

    with Live(render(), console=console, transient=True) as live:
        while True:
            key = _read_key()
            if key == "up":
                index = (index - 1) % len(rows)
            elif key == "down":
                index = (index + 1) % len(rows)
            elif key == "home":
                index = 0
            elif key == "end":
                index = len(rows) - 1
            elif key == "enter":
                return rows[index][2]
            elif key in ("esc", "interrupt", "q"):
                return None
            live.update(render())


def _searchable_select(console, title, rows_all, index, footer):
    query: list[str] = []
    filtered = list(range(len(rows_all)))
    cursor = 0

    def refine():
        text = "".join(query).lower()
        if not text:
            return list(range(len(rows_all)))
        return [
            pos for pos, (label, detail) in enumerate(rows_all)
            if text in (label + " " + detail).lower()
        ]

    def render():
        rows = [(rows_all[pos][0], rows_all[pos][1], pos) for pos in filtered]
        selected = min(cursor, len(rows) - 1) if rows else 0
        return _menu_panel(title, rows, selected, footer, "".join(query))

    with Live(render(), console=console, transient=True) as live:
        while True:
            key = _read_key()
            if key == "up" and filtered:
                cursor = (cursor - 1) % len(filtered)
            elif key == "down" and filtered:
                cursor = (cursor + 1) % len(filtered)
            elif key == "enter":
                return filtered[min(cursor, len(filtered) - 1)] if filtered else None
            elif key in ("esc", "interrupt"):
                return None
            elif key == "backspace":
                if query:
                    query.pop()
                filtered, cursor = refine(), 0
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                query.append(key)
                filtered, cursor = refine(), 0
            live.update(render())


def _fallback_select(console, title, rows_all):
    body = Text()
    body.append(title + "\n\n", style="bold cyan")
    for position, (label, detail) in enumerate(rows_all):
        body.append(f"  {position + 1}. ", style="dim")
        body.append(label, style="white")
        if detail:
            body.append(f"  — {detail}", style="dim")
        body.append("\n")
    console.print(Panel(body, border_style="cyan", box=box.ROUNDED))
    try:
        raw = console.input("[bold cyan]序号（回车取消）> [/bold cyan]").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw.isdigit():
        return None
    number = int(raw)
    return number - 1 if 1 <= number <= len(rows_all) else None


def select(console, title, options, *, index=0, searchable=False, footer=DEFAULT_FOOTER):
    """单选：返回被选中的原始下标；取消返回 None。"""
    rows_all = _normalize(options)
    if not rows_all:
        return None
    if not is_interactive(console):
        return _fallback_select(console, title, rows_all)
    if searchable:
        return _searchable_select(console, title, rows_all, index, footer)
    return _arrow_select(console, title, rows_all, index, footer)


def prompt_secret(console, title, *, hint="", required=True):
    """输入 API Key。交互终端下掩码；无 TTY 时明文读取以支持管道/自动化。取消返回 None。"""
    if hint:
        console.print(f"[dim]{hint}[/dim]")
    # Windows 的 getpass 直接读取控制台、忽略管道输入；仅在真正交互时启用掩码，
    # 否则回退为普通读取，避免重定向输入时挂起。
    masked = is_interactive(console)
    while True:
        try:
            value = console.input(f"[bold cyan]{title}[/bold cyan] ", password=masked).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if value or not required:
            return value
        console.print("[yellow]不能为空，请重新输入（Ctrl+C 取消）。[/yellow]")


def prompt_text(console, title, *, default="", required=False):
    """明文输入（用于自定义模型 id）。取消返回 None。"""
    suffix = f" [dim]({default})[/dim]" if default else ""
    while True:
        try:
            raw = console.input(f"[bold cyan]{title}[/bold cyan]{suffix} ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if raw:
            return raw
        if default:
            return default
        if not required:
            return ""
        console.print("[yellow]不能为空，请重新输入（Ctrl+C 取消）。[/yellow]")