import argparse
import os
import sys
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from rich.console import Console

from genesisai.app.terminal_view import CliSnapshot, CliView
from genesisai.model.config import build_model, load_model_config
from genesisai.model.settings import load_settings
from genesisai.model.providers.catalog import PROVIDERS
from genesisai.app import onboarding
from genesisai.app.desktop_pet import DesktopPet
from genesisai.app.session_picker import choose_session, session_entries
from genesisai.app.select import is_interactive, select
from genesisai.app.run_queue import QueueItem, RunQueue
from genesisai.shared.ids import uid
from genesisai.app.command_input import COMMANDS
from genesisai.shared.time_limits import DEFAULT_TASK_SECONDS, DEFAULT_REQUEST_TIMEOUT
from genesisai.agent.loop import Runner
from genesisai.shared.security import Access
from genesisai.core.extensions.skills.registry import SkillError, SkillRegistry
from genesisai.core.extensions.workflows import WorkflowError, WorkflowRegistry
from genesisai.core.state.store import HostLock, Store, application_state_root, default_workspace
from genesisai.core.tools.tool_runtime import ToolRuntime
from genesisai.core.tools.permissions import PermissionPolicy
from genesisai.core.tools.catalog import ToolCatalog
from genesisai.core.tools.registry import ToolRegistry
from genesisai.core.project_docs import AgentDocsManager


PROJECT_ROOT = Path(__file__).resolve().parents[3]
APPROVE_ALIASES = {"approve", "yes", "y", "确认", "同意"}
REJECT_ALIASES = {"reject", "no", "n", "拒绝", "不同意"}


@dataclass
class CliSession:
    """当前 CLI 绑定的连续会话及其运行组件。"""

    store: Store
    runtime: ToolRuntime
    runner: Runner
    snapshot: CliSnapshot


def select_workspace(explicit: Path | None, *, create=True) -> Path:
    """显式目录优先，否则使用固定默认工作区；首次使用自动创建。"""
    workspace = Path(explicit).expanduser().resolve() if explicit is not None else default_workspace()
    if workspace.exists() and not workspace.is_dir():
        raise ValueError('工作区必须是目录')
    if workspace.is_relative_to(application_state_root()):
        raise ValueError('工作区不能位于应用历史目录内')
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    elif not workspace.is_dir():
        raise ValueError('历史会话的工作区已不存在，请恢复原目录或新建会话')
    return workspace


def build_cli_session(
    store,
    *,
    roots,
    output,
    model,
    permission_mode,
    max_rounds,
    seconds,
    on_event,
    workspace=None,
    logger=None,
    request_timeout=DEFAULT_REQUEST_TIMEOUT,
):
    """将一个持久化 Session 绑定到访问边界、Runtime、Runner 和 UI 快照。"""
    store.data.update(grants=list(roots), output=str(output))
    store.save()
    actual_workspace = Path(workspace or store.data.get('workspace') or store.workspace).resolve()
    access = Access(roots, output, store.root, workspace_root=actual_workspace)
    runtime = ToolRuntime(
        store,
        access,
        permission_mode=permission_mode,
        logger=logger,
    )
    inventory = runtime.inventory()
    skills = SkillRegistry(actual_workspace, runtime.registry)
    snapshot = CliSnapshot(
        session_id=store.id,
        model_name=getattr(model, 'model', type(model).__name__),
        workspace=str(actual_workspace),
        history_root=str(store.root),
        input_roots=list(roots),
        output=str(output),
        permission_mode=runtime.permission_mode,
        registered_tools=len(runtime.registry),
        enabled_tools=len(inventory['enabled']),
        skill_name=','.join(store.data['tool_runtime']['active_skills']) or '未加载',
        git_state='工具可用' if 'run_commands' in runtime.registry and runtime.registry.get('run_commands').enabled else '不可用',
        agent_docs_status=(AgentDocsManager(actual_workspace).summary().get('active_status') or '就绪')
        if AgentDocsManager.exists(actual_workspace) else '未初始化',
    )
    runner = Runner(model, runtime, max_rounds=max_rounds, seconds=seconds, request_timeout=request_timeout, on_event=on_event)
    store.data['run_runtime']['execution_seconds_limit'] = seconds
    store.data['run_runtime']['request_timeout_seconds'] = request_timeout
    store.save()
    return CliSession(store=store, runtime=runtime, runner=runner, snapshot=snapshot)


def load_environment(explicit=None, workspace=None):
    """先加载工作区 .env，再补充本项目 .env（不读取项目以外的上级目录）。"""
    # 进程中已有的变量优先。工作区 .env 优先，GenesisAI/.env 只补充
    # 尚未设置的变量；不读取项目上级目录，避免历史遗留 .env 污染密钥。
    candidates = [Path(explicit)] if explicit else []
    if not explicit:
        if workspace:
            candidates.append(Path(workspace).resolve() / '.env')
        candidates.append(PROJECT_ROOT / '.env')
    for candidate in candidates:
        if candidate.is_file():
            values = dotenv_values(candidate)
            for provider in PROVIDERS:
                name = provider.api_key_env
                value = (values.get(name) or '').strip()
                if not os.environ.get(name, '').strip() and value:
                    os.environ[name] = value
            load_dotenv(candidate, override=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description='GenesisAI：面向小型项目开发的单 Agent CLI')
    parser.add_argument('--config', type=Path, help='模型 YAML，默认读取 config/model.yaml')
    parser.add_argument('--config-dir', type=Path, help='用户配置目录，默认 ~/.genesisai')
    parser.add_argument('--auth', action='store_true', help='交互式选择模型提供商/模型并写入配置后退出')
    parser.add_argument('--env-file', type=Path, help='显式 .env 路径；默认读取工作区根目录及本项目 .env')
    parser.add_argument('--workspace', type=Path, help='任务工作目录，默认 ~/GenesisAI/workspace；历史统一保存在默认工作区')
    parser.add_argument('--input', type=Path, action='append', default=[], help='授权读取目录/文件，可重复')
    parser.add_argument('--output', type=Path, help='新文件输出目录')
    parser.add_argument('--session', help='恢复会话 ID')
    parser.add_argument('--permissions', choices=['user', 'auto'], help='用户审批或自动审批；自动审批可读写工作区外的宿主文件')
    docs_group = parser.add_mutually_exclusive_group()
    docs_group.add_argument('--yes-agent-docs', action='store_true', help='在已指定工作区初始化 GenesisAI 管理的 agent_docs')
    docs_group.add_argument('--no-agent-docs', action='store_true', help='本次启动不初始化 agent_docs')
    parser.add_argument('--prompt', help='执行单次输入后退出；需要确认时保留会话')
    parser.add_argument('--max-rounds', type=int, default=20)
    parser.add_argument('--seconds', type=int, default=DEFAULT_TASK_SECONDS, help='一轮任务总执行时限，默认 300 秒')
    parser.add_argument('--request-timeout', type=int, default=DEFAULT_REQUEST_TIMEOUT, help='单次模型请求超时，默认 60 秒')
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure:
            reconfigure(encoding='utf-8', errors='replace')
    console = Console(legacy_windows=False)
    view = CliView(console)
    if args.max_rounds < 1 or args.seconds < 1 or args.request_timeout < 1:
        parser.error('预算必须为正数')
    interactive = not args.prompt and bool(getattr(sys.stdin, 'isatty', lambda: False)())
    try:
        actual_workspace = select_workspace(args.workspace, create=not args.session)
    except (ValueError, OSError) as exc:
        view.render_error(str(exc), title='工作区错误')
        return 2
    if args.auth:
        load_environment(args.env_file, actual_workspace)
        configured = onboarding.run_onboarding(console, directory=args.config_dir)
        if configured is None:
            view.render_error('已取消模型配置。', title='模型配置')
            return 2
        view.render_model_settings(configured)
        return 0
    state = None
    session_logger = None
    def record_console_output(logger=None):
        target = logger or session_logger
        if target is not None:
            rendered = console.export_text(clear=True)
            if rendered:
                target.record('cli_output', text=rendered)
        elif console.record:
            console.export_text(clear=True)

    state_root = application_state_root()
    initial_environment = dict(os.environ)
    environment_from_files = set()
    try:
        with HostLock(state_root), ExitStack() as lifetime:
            pet = DesktopPet()
            lifetime.callback(pet.close)
            runs = RunQueue() if interactive and is_interactive(console) else None
            console.record = True
            store = None
            model = None
            env_workspace = None
            docs_offered = set()
            roots = [str(p.resolve(strict=True)) for p in args.input] or [str(actual_workspace)]
            output = str((args.output or actual_workspace).resolve())
            mode = args.permissions or 'user'
            staged_skills = []

            def restore_environment():
                for key in environment_from_files:
                    if key in initial_environment:
                        os.environ[key] = initial_environment[key]
                    else:
                        os.environ.pop(key, None)

            def workspace_env():
                nonlocal env_workspace, environment_from_files
                if env_workspace == actual_workspace:
                    return
                restore_environment()
                load_environment(args.env_file, actual_workspace)
                environment_from_files = {k for k in os.environ if os.environ[k] != initial_environment.get(k)}
                env_workspace = actual_workspace

            def check_paths(workspace, inputs, destination):
                if any(not Path(root).resolve().is_relative_to(workspace) for root in inputs):
                    raise ValueError('输入目录必须位于用户指定的工作区内')
                path = Path(destination).resolve()
                if not path.is_relative_to(workspace):
                    raise ValueError('输出目录必须位于用户指定的工作区内')
                if any(path.is_relative_to(root) for root in (state_root, workspace / '.genesis')):
                    raise ValueError('输出目录不能位于 .genesis 运行状态目录内')

            def leave_session():
                nonlocal state, session_logger, model
                if state:
                    state.runtime.mcp.close()
                client = getattr(model, 'client', None)
                if client:
                    client.close()
                record_console_output()
                if session_logger:
                    session_logger.record('session_close', status=store.data['status'])
                state = session_logger = model = None
                view.draft = ''

            def open_session(session_id, *, startup=False):
                nonlocal store, actual_workspace, roots, output, session_logger, mode, staged_skills
                selected = Store(state_root, session_id, workspace=args.workspace if startup else None)
                workspace = select_workspace(selected.workspace, create=False)
                saved = selected.data
                inputs = saved['grants'] or [str(workspace)]
                destination = saved['output'] or str(workspace)
                check_paths(workspace, inputs, destination)
                if startup and ((args.input and roots != inputs) or (args.output and output != destination)):
                    raise ValueError('恢复时不能更改授权根目录或输出目录')
                if session_logger:
                    session_logger.record('session_switch', next_session_id=session_id)
                leave_session()
                store, actual_workspace, roots, output = selected, workspace, inputs, destination
                mode = (args.permissions if startup else None) or saved.get('permission_mode', 'user')
                staged_skills = list(saved['tool_runtime']['active_skills'])
                session_logger = store.get_logger()
                session_logger.record('session_open', resumed=True, workspace=str(workspace))
                view.recent = []
                view.render_opened(store)
                pet.update(saved['status'])

            def home():
                pet.update('idle')
                view.recent = session_entries(console, state_root, workspace=actual_workspace)[:5]
                view.render_home(actual_workspace, view.recent)

            def create_store():
                nonlocal store, session_logger
                store = Store(state_root, workspace=actual_workspace)
                session_logger = store.get_logger()
                session_logger.record('session_open', resumed=False, workspace=str(actual_workspace), options=vars(args))

            def activate():
                nonlocal state, model
                if model is not None:
                    return
                workspace_env()
                current_settings = load_settings(args.config_dir)
                if current_settings:
                    needs_key = not onboarding.has_credentials(current_settings)
                else:
                    needs_key = interactive and not onboarding.env_key(load_model_config(args.config).provider)
                if needs_key:
                    if not interactive:
                        raise ValueError('模型配置缺少 API Key；请运行 python cli.py --auth 完成配置')
                    current_settings = onboarding.run_onboarding(console, directory=args.config_dir, initial=current_settings)
                    if current_settings is None:
                        raise ValueError('已取消模型配置，草稿已保留')
                selected_model, _ = (build_model(args.config, settings=current_settings)
                                          if current_settings is not None else build_model(args.config))
                try:
                    if store is None:
                        create_store()
                    store.data['tool_runtime']['active_skills'] = staged_skills
                    state = build_cli_session(store, roots=roots, output=output, model=selected_model,
                                              permission_mode=mode, max_rounds=args.max_rounds,
                                              seconds=args.seconds, request_timeout=args.request_timeout,
                                              on_event=runs.emit if runs else view.render_progress,
                                              workspace=actual_workspace, logger=session_logger)
                    view.recent = []
                    model = selected_model
                except BaseException:
                    client = getattr(selected_model, 'client', None)
                    if client:
                        client.close()
                    raise

            def submit(text, *, on_registered=None):
                view.draft = text
                if len(text) > 16000 or not text.strip():
                    raise ValueError('输入不能为空或超过16000字符')
                new = store is None
                if store:
                    Runner.check_available(store.data)
                if new and args.prompt:
                    create_store()
                activate()
                if new:
                    session_logger.record('cli_input', text=text)
                view.draft = ''
                selector = state.runtime.prompt
                if (interactive and not args.no_agent_docs and actual_workspace not in docs_offered
                        and not AgentDocsManager.exists(actual_workspace)
                        and selector.requests_development_execution(text, selector.protocols_for(text))):
                    docs_offered.add(actual_workspace)
                    view.render_agent_docs_consent(str(actual_workspace / 'agent_docs'))
                    consent = view.request_consent('创建项目任务记录', '初始化项目记录')
                    session_logger.record('startup_input', purpose='agent_docs_consent', text=consent)
                    if consent == 'allow':
                        AgentDocsManager(actual_workspace, create=True)
                return run_and_show(lambda: state.runner.start(text, on_registered=on_registered))

            if args.session:
                open_session(args.session, startup=True)
            else:
                check_paths(actual_workspace, roots, output)
                if not args.prompt:
                    home()
            if args.yes_agent_docs and not AgentDocsManager.exists(actual_workspace):
                AgentDocsManager(actual_workspace, create=True)
            record_console_output()

            def show(result):
                d = state.store.data
                pet.update(result['status'])
                view.render_result(result, d)
                if result['status'] == 'awaiting_confirmation':
                    call = d['pending'][0]
                    preview = d['calls'][call['id']]['preview']
                    view.render_confirmation(call['name'], preview)

            def close_queue():
                runs.close()
                if runs.future is not None:
                    runs.drain(render_progress)
                    runs.flush_preview(view.render_progress)
                    result = runs.finish()
                    if result['status'] == 'awaiting_confirmation':
                        result = state.runner.cancel()
                    show(result)
                if runs.pending and session_logger:
                    session_logger.record('queue_discard', count=len(runs.pending), reason='exit')

            if runs:
                lifetime.callback(close_queue)

            def render_progress(kind, value):
                view.render_progress(kind, value)
                if kind == 'command_output':
                    # Flush Rich's transcript for each displayed batch, even
                    # while the user keeps the same input prompt open.
                    record_console_output()

            def run_and_show(action, message='GenesisAI 正在处理...'):
                if runs:
                    view.prepare_run(state.snapshot, store.data)
                    runs.start(action)
                    pet.update('running')
                    return
                pet.update('running')
                try:
                    with view.working(message):
                        result = action()
                    show(result)
                    return result
                except Exception:
                    pet.update('failed')
                    raise
                finally:
                    record_console_output()

            def configure_model(command):
                nonlocal model, state
                workspace_env()
                current_settings = load_settings(args.config_dir)
                if not interactive:
                    name = current_settings.model if current_settings else load_model_config(args.config).model
                    console.print('模型  ' + name if command == '/model' else '配置需要交互式终端；请运行 python cli.py --auth。')
                    return
                if command == '/model' and current_settings:
                    updated = onboarding.select_model_only(console, current_settings, directory=args.config_dir)
                else:
                    updated = onboarding.run_onboarding(console, directory=args.config_dir, initial=current_settings)
                if updated:
                    if state:
                        state.runtime.mcp.close()
                    client = getattr(model, 'client', None)
                    if client:
                        client.close()
                    model = state = None
                    view.render_model_settings(updated)

            if args.prompt:
                submit(args.prompt)
                return 0 if state.store.data['status'] == 'completed' else 2

            def print_queue():
                console.print(f'队列{"已暂停" if runs.paused else ""} · 等待 {len(runs.pending)} 项')
                for index, item in enumerate(runs.pending, 1):
                    console.print(f'{index}. {item.text[:100]}', markup=False, highlight=False)
                if runs.paused:
                    console.print('输入 /queue resume 继续，或 /queue clear 清空。')

            def enqueue(text):
                if len(text) > 16000:
                    raise ValueError('输入不能超过16000字符')
                item = QueueItem(uid('queue'), text)
                runs.pending.append(item)
                session_logger.record('queue_add', event_run_id=None, round_num=0,
                                      queue_id=item.id, position=len(runs.pending), text=text)
                console.print(f'已排队 · 第 {len(runs.pending)} 项')

            def start_next():
                draft = view.draft
                item = runs.pending[0]

                def registered(run_id):
                    item.run_id = run_id
                    session_logger.record('queue_start', event_run_id=run_id, round_num=0, queue_id=item.id)

                try:
                    session_logger.record('queue_dispatch', event_run_id=None, round_num=0, queue_id=item.id)
                    submit(item.text, on_registered=registered)
                except BaseException:
                    runs.paused = True
                    raise
                else:
                    runs.starting = runs.pending.popleft()
                finally:
                    view.draft = draft

            while True:
                try:
                    if runs and runs.future is not None:
                        if runs.future.done():
                            runs.drain(render_progress)
                            runs.flush_preview(view.render_progress)
                            stop_requested = runs.control.cancelled.is_set()
                            try:
                                result = runs.finish()
                            except (ValueError, RuntimeError, OSError):
                                pet.update('failed')
                                if runs.paused:
                                    print_queue()
                                raise
                            if stop_requested and result['status'] == 'awaiting_confirmation':
                                result = state.runner.cancel()
                            show(result)
                            if runs.pending:
                                if result['status'] == 'completed' and not runs.paused:
                                    start_next()
                                else:
                                    print_queue()
                            continue
                        raw = view.prompt_running(runs, render_progress)
                        if raw is None:
                            continue
                        text = raw.strip()
                        command = text.casefold()
                        session_logger.record('cli_input', text=raw)
                        if command == '/exit':
                            console.print(f'正在停止当前任务，退出后丢弃 {len(runs.pending)} 项排队消息。')
                            break
                        if command == '/cancel':
                            runs.control.cancel()
                            console.print('已请求取消，正在等待当前操作收尾。')
                        elif command == '/status':
                            console.print(f'{"取消中" if runs.control.cancelled.is_set() else runs.stage} · 等待 {len(runs.pending)} 项')
                        elif command == '/pet':
                            pet.show()
                        elif command == '/queue':
                            print_queue()
                        elif command == '/queue clear':
                            runs.pending.clear()
                            console.print('排队消息已清空。')
                        elif command.startswith('/'):
                            console.print('运行中可使用 /pet、/status、/cancel、/queue、/queue clear 或 /exit。')
                        elif text:
                            enqueue(text)
                        continue
                    data = store.data if store else {}
                    staged_skills = data.get('tool_runtime', {}).get('active_skills', staged_skills)
                    snapshot = state.snapshot if state else CliSnapshot(
                        store.id if store else '', 'GenesisAI', str(actual_workspace), roots,
                        output, mode, 0, history_root=str(state_root))
                    view.bind_session(snapshot, data)
                    try:
                        text = view.prompt()
                    except OSError as exc:
                        view.render_error(str(exc), title='无法读取终端输入')
                        return 2
                    if session_logger:
                        session_logger.record('cli_input', text=getattr(view, 'last_input_raw', text))
                    command = text.casefold()
                    if command == '/' or (command == '/help' and interactive):
                        if interactive:
                            selected_command = view.choose_command()
                        else:
                            choices = [(cmd, desc) for cmd, desc, group in COMMANDS]
                            picked = select(console, '选择操作', choices, searchable=True)
                            selected_command = choices[picked][0] if picked is not None else None
                        if not selected_command:
                            continue
                        text = selected_command
                        command = text.casefold()
                        if session_logger:
                            session_logger.record('cli_menu_choice', command=command)
                    awaiting = data.get('status') == 'awaiting_confirmation'
                    if command == '/pet':
                        pet.show()
                        continue
                    if command == '/exit':
                        if runs and runs.pending:
                            console.print(f'退出后丢弃 {len(runs.pending)} 项排队消息。')
                        view.render_goodbye()
                        break
                    if not text:
                        continue
                    if command in {'/queue', '/queue resume', '/queue clear'}:
                        if not runs:
                            console.print('队列需要交互式终端。')
                        elif command == '/queue clear':
                            runs.pending.clear()
                            runs.paused = False
                            console.print('排队消息已清空。')
                        elif command == '/queue resume' and runs.pending:
                            Runner.check_available(data)
                            runs.paused = False
                            start_next()
                        else:
                            print_queue()
                        continue
                    if runs and runs.pending and (command in {'/sessions', '/recent', '/new', '/auth', '/model'}
                            or command.startswith(('/workspace ', '/workflow '))):
                        raise ValueError('请先处理当前会话的队列。使用 /queue 查看或 /queue clear 清空。')
                    if runs and not command.startswith('/') and (awaiting or runs.pending):
                        if not awaiting or command not in APPROVE_ALIASES | REJECT_ALIASES:
                            enqueue(text)
                            continue
                    if command == '/workspace':
                        console.print(str(actual_workspace), markup=False)
                        console.print('输入 /workspace 路径 打开该工作区的首页。')
                        continue
                    if command in {'/sessions', '/recent', '/new'} or command.startswith('/workspace '):
                        if store:
                            Runner.check_available(data)
                        if command == '/new' or command.startswith('/workspace '):
                            workspace = actual_workspace if command == '/new' else select_workspace(
                                Path(text.split(maxsplit=1)[1].strip('"')))
                            leave_session()
                            if workspace != actual_workspace:
                                actual_workspace, roots, output = workspace, [str(workspace)], str(workspace)
                            store = None
                            mode = 'user'
                            staged_skills = []
                            home()
                        else:
                            if command == '/recent' and view.recent:
                                picked = select(console, '最近对话', [(t, d) for _, t, d in view.recent])
                                selected = view.recent[picked][0] if picked is not None else None
                            else:
                                selected = choose_session(console, state_root, store.id if store else None)
                            if selected:
                                open_session(selected)
                        continue
                    if command in {'/auth', '/model'}:
                        configure_model(command)
                        continue
                    if command == '/history':
                        view.render_history(data)
                        continue
                    if command == '/help':
                        view.render_help()
                        continue
                    if command == '/status':
                        view.render_status(data, snapshot)
                        if runs and runs.pending:
                            print_queue()
                        continue
                    if command == '/logs':
                        if session_logger:
                            view.render_logs(str(session_logger.journal.path))
                        else:
                            console.print('发送第一条消息后生成会话日志。')
                        continue
                    if command in {'/project', '/project status', '/project init'}:
                        manager = AgentDocsManager(actual_workspace, create=command == '/project init')
                        view.render_data('Agent Docs', manager.context() if manager.available else {
                            'available': False, 'path': str(actual_workspace / 'agent_docs'), 'next': '/project init'})
                        continue
                    if command.split()[0] == '/permissions':
                        parts = command.split()
                        if len(parts) == 2:
                            selected_mode = PermissionPolicy(parts[1]).mode
                            if state:
                                state.runtime.set_permission(selected_mode)
                                state.snapshot = replace(state.snapshot, permission_mode=selected_mode)
                            elif store:
                                store.data['permission_mode'] = selected_mode
                                store.save()
                            mode = selected_mode
                        elif len(parts) > 2:
                            raise ValueError('用法：/permissions user 或 /permissions auto')
                        view.render_permissions(mode)
                        if awaiting:
                            console.print('已有待确认操作，请使用 /approve 或 /reject。')
                        continue
                    if command == '/tools':
                        inventory = state.runtime.inventory() if state else ToolCatalog(ToolRegistry(), None, store).inventory()
                        view.render_tools(inventory)
                        continue
                    if command == '/budget':
                        view.render_budget(data)
                        continue
                    if command == '/trace':
                        if store:
                            view.render_trace(store)
                        else:
                            console.print('当前没有运行记录。')
                        continue
                    if command == '/diff':
                        view.render_changes(data.get('changes', []), data.get('run_id'))
                        continue
                    if command == '/details':
                        view.toggle_details()
                        continue
                    if runs and command == '/cancel' and data.get('status') not in {'running', 'awaiting_confirmation', 'interrupted'}:
                        console.print('当前没有运行中的任务。')
                        continue
                    if state is None:
                        if command == '/cancel' and store:
                            # Cancellation is local and must work without model credentials.
                            local_state = build_cli_session(store, roots=roots, output=output,
                                model=None, permission_mode=mode,
                                max_rounds=args.max_rounds, seconds=args.seconds, request_timeout=args.request_timeout,
                                on_event=view.render_progress)
                            try:
                                result = local_state.runner.cancel()
                                pet.update(result['status'])
                                view.render_result(result, data)
                                if runs and runs.pending:
                                    runs.paused = True
                                    print_queue()
                            finally:
                                local_state.runtime.mcp.close()
                            continue
                        if command.startswith('/') and not command.startswith(('/skills', '/workflow')):
                            if store and command in {'/approve', '/reject', '/resume', '/compact'}:
                                activate()
                            elif command in {cmd for cmd, _, _ in COMMANDS}:
                                console.print('当前没有活动任务。')
                                continue
                            else:
                                view.render_error('未知命令；使用 /help 查看可用命令。', title='未知命令')
                                continue
                        if awaiting and command in APPROVE_ALIASES | REJECT_ALIASES:
                            activate()
                    if awaiting and command in APPROVE_ALIASES:
                        run_and_show(lambda: state.runner.confirm(True), '正在执行已批准操作...')
                    elif awaiting and command in REJECT_ALIASES:
                        run_and_show(lambda: state.runner.confirm(False), '正在拒绝待确认操作...')
                    elif command == '/compact':
                        summary = state.runner.context_budgeter.compact(force=True)
                        view.render_data('Context Compact', {'summary': summary, 'report': state.store.data['run_runtime'].get('context_report', {})})
                    elif command.startswith('/skills'):
                        registry = SkillRegistry(actual_workspace, state.runtime.registry if state else ToolRegistry())
                        parts = text.split(maxsplit=2)
                        action = parts[1].casefold() if len(parts) > 1 else 'list'
                        if action == 'list':
                            value = {'skills': registry.search(), 'loaded': staged_skills, 'diagnostics': registry.diagnostics}
                        elif action == 'search':
                            value = registry.search(parts[2] if len(parts) > 2 else '')
                        elif action == 'show' and len(parts) > 2:
                            value = registry.describe(parts[2], include_content=True)
                        elif action == 'load' and len(parts) > 2:
                            names = [item.strip() for item in parts[2].split(',') if item.strip()]
                            value = registry.load(names, staged_skills)
                            staged_skills = value
                            if store:
                                store.data['tool_runtime']['active_skills'] = value
                                store.save()
                        elif action == 'unload':
                            current = staged_skills
                            value = [] if len(parts) < 3 else [item for item in current if item != parts[2]]
                            staged_skills = value
                            if store:
                                store.data['tool_runtime']['active_skills'] = value
                                store.save()
                        else:
                            raise ValueError('用法：/skills list|search 词|show 名称|load 名称[,名称]|unload [名称]')
                        if state:
                            state.snapshot = replace(state.snapshot, skill_name=','.join(staged_skills) or None)
                        view.render_data('Skills', value)
                    elif command.startswith('/workflow'):
                        registry = WorkflowRegistry(actual_workspace)
                        parts = text.split(maxsplit=1)
                        if len(parts) < 2 or not parts[1].strip():
                            view.render_data('Workflows', {'workflows': registry.summary(), 'diagnostics': registry.diagnostics})
                        else:
                            try:
                                prompt = registry.expand(parts[1].strip())
                            except WorkflowError as exc:
                                view.render_data('Workflows', {'error': str(exc), 'workflows': registry.summary()})
                            else:
                                submit(prompt)
                    elif command == '/approve':
                        run_and_show(lambda: state.runner.confirm(True), '正在执行已批准操作...')
                    elif command == '/reject':
                        run_and_show(lambda: state.runner.confirm(False), '正在拒绝待确认操作...')
                    elif command == '/resume':
                        run_and_show(state.runner.resume, '正在恢复会话...')
                    elif command == '/cancel':
                        show(state.runner.cancel())
                        if runs and runs.pending:
                            runs.paused = True
                            print_queue()
                    elif command.startswith('/'):
                        view.render_error('未知命令；使用 /help 查看可用命令。', title='未知命令')
                    elif awaiting and text:
                        view.render_error('请先处理确认。输入 /approve 或“确认”仅批准当前操作；/reject 拒绝，/cancel 取消。其他语句不会自动批准。')
                        pending = data['pending'][0]
                        view.render_confirmation(pending['name'], data['calls'].get(pending['id'], {}).get('preview', {}))
                    elif text:
                        submit(text)
                except (EOFError, KeyboardInterrupt) as exc:
                    if isinstance(exc, KeyboardInterrupt) and runs and runs.future is not None:
                        runs.control.cancel()
                        console.print('已请求取消当前任务。')
                        continue
                    if session_logger:
                        session_logger.record('cli_closed', reason='eof_or_interrupt')
                    view.render_goodbye()
                    break
                except (ValueError, RuntimeError, SkillError, OSError) as exc:
                    if session_logger:
                        session_logger.error('cli_error', exc)
                    view.render_error(str(exc))
                finally:
                    record_console_output()
            return 0
    except (ValueError, RuntimeError, OSError) as exc:
        if session_logger is not None:
            session_logger.error('cli_error', exc)
        view.render_error(str(exc), title='启动失败')
        return 2
    finally:
        if environment_from_files:
            restore_environment()
        if state:
            state.runtime.mcp.close()
            client = getattr(state.runner.model, 'client', None)
            if client:
                client.close()
        if session_logger is not None:
            record_console_output()
            session_logger.record('session_close', status=session_logger.store.data['status'])


def entry():
    raise SystemExit(main())



