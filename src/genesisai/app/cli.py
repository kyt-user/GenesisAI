import argparse
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv, dotenv_values
from rich.console import Console

from genesisai.app.terminal_view import CliSnapshot, CliView
from genesisai.model.config import build_model, load_model_config
from genesisai.model.settings import load_settings
from genesisai.model.providers.catalog import PROVIDERS
from genesisai.app import onboarding
from genesisai.app.session_picker import choose_session
from genesisai.app.select import select
from genesisai.app.command_input import COMMANDS
from genesisai.shared.time_limits import DEFAULT_TASK_SECONDS, DEFAULT_REQUEST_TIMEOUT
from genesisai.agent.loop import Runner
from genesisai.shared.security import Access
from genesisai.core.extensions.skills.registry import SkillError, SkillRegistry
from genesisai.core.extensions.workflows import WorkflowError, WorkflowRegistry
from genesisai.core.state.store import HostLock, Store, application_state_root, default_workspace
from genesisai.core.tools.tool_runtime import ToolRuntime
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
    remote,
    model,
    confirm_writes,
    confirm_search,
    max_rounds,
    seconds,
    on_event,
    workspace=None,
    confirm_shell=True,
    logger=None,
    request_timeout=DEFAULT_REQUEST_TIMEOUT,
):
    """将一个持久化 Session 绑定到访问边界、Runtime、Runner 和 UI 快照。"""
    store.data.update(grants=list(roots), output=str(output), remote_allowed=remote)
    store.save()
    actual_workspace = Path(workspace or store.data.get('workspace') or store.workspace).resolve()
    access = Access(roots, output, store.root, workspace_root=actual_workspace)
    runtime = ToolRuntime(
        store,
        access,
        confirm_writes=confirm_writes,
        confirm_search=confirm_search,
        confirm_shell=confirm_shell,
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


def apply_permission_command(runtime, text):
    """解析 `/permissions <network|writes|shell> <ask|allow>`。"""
    parts = text.casefold().split()
    if len(parts) == 1:
        return False
    if len(parts) != 3 or parts[0] != '/permissions':
        raise ValueError('用法：/permissions network|writes|shell ask|allow')
    runtime.set_permission(parts[1], parts[2])
    return True


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
    parser.add_argument('--allow-remote-data', action='store_true', help='允许授权资料与会话发送至远程模型，并允许据其生成公开搜索词')
    parser.add_argument('--yes-writes', action='store_true', help='预先允许输出目录内的新文件创建/复制，仍禁止覆盖')
    parser.add_argument('--yes-search', action='store_true', help='允许模型生成的关键词和URL发送到公开网络；否则每次网络请求需确认')
    parser.add_argument('--yes-shell', action='store_true', help='允许当前 Session 的受控 Shell 与测试命令')
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

    state_root = application_state_root()
    initial_environment = dict(os.environ)
    try:
        with HostLock(state_root):
            store = Store(state_root, args.session,
                          workspace=None if args.session and args.workspace is None else actual_workspace)
            actual_workspace = store.workspace
            if not actual_workspace.is_dir():
                raise ValueError('历史会话的工作区已不存在，请恢复原目录或新建会话')
            load_environment(args.env_file, actual_workspace)
            environment_from_files = {key for key in os.environ if initial_environment.get(key) != os.environ[key]}
            session_logger = store.get_logger()
            console.record = True
            session_logger.record('session_open', workspace=str(actual_workspace), resumed=bool(args.session),
                                  log_path=str(session_logger.journal.path), options=vars(args))
            d = store.data
            roots = [str(p.resolve(strict=True)) for p in args.input] or d['grants'] or [str(actual_workspace)]
            if any(not Path(root).is_relative_to(actual_workspace) for root in roots):
                raise ValueError('输入目录必须位于用户指定的工作区内')
            output_path = (args.output or (Path(d['output']) if d['output'] else actual_workspace)).resolve()
            if not output_path.is_relative_to(actual_workspace):
                raise ValueError('输出目录必须位于用户指定的工作区内')
            if any(output_path.is_relative_to(root) for root in (state_root, actual_workspace / '.genesis')):
                raise ValueError('输出目录不能位于 .genesis 运行状态目录内')
            output = str(output_path)
            if args.session and (roots != d['grants'] or output != d['output']):
                raise ValueError('恢复时不能更改授权根目录或输出目录，请创建新会话')
            current_settings = load_settings(args.config_dir)
            needs_config = current_settings is None or not onboarding.has_credentials(current_settings)
            if current_settings is None and interactive and not args.prompt:
                configured_model = load_model_config(args.config)
                needs_config = not onboarding.env_key(configured_model.provider)
            if needs_config and interactive and not args.prompt:
                current_settings = onboarding.run_onboarding(
                    console, directory=args.config_dir, initial=current_settings
                ) or current_settings
            if current_settings is not None and not onboarding.has_credentials(current_settings) and not interactive:
                raise ValueError('模型配置缺少 API Key；请运行 python cli.py --auth 完成配置')
            model, remote = (
                build_model(args.config, settings=current_settings)
                if current_settings is not None
                else build_model(args.config)
            )
            # 即使没有授权本地输入目录，远程提供商仍会收到用户提示词。
            # 因此应对整段远程会话取得授权，而不只是对本地文件外发授权。
            if remote and not args.allow_remote_data:
                if args.prompt:
                    raise ValueError('远程模型需 --allow-remote-data 明确允许会话、授权资料和搜索词发送')
                view.render_remote_consent(roots, getattr(model, 'model', None))
                consent = view.request_consent('允许本次会话使用远程模型', '远程模型授权')
                session_logger.record('startup_input', purpose='remote_data_consent', text=consent)
                if consent != 'allow':
                    raise ValueError('未允许远程资料发送')
            if not AgentDocsManager.exists(actual_workspace) and not args.no_agent_docs:
                initialize_docs = args.yes_agent_docs
                if interactive and not initialize_docs:
                    view.render_agent_docs_consent(str(actual_workspace / 'agent_docs'))
                    consent = view.request_consent('创建项目任务记录', '初始化项目记录')
                    session_logger.record('startup_input', purpose='agent_docs_consent', text=consent)
                    initialize_docs = consent.casefold() == 'allow'
                if initialize_docs:
                    AgentDocsManager(actual_workspace, create=True)
            saved_modes = d.get('permission_modes', {'network': 'ask', 'writes': 'ask', 'shell': 'ask'})
            base_confirm_writes = False if args.yes_writes else saved_modes['writes'] == 'ask'
            base_confirm_search = False if args.yes_search else saved_modes['network'] == 'ask'
            base_confirm_shell = False if args.yes_shell else saved_modes['shell'] == 'ask'
            if args.yes_writes:
                d['permission_modes']['writes'] = 'allow'
            if args.yes_search:
                d['permission_modes']['network'] = 'allow'
            if args.yes_shell:
                d['permission_modes']['shell'] = 'allow'
            store.save()

            def progress(kind, value):
                view.render_progress(kind, value)

            state = build_cli_session(
                store,
                roots=roots,
                output=output,
                remote=remote,
                model=model,
                confirm_writes=base_confirm_writes,
                confirm_search=base_confirm_search,
                max_rounds=args.max_rounds,
                seconds=args.seconds,
                request_timeout=args.request_timeout,
                on_event=progress,
                workspace=actual_workspace,
                confirm_shell=base_confirm_shell,
                logger=session_logger,
            )
            view.render_startup(state.snapshot, state.store.data)
            record_console_output()

            def show(result):
                d = state.store.data
                view.render_result(result, d)
                if result['status'] == 'awaiting_confirmation':
                    call = d['pending'][0]
                    preview = d['calls'][call['id']]['preview']
                    view.render_confirmation(call['name'], preview)

            def run_and_show(action, message='GenesisAI 正在处理...'):
                try:
                    with view.working(message):
                        result = action()
                    show(result)
                    return result
                finally:
                    record_console_output()

            def apply_model_settings(new_settings):
                nonlocal model, remote, current_settings
                model, remote = build_model(args.config, settings=new_settings)
                current_settings = new_settings
                state.runner.set_model(model)
                state.snapshot = replace(state.snapshot, model_name=getattr(model, 'model', new_settings.model))
                view.render_model_settings(new_settings)

            if args.prompt:
                run_and_show(lambda: state.runner.start(args.prompt))
                return 0 if state.store.data['status'] == 'completed' else 2
            while True:
                try:
                    view.bind_session(state.snapshot, state.store.data)
                    text = view.prompt()
                    state.runtime.logger.record('cli_input', text=getattr(view, 'last_input_raw', text))
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
                        state.runtime.logger.record('cli_menu_choice', command=command)
                    awaiting = state.store.data['status'] == 'awaiting_confirmation'
                    if command == '/exit':
                        view.render_goodbye()
                        break
                    if awaiting and command in APPROVE_ALIASES:
                        run_and_show(lambda: state.runner.confirm(True), '正在执行已批准操作...')
                    elif command == '/allow network run':
                        state.runtime.policy.allow_network_run()
                        state.store.save()
                        if awaiting and state.runtime.registry.get(state.store.data['pending'][0]['name']).spec.permission == 'network':
                            run_and_show(lambda: state.runner.confirm(True), '正在执行并允许本轮后续网络请求...')
                        else:
                            view.render_status(state.store.data, state.snapshot)
                    elif command in {'/allow network', '/allow writes', '/allow shell'}:
                        capability = command.split()[1]
                        state.runtime.set_permission(capability, 'allow')
                        state.snapshot = replace(state.snapshot, permission_mode=state.runtime.permission_mode)
                        pending_permission = None
                        if awaiting and state.store.data['pending']:
                            pending_name = state.store.data['pending'][0]['name']
                            pending_permission = state.runtime.registry.get(pending_name).spec.permission
                        aliases = {'network': 'network', 'writes': 'write', 'shell': 'shell'}
                        if awaiting and pending_permission == aliases[capability]:
                            run_and_show(lambda: state.runner.confirm(True), f'正在执行并允许本会话后续{capability}操作...')
                        else:
                            view.render_permissions(confirm_writes=state.runtime.confirm_writes, confirm_search=state.runtime.confirm_search, confirm_shell=state.runtime.confirm_shell)
                    elif awaiting and command in REJECT_ALIASES:
                        run_and_show(lambda: state.runner.confirm(False), '正在拒绝待确认操作...')
                    elif command == '/help':
                        view.render_help()
                    elif command == '/status':
                        view.render_status(state.store.data, state.snapshot)
                    elif command == '/budget':
                        view.render_budget(state.store.data)
                    elif command == '/compact':
                        summary = state.runner.context_budgeter.compact(force=True)
                        view.render_data('Context Compact', {'summary': summary, 'report': state.store.data['run_runtime'].get('context_report', {})})
                    elif command == '/trace':
                        view.render_trace(state.store)
                    elif command == '/details':
                        view.toggle_details()
                    elif command == '/history':
                        view.render_history(state.store.data['messages'])
                    elif command == '/model':
                        if not interactive:
                            view.render_model(state.snapshot)
                        elif current_settings is None:
                            updated = onboarding.run_onboarding(console, directory=args.config_dir)
                            if updated is not None:
                                apply_model_settings(updated)
                        else:
                            updated = onboarding.select_model_only(console, current_settings, directory=args.config_dir)
                            if updated is not None:
                                apply_model_settings(updated)
                    elif command == '/auth':
                        if interactive:
                            updated = onboarding.run_onboarding(console, directory=args.config_dir, initial=current_settings)
                            if updated is not None:
                                apply_model_settings(updated)
                        else:
                            view.render_error('配置需要交互式终端；请运行 python cli.py --auth。', title='模型配置')
                    elif command.startswith('/permissions'):
                        apply_permission_command(state.runtime, text)
                        state.snapshot = replace(state.snapshot, permission_mode=state.runtime.permission_mode)
                        view.render_permissions(
                            confirm_writes=state.runtime.confirm_writes,
                            confirm_search=state.runtime.confirm_search,
                            confirm_shell=state.runtime.confirm_shell,
                        )
                    elif command == '/tools':
                        view.render_tools(state.runtime.inventory())
                    elif command in {'/project', '/project status'}:
                        manager = AgentDocsManager(actual_workspace)
                        value = manager.context() if manager.available else {
                            'available': False,
                            'path': str(actual_workspace / 'agent_docs'),
                            'next': '/project init',
                        }
                        view.render_data('Agent Docs', value)
                    elif command == '/project init':
                        manager = AgentDocsManager(actual_workspace, create=True)
                        state.snapshot = replace(state.snapshot, agent_docs_status=manager.summary().get('active_status') or '就绪')
                        view.render_data('Agent Docs · 已初始化', manager.summary())
                    elif command == '/sessions':
                        state.runner.ensure_available()
                        selected_id = choose_session(console, state_root, state.store.id)
                        if selected_id is None:
                            continue
                        restored = Store(state_root, selected_id)
                        selected_workspace = select_workspace(restored.workspace, create=False)
                        selected_roots = restored.data['grants'] or [str(selected_workspace)]
                        selected_output = Path(restored.data['output'] or selected_workspace).resolve()
                        if any(not Path(p).resolve().is_relative_to(selected_workspace) for p in selected_roots):
                            raise ValueError('历史会话的授权目录已越出工作区')
                        if (not selected_output.is_relative_to(selected_workspace)
                                or selected_output.is_relative_to(state_root)
                                or selected_output.is_relative_to(selected_workspace / '.genesis')):
                            raise ValueError('历史会话的输出目录无效')
                        previous_environment = dict(os.environ)
                        selected_model = None
                        try:
                            for key in environment_from_files:
                                if key in initial_environment:
                                    os.environ[key] = initial_environment[key]
                                else:
                                    os.environ.pop(key, None)
                            load_environment(args.env_file, selected_workspace)
                            selected_model, selected_remote = (build_model(args.config, settings=current_settings)
                                if current_settings is not None else build_model(args.config))
                            if selected_remote and not (args.allow_remote_data or restored.data['remote_allowed']):
                                view.render_remote_consent(selected_roots, getattr(selected_model, 'model', None))
                                consent = view.request_consent('允许所选会话使用远程模型', '远程模型授权')
                                state.runtime.logger.record('startup_input', purpose='restore_remote_consent', text=consent)
                                if consent != 'allow':
                                    raise ValueError('已取消恢复远程会话')
                            modes = restored.data['permission_modes']
                            selected_state = build_cli_session(
                                restored, roots=selected_roots, output=str(selected_output), remote=selected_remote,
                                model=selected_model, confirm_writes=modes['writes'] == 'ask',
                                confirm_search=modes['network'] == 'ask', confirm_shell=modes['shell'] == 'ask',
                                max_rounds=args.max_rounds, seconds=args.seconds, request_timeout=args.request_timeout,
                                on_event=progress, workspace=selected_workspace)
                        except Exception:
                            selected_client = getattr(selected_model, 'client', None)
                            if selected_client is not None:
                                selected_client.close()
                            for key in set(os.environ) | set(previous_environment):
                                if key in previous_environment:
                                    os.environ[key] = previous_environment[key]
                                else:
                                    os.environ.pop(key, None)
                            raise
                        environment_from_files = {key for key in os.environ if initial_environment.get(key) != os.environ[key]}
                        old_logger = state.runtime.logger
                        record_console_output(old_logger)
                        old_logger.record('session_switch', next_session_id=selected_id)
                        previous_client = getattr(model, 'client', None)
                        if previous_client is not None and selected_model is not model:
                            previous_client.close()
                        state = selected_state
                        actual_workspace, roots, output = selected_workspace, selected_roots, str(selected_output)
                        model, remote = selected_model, selected_remote
                        session_logger = state.runtime.logger
                        session_logger.record('session_open', workspace=str(actual_workspace), resumed=True)
                        view.render_startup(state.snapshot, state.store.data)
                        view.render_status(state.store.data, state.snapshot)
                    elif command == '/new':
                        state.runner.ensure_available()
                        old_logger = state.runtime.logger
                        record_console_output(old_logger)
                        new_store = Store(state_root, workspace=actual_workspace)
                        new_output = output
                        state = build_cli_session(
                            new_store,
                            roots=roots,
                            output=new_output,
                            remote=remote,
                            model=model,
                            confirm_writes=True,
                            confirm_search=True,
                            max_rounds=args.max_rounds,
                            seconds=args.seconds,
                            request_timeout=args.request_timeout,
                            on_event=progress,
                            workspace=actual_workspace,
                            confirm_shell=True,
                        )
                        old_logger.record('session_switch', next_session_id=state.store.id)
                        session_logger = state.runtime.logger
                        state.runtime.logger.record('session_open', workspace=str(actual_workspace), resumed=False)
                        view.render_startup(state.snapshot, state.store.data)
                    elif command == '/logs':
                        state.runtime.logger.record('log_location', path=str(state.runtime.logger.journal.path))
                        view.render_logs(str(state.runtime.logger.journal.path))
                    elif command.startswith('/skills'):
                        registry = SkillRegistry(actual_workspace, state.runtime.registry)
                        parts = text.split(maxsplit=2)
                        action = parts[1].casefold() if len(parts) > 1 else 'list'
                        if action == 'list':
                            value = {'skills': registry.search(), 'loaded': state.store.data['tool_runtime']['active_skills'], 'diagnostics': registry.diagnostics}
                        elif action == 'search':
                            value = registry.search(parts[2] if len(parts) > 2 else '')
                        elif action == 'show' and len(parts) > 2:
                            value = registry.describe(parts[2], include_content=True)
                        elif action == 'load' and len(parts) > 2:
                            names = [item.strip() for item in parts[2].split(',') if item.strip()]
                            value = registry.load(names, state.store.data['tool_runtime']['active_skills'])
                            state.store.data['tool_runtime']['active_skills'] = value; state.store.save()
                        elif action == 'unload':
                            current = state.store.data['tool_runtime']['active_skills']
                            value = [] if len(parts) < 3 else [item for item in current if item != parts[2]]
                            state.store.data['tool_runtime']['active_skills'] = value; state.store.save()
                        else:
                            raise ValueError('用法：/skills list|search 词|show 名称|load 名称[,名称]|unload [名称]')
                        state.snapshot = replace(state.snapshot, skill_name=','.join(state.store.data['tool_runtime']['active_skills']) or None)
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
                                run_and_show(lambda: state.runner.start(prompt), '正在展开工作流...')
                    elif command == '/diff':
                        view.render_changes(state.store.data.get('changes', []), state.store.data.get('run_id'))
                    elif command == '/approve':
                        run_and_show(lambda: state.runner.confirm(True), '正在执行已批准操作...')
                    elif command == '/reject':
                        run_and_show(lambda: state.runner.confirm(False), '正在拒绝待确认操作...')
                    elif command == '/resume':
                        run_and_show(state.runner.resume, '正在恢复会话...')
                    elif command == '/cancel':
                        show(state.runner.cancel())
                    elif command.startswith('/'):
                        view.render_error('未知命令；使用 /help 查看可用命令。', title='未知命令')
                    elif awaiting and text:
                        view.render_error('请先处理确认。输入 /approve 或“确认”仅批准当前操作；/reject 拒绝，/cancel 取消。其他语句不会自动批准。')
                        pending = state.store.data['pending'][0]
                        view.render_confirmation(pending['name'], state.store.data['calls'].get(pending['id'], {}).get('preview', {}))
                    elif text:
                        run_and_show(lambda: state.runner.start(text))
                except (EOFError, KeyboardInterrupt):
                    state.runtime.logger.record('cli_closed', reason='eof_or_interrupt')
                    view.render_goodbye()
                    break
                except (ValueError, SkillError, OSError) as exc:
                    state.runtime.logger.error('cli_error', exc)
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
        if session_logger is not None:
            record_console_output()
            session_logger.record('session_close', status=session_logger.store.data['status'])


def entry():
    raise SystemExit(main())



