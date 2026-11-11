"""Minimal, dependency-free JSON-Schema-subset validator for request intake.

Supported keywords: type, required, properties, additionalProperties (bool),
minimum, maximum, exclusiveMinimum, exclusiveMaximum, minLength, maxLength,
pattern, enum, items, minItems, maxItems, format (date).

Returns *all* errors (not just the first) so clients can fix a payload in one
round trip.
"""

import datetime
import re

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    # bool is a subclass of int in Python; exclude it explicitly
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "null": lambda v: v is None,
}

SUPPORTED_KEYWORDS = {
    "type", "required", "properties", "additionalProperties", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "pattern",
    "enum", "items", "minItems", "maxItems", "format", "description",
}


def validate(schema, value, path="$"):
    errors = []
    _validate(schema, value, path, errors)
    return errors


def _validate(schema, value, path, errors):
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_TYPES[t](value) for t in types):
            errors.append(f"{path}: expected {'/'.join(types)}, got {_type_name(value)}")
            return  # further checks are meaningless on the wrong type

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}, got {value!r}")

    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: must be >= {schema['minimum']}, got {value}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: must be <= {schema['maximum']}, got {value}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be > {schema['exclusiveMinimum']}, got {value}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: must be < {schema['exclusiveMaximum']}, got {value}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: length must be >= {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: length must be <= {schema['maxLength']}")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errors.append(f"{path}: does not match pattern {schema['pattern']}")
        if schema.get("format") == "date":
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path}: not a valid ISO date (YYYY-MM-DD)")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}.{name}: is required")
        props = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                _validate(sub, value[name], f"{path}.{name}", errors)
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append(f"{path}.{name}: unexpected property")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: must have >= {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: must have <= {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(schema["items"], item, f"{path}[{i}]", errors)


def check_schema(schema, path="input_schema"):
    """Validate the *schema itself* at config-load time (fail fast on typos)."""
    errors = []
    if not isinstance(schema, dict):
        return [f"{path}: must be an object"]
    for key in schema:
        if key not in SUPPORTED_KEYWORDS:
            errors.append(f"{path}: unsupported keyword '{key}'")
    t = schema.get("type")
    for tt in (t if isinstance(t, list) else [t] if t else []):
        if tt not in _TYPES:
            errors.append(f"{path}: unknown type '{tt}'")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            errors.append(f"{path}: invalid pattern ({exc})")
    for name, sub in schema.get("properties", {}).items():
        errors.extend(check_schema(sub, f"{path}.properties.{name}"))
    if "items" in schema:
        errors.extend(check_schema(schema["items"], f"{path}.items"))
    return errors


def _type_name(v):
    for name in ("boolean", "integer", "number", "string", "array", "object", "null"):
        if _TYPES[name](v):
            return name
    return type(v).__name__
