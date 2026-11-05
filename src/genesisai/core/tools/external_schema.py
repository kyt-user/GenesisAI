"""Validate the server schema verbatim. Unregistered external references cannot perform I/O."""

import json
from functools import lru_cache

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from referencing import Registry

from .base import SchemaConfigError


def validator(schema):
    return _compile(json.dumps(schema, sort_keys=True))


@lru_cache(maxsize=128)
def _compile(encoded):
    schema = json.loads(encoded)
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise SchemaConfigError("MCP inputSchema 必须是对象 schema")
    cls = validator_for(schema)
    try:
        cls.check_schema(schema)
    except SchemaError as exc:
        raise SchemaConfigError(exc.message) from exc
    return cls(schema, registry=Registry())
