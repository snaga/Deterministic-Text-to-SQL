"""Schema loader for reading and validating semantic schema metadata."""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from deterministic_text_to_sql.schema.exceptions import SchemaValidationError
from deterministic_text_to_sql.schema.models import SchemaMetadata


def load_schema_from_dict(data: dict[str, Any]) -> SchemaMetadata:
    """Validate and load semantic schema metadata from a dictionary.

    Args:
        data: Raw schema dictionary.

    Returns:
        Validated SchemaMetadata instance.

    Raises:
        SchemaValidationError: If data is not a dict or validation fails.
    """
    if not isinstance(data, dict):
        raise SchemaValidationError(f"Expected dict, got {type(data).__name__}")

    try:
        return SchemaMetadata.model_validate(data)
    except ValidationError as e:
        raise SchemaValidationError(f"Schema validation failed: {e}") from e


def load_schema_from_json(path: Path | str) -> SchemaMetadata:
    """Read, parse, and validate semantic schema metadata from a JSON file.

    Args:
        path: Path to the JSON schema file.

    Returns:
        Validated SchemaMetadata instance.

    Raises:
        SchemaValidationError: If file does not exist, is invalid JSON,
            or fails schema validation.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise SchemaValidationError(f"Schema file not found: {file_path}")
    if not file_path.is_file():
        raise SchemaValidationError(f"Schema path is not a file: {file_path}")

    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as e:
        raise SchemaValidationError(f"Failed to read schema file '{file_path}': {e}") from e

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise SchemaValidationError(f"Invalid JSON in schema file '{file_path}': {e}") from e

    return load_schema_from_dict(data)


class SchemaLoader:
    """Utility class for loading and parsing schema metadata."""

    @staticmethod
    def load_from_json(path: Path | str) -> SchemaMetadata:
        """Load schema metadata from a JSON file.

        Args:
            path: Path to the JSON schema file.

        Returns:
            Validated SchemaMetadata instance.
        """
        return load_schema_from_json(path)

    @staticmethod
    def load_from_dict(data: dict[str, Any]) -> SchemaMetadata:
        """Load schema metadata from a dictionary.

        Args:
            data: Raw schema dictionary.

        Returns:
            Validated SchemaMetadata instance.
        """
        return load_schema_from_dict(data)
