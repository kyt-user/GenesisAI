"""HTTP header authority is distinct from process environment authority."""
from types import SimpleNamespace

import pytest

from genesisai.core.tools.authorization import AuthorizationService
from genesisai.core.tools.base import ToolDescriptor, ToolSpec, object_schema
from genesisai.core.tools.permissions import PermissionPolicy
from genesisai.shared.security import ToolError


def test_http_header_mapping_scope_is_case_insensitive_and_separate(tmp_path):
    store = SimpleNamespace(id='session_headers', workspace=tmp_path,
        record=SimpleNamespace(authorizations=[]), commit=lambda: None)
    authorizations = AuthorizationService(store)
    descriptor = ToolDescriptor('mcp_example_read', True, 'mcp', 'http mcp', tmp_path,
        ToolSpec(parameters=object_schema([]), permission='network', network=True), source='external')
    policy = PermissionPolicy('user', authorizations)
    preview = {'transport': 'streamable_http', 'endpoint': 'https://api.corp.test/mcp',
               'headers_from': {'X-Api-Key': 'SERVICE_SECRET'}}
    assert policy.requires_confirmation(descriptor, {}, preview)
    item = authorizations.grant({'scope': 'session', 'tools': ['mcp_example_read'],
        'domains': ['api.corp.test'], 'headers_from': {'X-API-KEY': 'SERVICE_SECRET'}}, source='cli')
    assert item['headers_from'] == {'x-api-key': 'SERVICE_SECRET'}
    assert item['env_from'] == {}
    assert not policy.requires_confirmation(descriptor, {}, preview)
    assert policy.requires_confirmation(descriptor, {}, {**preview,
        'headers_from': {'X-API-KEY': 'DIFFERENT_SECRET'}})
    assert policy.requires_confirmation(descriptor, {}, {**preview,
        'env_from': {'PROCESS_KEY': 'SERVICE_SECRET'}})
    with pytest.raises(ToolError, match='env_from'):
        authorizations.grant({'scope': 'session', 'tools': ['mcp_example_read'],
            'env_from': {'X-API-KEY': 'SERVICE_SECRET'}}, source='cli')


@pytest.mark.parametrize('mapping', [
    {'X-Key': 'SOURCE', 'x-key': 'SOURCE'}, {'X-Key\nInjected': 'SOURCE'}, {'X-Key': 'INVALID-NAME'},
])
def test_header_scope_rejects_invalid_names_and_ambiguous_case(tmp_path, mapping):
    store = SimpleNamespace(id='session_headers', workspace=tmp_path,
        record=SimpleNamespace(authorizations=[]), commit=lambda: None)
    with pytest.raises(ToolError):
        AuthorizationService(store).grant({'scope': 'session', 'tools': ['mcp_example_read'],
            'headers_from': mapping}, source='cli')
    assert store.record.authorizations == []
