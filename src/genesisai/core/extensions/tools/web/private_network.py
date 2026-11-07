"""Explicitly permitted private HTTP reads; public Network never uses this policy."""
import ipaddress
from urllib.parse import urlsplit, urlunsplit

from genesisai.core.tools.permissions import accepted_tool, current_authorization
from genesisai.shared.security import ToolError, digest
from .network import Network


def canonicalize_private_url(value):
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError('需要不含凭据的HTTP(S) URL')
        host = parsed.hostname.lower().rstrip('.')
        if any(character.isspace() or ord(character) < 32 for character in host):
            raise ValueError('域名包含空白或控制字符')
        try:
            address = ipaddress.ip_address(host)
            host = '[' + address.compressed + ']' if address.version == 6 else address.compressed
        except ValueError:
            host = host.encode('idna').decode()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('内网端口无效')
        default = 443 if parsed.scheme.lower() == 'https' else 80
        return urlunsplit((parsed.scheme.lower(), host + (':' + str(port) if port is not None and port != default else ''),
                           parsed.path or '/', parsed.query, ''))
    except (ValueError, UnicodeError) as exc:
        raise ToolError('unsafe_url', '内网读取URL无效：需要不含凭据的HTTP(S)地址和有效端口') from exc


def address_ranges(values):
    if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
        raise ToolError('validation_error', 'address_ranges需要明确的IP/CIDR列表')
    try:
        return list(dict.fromkeys(ipaddress.ip_network(value, strict=False).with_prefixlen for value in values))
    except ValueError as exc:
        raise ToolError('validation_error', 'address_ranges包含无效IP/CIDR') from exc


def private_permission(implementation, context, args):
    requested = [ipaddress.ip_network(value) for value in address_ranges(args['address_ranges'])]
    grant = current_authorization.get()
    if grant is not None:
        permitted = [ipaddress.ip_network(value) for value in address_ranges(grant.get('address_ranges'))]
        if not all(any(net.version == allowed.version and net.subnet_of(allowed) for allowed in permitted) for net in requested):
            raise ToolError('permission_denied', '请求地址范围超出当前内网授权')
        return {'domains': grant['domains'], 'address_ranges': [str(net) for net in requested]}
    confirmation = accepted_tool.get()
    preview = implementation.prepare(context, args)
    if not confirmation or confirmation.get('name') != implementation.name or confirmation.get('digest') != digest({**preview, 'args': args}):
        raise ToolError('permission_denied', '内网读取需要匹配的范围授权或具体资源确认')
    return {'domains': [urlsplit(canonicalize_private_url(args['url'])).hostname], 'address_ranges': [str(net) for net in requested]}


class PrivateNetwork(Network):
    canonicalize = staticmethod(canonicalize_private_url)
    address_failure = ('permission_denied', '解析地址超出当前CIDR许可，或属于禁止的特殊用途地址')

    def __init__(self, *, permit, resolver=None, transport=None, limits=None):
        self.ranges = [ipaddress.ip_network(value) for value in address_ranges(permit['address_ranges'])]
        super().__init__(resolver, transport, limits=limits, scope={'domains': permit['domains']})

    def allowed_address(self, value):
        address = ipaddress.ip_address(value)
        if address.is_link_local or address.is_multicast or address.is_unspecified or (address.is_reserved and not address.is_loopback):
            return False
        return any(address.version == network.version and address in network for network in self.ranges)
