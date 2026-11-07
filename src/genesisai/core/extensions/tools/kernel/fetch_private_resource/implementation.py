"""Private resources use their own explicitly authorized transport."""
from dataclasses import asdict

from genesisai.core.extensions.tools.kernel.fetch_web_content.implementation import Implementation as PublicReader
from genesisai.core.extensions.tools.web.budget import active_limits, remaining
from genesisai.core.extensions.tools.web.private_network import (
    PrivateNetwork, address_ranges, canonicalize_private_url, private_permission,
)


class Implementation(PublicReader):
    name = 'fetch_private_resource'
    source_kind = 'private_web'
    canonicalize = staticmethod(canonicalize_private_url)

    def prepare(self, context, args):
        preview = super().prepare(context, args)
        return {**preview, 'target': self.canonicalize(args['url']),
                'address_ranges': address_ranges(args['address_ranges']), 'network_boundary': 'private',
                'redirect_policy': '每跳域名匹配授权且全部解析IP落在请求CIDR；具体确认仅允许初始域名'}

    def execute(self, context, args):
        # Permission is checked before reading even a previously cached private document.
        self.permit = private_permission(self, context, args)
        result = super().execute(context, args)
        if result.get('read_more'):
            result['read_more']['address_ranges'] = args['address_ranges']
        return result

    def cache_key(self):
        from genesisai.shared.security import digest
        return digest(self.permit)

    def fetch_source(self, context, url, args):
        return PrivateNetwork(permit=self.permit, limits={**asdict(active_limits.get()),
                              'total_seconds': remaining()}).fetch(url)
