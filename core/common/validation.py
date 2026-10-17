"""Small JSON Schema validator for the schema keywords used by adapters.

The project keeps adapter schemas as the source of truth and has no third-party
runtime dependencies. This module implements the JSON Schema subset currently
used by the project and fails explicitly when a schema uses an unsupported
keyword.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


class SchemaDefinitionError(ValueError):
    """Raised when an adapter schema uses an unsupported or invalid feature."""


class ValidationIssue:
    """One schema violation without including the rejected data value."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.message = message

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


_SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "$comment",
    "$defs",
    "$ref",
    "title",
    "description",
    "default",
    "examples",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "minProperties",
    "maxProperties",
    "anyOf",
    "oneOf",
    "allOf",
    "if",
    "then",
    "else",
    "format",
}

_RFC3339_DATE_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


def validate_json_schema(instance: Any, schema: dict[str, Any]) -> list[ValidationIssue]:
    """Return validation issues for a JSON value and a local JSON Schema."""
    if not isinstance(schema, dict):
        raise SchemaDefinitionError("schema root must be a JSON object")

    issues: list[ValidationIssue] = []
    _validate(instance, schema, schema, "$", issues, 0)
    return issues


def _validate(
    instance: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
    issues: list[ValidationIssue],
    depth: int,
) -> None:
    if depth > 100:
        raise SchemaDefinitionError("schema reference depth exceeded")
    if not isinstance(schema, dict):
        raise SchemaDefinitionError(f"schema at {path} must be a JSON object")

    unsupported = sorted(set(schema) - _SUPPORTED_KEYWORDS)
    if unsupported:
        raise SchemaDefinitionError(
            f"unsupported JSON Schema keyword: {unsupported[0]}"
        )
    format_name = schema.get("format")
    if format_name is not None and format_name != "date-time":
        raise SchemaDefinitionError(f"unsupported string format: {format_name}")

    reference = schema.get("$ref")
    if reference is not None:
        target = _resolve_reference(root_schema, reference)
        _validate(instance, target, root_schema, path, issues, depth + 1)

    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(instance, expected_type):
        issues.append(
            ValidationIssue(path, f"expected type {expected_type}")
        )
        return

    if "const" in schema and not _json_equal(instance, schema["const"]):
        issues.append(ValidationIssue(path, "value does not match the required constant"))

    if "enum" in schema and not any(
        _json_equal(instance, candidate) for candidate in schema["enum"]
    ):
        issues.append(ValidationIssue(path, "value is not in the allowed set"))

    if "allOf" in schema:
        for subschema in schema["allOf"]:
            _validate(instance, subschema, root_schema, path, issues, depth + 1)

    if "anyOf" in schema:
        matches = 0
        for subschema in schema["anyOf"]:
            branch_issues: list[ValidationIssue] = []
            _validate(
                instance, subschema, root_schema, path, branch_issues, depth + 1
            )
            if not branch_issues:
                matches += 1
        if matches == 0:
            issues.append(
                ValidationIssue(path, "value must match at least one schema")
            )

    if "oneOf" in schema:
        matches = 0
        for subschema in schema["oneOf"]:
            branch_issues: list[ValidationIssue] = []
            _validate(
                instance, subschema, root_schema, path, branch_issues, depth + 1
            )
            if not branch_issues:
                matches += 1
        if matches != 1:
            issues.append(
                ValidationIssue(path, f"value must match exactly one schema (matched {matches})")
            )

    if "if" in schema:
        condition_issues: list[ValidationIssue] = []
        _validate(
            instance, schema["if"], root_schema, path, condition_issues, depth + 1
        )
        branch = schema.get("then") if not condition_issues else schema.get("else")
        if branch is not None:
            _validate(instance, branch, root_schema, path, issues, depth + 1)

    if isinstance(instance, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                issues.append(
                    ValidationIssue(path, f"missing required property {key!r}")
                )

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaDefinitionError(f"properties at {path} must be an object")

        for key, subschema in properties.items():
            if key in instance:
                _validate(
                    instance[key],
                    subschema,
                    root_schema,
                    _child_path(path, key),
                    issues,
                    depth + 1,
                )

        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                continue
            if additional is False:
                issues.append(
                    ValidationIssue(_child_path(path, key), "additional property is not allowed")
                )
            elif isinstance(additional, dict):
                _validate(
                    value,
                    additional,
                    root_schema,
                    _child_path(path, key),
                    issues,
                    depth + 1,
                )

        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            issues.append(ValidationIssue(path, "object has too few properties"))
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            issues.append(ValidationIssue(path, "object has too many properties"))

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(instance):
                _validate(
                    value,
                    item_schema,
                    root_schema,
                    f"{path}[{index}]",
                    issues,
                    depth + 1,
                )
        elif isinstance(item_schema, list):
            for index, subschema in enumerate(item_schema):
                if index < len(instance):
                    _validate(
                        instance[index],
                        subschema,
                        root_schema,
                        f"{path}[{index}]",
                        issues,
                        depth + 1,
                    )

        if "minItems" in schema and len(instance) < schema["minItems"]:
            issues.append(ValidationIssue(path, "array has too few items"))
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            issues.append(ValidationIssue(path, "array has too many items"))
        if schema.get("uniqueItems") and not _has_unique_json_values(instance):
            issues.append(ValidationIssue(path, "array items must be unique"))

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            issues.append(ValidationIssue(path, "string is too short"))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            issues.append(ValidationIssue(path, "string is too long"))
        if schema.get("format") == "date-time" and not _is_rfc3339_date_time(instance):
            issues.append(ValidationIssue(path, "string is not a valid RFC 3339 date-time"))
        if "pattern" in schema:
            try:
                matched = re.search(schema["pattern"], instance) is not None
            except re.error as error:
                raise SchemaDefinitionError("schema contains an invalid regular expression") from error
            if not matched:
                issues.append(ValidationIssue(path, "string does not match the required pattern"))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            issues.append(ValidationIssue(path, "number is below the minimum"))
        if "maximum" in schema and instance > schema["maximum"]:
            issues.append(ValidationIssue(path, "number is above the maximum"))

    if len(issues) > 50:
        del issues[50:]


def _matches_type(instance: Any, expected: str | list[str]) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for candidate in expected_types:
        if candidate == "object" and isinstance(instance, dict):
            return True
        if candidate == "array" and isinstance(instance, list):
            return True
        if candidate == "string" and isinstance(instance, str):
            return True
        if candidate == "boolean" and isinstance(instance, bool):
            return True
        if candidate == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
            return True
        if candidate == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
            return True
        if candidate == "null" and instance is None:
            return True
    return False


def _resolve_reference(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise SchemaDefinitionError("only local JSON Schema references are supported")

    target: Any = root_schema
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(target, dict) or key not in target:
            raise SchemaDefinitionError(f"unresolved local schema reference: {reference}")
        target = target[key]
    if not isinstance(target, dict):
        raise SchemaDefinitionError(f"schema reference is not an object: {reference}")
    return target


def _child_path(parent: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return type(left) is type(right) and left == right
    return type(left) is type(right) and left == right


def _is_rfc3339_date_time(value: str) -> bool:
    if not _RFC3339_DATE_TIME.fullmatch(value):
        return False
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _has_unique_json_values(values: list[Any]) -> bool:
    serialized = [
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        for value in values
    ]
    return len(serialized) == len(set(serialized))
