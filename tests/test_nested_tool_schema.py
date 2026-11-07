import pytest

from genesisai.core.tools.base import Tool, SchemaValidationError, SchemaConfigError, object_schema
from genesisai.core.extensions.tools.kernel.fetch_web_content.spec import SPEC as WEB
from genesisai.core.extensions.tools.kernel.run_commands.spec import SPEC as COMMAND


def tool(schema):
    return Tool(name='fixture', description='', parameters=schema)


def test_web_field_enum_is_checked_inside_array():
    with pytest.raises(SchemaValidationError, match=r'fields\[0\]'):
        tool(WEB.parameters)._validate_args({'url': 'https://example.com', 'fields': ['Bogus']})


@pytest.mark.parametrize('interaction', [{'mode': 'unknown'}, {'mode': 'pipe', 'unrecognized': 1}, {'mode': 'pty', 'rows': True}])
def test_interaction_object_is_validated_before_execution(interaction):
    with pytest.raises(SchemaValidationError):
        tool(COMMAND.parameters)._validate_args({'command': ['python'], 'interaction': interaction})


def test_mapped_environment_values_must_be_strings():
    with pytest.raises(SchemaValidationError):
        tool(COMMAND.parameters)._validate_args({'command': ['python'], 'env_from': {'TARGET': False}})


@pytest.mark.parametrize('values', [[True], [0], [float('nan')]])
def test_array_items_follow_their_complete_numeric_schema(values):
    schema = object_schema(['values'], values={'type': 'array', 'items': {'type': 'integer', 'minimum': 1}})
    with pytest.raises(SchemaValidationError):
        tool(schema)._validate_args({'values': values})


def test_nested_schema_mistakes_fail_at_registration():
    schema = object_schema([], options={'type': 'object', 'properties': {'count': {'type': 'integer', 'minimum': 'bad'}}})
    with pytest.raises(SchemaConfigError):
        tool(schema)._validate_schema()


def test_validation_diagnostic_does_not_echo_rejected_input():
    schema = object_schema([], mode={'type': 'string', 'enum': ['public']})
    with pytest.raises(SchemaValidationError) as error:
        tool(schema)._validate_args({'mode': 'synthetic-private-value'})
    assert 'synthetic-private-value' not in str(error.value)
