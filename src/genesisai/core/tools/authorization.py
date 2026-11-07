"""User-created, revocable session/task grants; no model-facing grant tool."""
from copy import deepcopy
import ipaddress
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from genesisai.shared.ids import uid
from genesisai.shared.process_tree import validate_environment_mapping
from genesisai.shared.security import ToolError


TERMINAL = {'completed', 'cancelled', 'superseded'}
FIELDS = {'id', 'session_id', 'scope', 'task_id', 'tools', 'paths', 'domains', 'address_ranges', 'env_from', 'headers_from',
          'command_prefixes', 'source', 'created_at', 'revoked_at'}


def domain_pattern(value):
    if isinstance(value, str):
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError:
            pass
    if not isinstance(value, str) or not value or any(char in value for char in '/:@\\'):
        raise ToolError('validation_error', 'domains 须为域名、*.域名或显式 *，不接受 URL')
    if value == '*':
        return value
    wildcard = value.startswith('*.')
    value = value[2:] if wildcard else value
    try:
        value = value.rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError as exc:
        raise ToolError('validation_error', '域名无效') from exc
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value) or '..' in value:
        raise ToolError('validation_error', '域名无效')
    return ('*.' if wildcard else '') + value


def domain_matches(host, patterns):
    host = domain_pattern(host)
    return any(pattern == '*' or host == pattern or
               (pattern.startswith('*.') and host.endswith(pattern[1:]) and host != pattern[2:])
               for pattern in patterns)


def address_ranges(values):
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ToolError('validation_error', 'address_ranges 必须为 IP 或 CIDR 字符串列表')
    try:
        return list(dict.fromkeys(ipaddress.ip_network(value, strict=False).with_prefixlen for value in values))
    except ValueError as exc:
        raise ToolError('validation_error', 'address_ranges 含无效 IP 或 CIDR') from exc


def ranges_within(requested, granted):
    requested, granted = address_ranges(requested), address_ranges(granted)
    parents = [ipaddress.ip_network(value) for value in granted]
    return bool(requested) and all(any(child.version == parent.version and child.subnet_of(parent)
        for parent in parents) for child in map(ipaddress.ip_network, requested))


def validate_header_mapping(mapping):
    if (not isinstance(mapping, dict) or any(not isinstance(key, str)
            or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) is None for key in mapping)
            or len({key.casefold() for key in mapping}) != len(mapping)):
        raise ToolError('validation_error', 'headers_from 必须为互不冲突的 HTTP 头名到宿主变量名映射')
    for source in mapping.values():
        validate_environment_mapping({'VALUE': source})
    return {key.casefold(): source for key, source in mapping.items()}


def destructive(args):
    return args.get('operation') in {'delete', 'remove', 'delete_tree'}


def validate_records(records, session_id):
    if not isinstance(records, list):
        raise ValueError('authorizations 必须为列表')
    seen = set()
    for item in records:
        if (not isinstance(item, dict) or set(item) != FIELDS or item['session_id'] != session_id
                or not isinstance(item['id'], str) or not re.fullmatch(r'auth_[a-f0-9]{16}', item['id'])
                or item['id'] in seen or item['scope'] not in {'session', 'task'}
                or item['source'] not in {'cli', 'confirmation'}
                or (item['scope'] == 'task' and not isinstance(item['task_id'], str))
                or (item['scope'] == 'session' and item['task_id'] is not None)
                or not isinstance(item['created_at'], (int, float))
                or (item['revoked_at'] is not None and not isinstance(item['revoked_at'], (int, float)))):
            raise ValueError('授权记录无效或跨会话')
        seen.add(item['id'])
        for key in ('tools', 'paths', 'domains'):
            if not isinstance(item[key], list) or any(not isinstance(value, str) or not value for value in item[key]):
                raise ValueError('授权集合字段无效')
        if not item['tools'] or any(not Path(value).is_absolute() for value in item['paths']):
            raise ValueError('授权需要工具集合与规范绝对路径')
        if not isinstance(item['command_prefixes'], list) or any(
                not isinstance(prefix, list) or not prefix or any(not isinstance(part, str) for part in prefix)
                for prefix in item['command_prefixes']):
            raise ValueError('授权 command_prefixes 无效')
        validate_environment_mapping(item['env_from'])
        if validate_header_mapping(item['headers_from']) != item['headers_from']:
            raise ValueError('授权 HTTP 头映射未规范化')
        if address_ranges(item['address_ranges']) != item['address_ranges']:
            raise ValueError('授权地址范围未规范化')
        for domain in item['domains']:
            if domain_pattern(domain) != domain:
                raise ValueError('授权域名未规范化')


class AuthorizationService:
    def __init__(self, store):
        self.store = store

    def _task(self):
        from genesisai.core.memory.tasks import TaskService
        return TaskService(self.store).active_task()

    def _active(self, item):
        if item['session_id'] != self.store.id or item['revoked_at'] is not None:
            return False
        if item['scope'] == 'session':
            return True
        task = self._task()
        return bool(task and task['id'] == item['task_id'] and task['status'] not in TERMINAL)

    def list(self):
        return [{**deepcopy(item), 'active': self._active(item)} for item in self.store.record.authorizations]

    def grant(self, payload, *, source):
        allowed = {'scope', 'tools', 'paths', 'domains', 'address_ranges', 'env_from', 'headers_from', 'command_prefixes'}
        if not isinstance(payload, dict) or payload.keys() - allowed or source not in {'cli', 'confirmation'}:
            raise ToolError('validation_error', '授权只接受 scope/tools/paths/domains/address_ranges/env_from/headers_from/command_prefixes')
        scope, tools = payload.get('scope'), payload.get('tools')
        if scope not in {'session', 'task'} or not isinstance(tools, list) or not tools or any(
                not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', name) for name in tools):
            raise ToolError('validation_error', 'scope 须为 session/task，tools 须为明确工具名列表')
        task = self._task() if scope == 'task' else None
        if scope == 'task' and (not task or task['status'] in TERMINAL):
            raise ToolError('task_missing', '任务范围授权需要当前活动任务')
        paths, domains = payload.get('paths', []), payload.get('domains', [])
        if any(not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
               for values in (paths, domains)):
            raise ToolError('validation_error', 'paths/domains 必须为非空字符串列表')
        paths = [str((self.store.workspace / path).resolve()) for path in paths]
        prefixes = payload.get('command_prefixes', [])
        if not isinstance(prefixes, list):
            raise ToolError('validation_error', 'command_prefixes 必须为 argv 前缀列表')
        from genesisai.core.extensions.tools.shell.commands import resolve_command
        prefixes = [resolve_command(prefix, self.store.workspace) for prefix in prefixes]
        if 'run_commands' in tools and not prefixes:
            raise ToolError('validation_error', 'run_commands 范围授权须声明 command_prefixes；cwd 范围不是 OS 沙箱')
        item = dict(id=uid('auth'), session_id=self.store.id, scope=scope,
            task_id=task['id'] if task else None, tools=list(dict.fromkeys(tools)),
            paths=list(dict.fromkeys(paths)), domains=list(dict.fromkeys(map(domain_pattern, domains))),
            address_ranges=address_ranges(payload.get('address_ranges', [])),
            env_from=validate_environment_mapping(payload.get('env_from', {})),
            headers_from=validate_header_mapping(payload.get('headers_from', {})), command_prefixes=prefixes,
            source=source, created_at=time.time(), revoked_at=None)
        previous = deepcopy(self.store.record.authorizations)
        self.store.record.authorizations.append(item)
        try:
            self.store.commit()
        except BaseException:
            self.store.record.authorizations = previous
            raise
        return deepcopy(item)

    def revoke(self, identity):
        item = next((item for item in self.store.record.authorizations if item['id'] == identity), None)
        if item is None:
            raise ToolError('unknown_authorization', '本会话没有此授权')
        previous = item['revoked_at']
        if previous is None:
            item['revoked_at'] = time.time()
            try:
                self.store.commit()
            except BaseException:
                item['revoked_at'] = previous
                raise
        return deepcopy(item)

    def require_active(self, identity):
        item = next((item for item in self.store.record.authorizations if item['id'] == identity), None)
        if item is None or not self._active(item):
            raise ToolError('authorization_revoked', '范围授权已撤销或不再属于当前活动任务；禁止继续执行或应用待提交修改，已有副作用不会自动回滚')
        return item

    def match(self, descriptor, args, preview):
        if descriptor.spec.requires_acceptance or destructive(args):
            return None
        preview = preview if isinstance(preview, dict) else {}
        mappings, header_mappings = [], []
        def collect_mappings(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == 'env_from' and isinstance(child, dict):
                        mappings.extend(child.items())
                    elif key == 'headers_from' and isinstance(child, dict):
                        header_mappings.extend(validate_header_mapping(child).items())
                    elif isinstance(child, (dict, list)):
                        collect_mappings(child)
            elif isinstance(value, list):
                for child in value:
                    collect_mappings(child)
        collect_mappings(args)
        collect_mappings(preview)
        paths = []
        # Prepared absolute targets bind writes to the resources the user saw.
        for key in ('target', 'source', 'destination', 'workspace'):
            value = preview.get(key)
            if isinstance(value, str) and Path(value).is_absolute():
                paths.append(Path(value).resolve())
        def collect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {'path', 'cwd', 'source', 'destination', 'new_path'} and isinstance(child, str):
                        base = Path(self.store.record.output) if key in {'path', 'destination', 'new_path'} and descriptor.spec.path_scope == 'output' else self.store.workspace
                        paths.append((base / child).resolve())
                    elif isinstance(child, (dict, list)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(args)
        domains, unrestricted_network = [], False
        if descriptor.spec.network:
            if descriptor.spec.network_boundary == 'private':
                domains.append(urlsplit(args.get('url', '')).hostname or '')
            elif descriptor.name in {'fetch_web_content', 'fetch_private_resource', 'render_web_page'} and args.get('url'):
                domains.append(urlsplit(args['url']).hostname or '')
            elif descriptor.category == 'mcp' and preview.get('transport') == 'streamable_http':
                domains.append(urlsplit(preview.get('endpoint', '')).hostname or '')
            else:
                unrestricted_network = True
        command = preview.get('command') if descriptor.spec.permission == 'shell' else None
        for item in self.store.record.authorizations:
            if descriptor.name not in item['tools'] or not self._active(item):
                continue
            if descriptor.spec.network_boundary == 'private' and not ranges_within(
                    args.get('address_ranges', []), item['address_ranges']):
                continue
            if any(item['env_from'].get(key) != value for key, value in mappings):
                continue
            if any(item['headers_from'].get(key) != value for key, value in header_mappings):
                continue
            if descriptor.spec.path_scope != 'none' and not paths:
                continue
            if any(not any(path == Path(root) or path.is_relative_to(Path(root)) for root in item['paths']) for path in paths):
                continue
            if unrestricted_network and '*' not in item['domains']:
                continue
            if domains and (not all(domains) or any(not domain_matches(host, item['domains']) for host in domains)):
                continue
            if descriptor.spec.permission == 'shell' and descriptor.name == 'run_commands':
                if not isinstance(command, list) or not any(len(command) >= len(prefix)
                        and os.path.normcase(command[0]) == os.path.normcase(prefix[0])
                        and command[1:len(prefix)] == prefix[1:] for prefix in item['command_prefixes']):
                    continue
            elif descriptor.spec.permission == 'shell' and not (
                    descriptor.source == 'external' and preview.get('capability_id') and preview.get('digest')):
                continue  # An unknown shell adapter cannot claim a bounded command scope.
            return deepcopy(item)
        return None


def verify_job_authorization(info):
    identity = info.get('authorization_id')
    if not identity:
        return
    from types import SimpleNamespace
    from genesisai.core.persistence.session import Session
    from genesisai.core.persistence.database import Database
    state, _ = Session.inspect(info['state_root'], info['session_id'])
    store = SimpleNamespace(id=info['session_id'], root=Path(info['state_root']),
        workspace=Path(state['workspace']), record=SimpleNamespace(**state), database=Database(info['state_root']))
    AuthorizationService(store).require_active(identity)
