"""Apache Ossie semantic model adapter for SchemaMetadata conversion."""

import json
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from deterministic_text_to_sql.schema.exceptions import SchemaValidationError
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    ColumnRole,
    DimensionMetadata,
    ForeignKeyMetadata,
    HierarchyRule,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
)

VALID_ROLES: set[str] = {
    "id",
    "dimension",
    "metric",
    "fk",
    "attribute",
    "filter",
    "date_filter",
}

TIME_GRANULARITY_MAP: dict[str, dict[str, Any]] = {
    "year": {
        "base_synonyms": ["年", "年別"],
        "extra_synonyms": ["年度"],
        "suffix": "年",
    },
    "month": {
        "base_synonyms": ["月", "月別"],
        "extra_synonyms": [],
        "suffix": "月",
    },
    "day": {
        "base_synonyms": ["日", "日別"],
        "extra_synonyms": [],
        "suffix": "日",
    },
    "quarter": {
        "base_synonyms": ["四半期", "四半期別"],
        "extra_synonyms": [],
        "suffix": "四半期",
    },
    "week": {
        "base_synonyms": ["週", "週別"],
        "extra_synonyms": [],
        "suffix": "週",
    },
    "hour": {
        "base_synonyms": ["時", "時間別"],
        "extra_synonyms": [],
        "suffix": "時",
    },
}


class OssieSchemaAdapter:
    """Adapter for converting Apache Ossie semantic models to SchemaMetadata."""

    @classmethod
    def load_from_yaml(cls, path: Path | str) -> SchemaMetadata:
        """Load and convert an Apache Ossie YAML semantic model file.

        Args:
            path: File path to YAML file.

        Returns:
            Validated SchemaMetadata instance.

        Raises:
            SchemaValidationError: If file does not exist, cannot be read,
                contains invalid YAML syntax, or validation fails.
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
            data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise SchemaValidationError(f"Invalid YAML in schema file '{file_path}': {e}") from e

        if not isinstance(data, dict):
            raise SchemaValidationError(
                f"Expected YAML mapping at root in '{file_path}', got {type(data).__name__}"
            )

        return cls.load_from_dict(data)

    @classmethod
    def load_from_json(cls, path: Path | str) -> SchemaMetadata:
        """Load and convert an Apache Ossie JSON semantic model file.

        Args:
            path: File path to JSON file.

        Returns:
            Validated SchemaMetadata instance.

        Raises:
            SchemaValidationError: If file does not exist, cannot be read,
                contains invalid JSON syntax, or validation fails.
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

        if not isinstance(data, dict):
            raise SchemaValidationError(
                f"Expected JSON object at root in '{file_path}', got {type(data).__name__}"
            )

        return cls.load_from_dict(data)

    @classmethod
    def load_from_dict(cls, data: dict[str, Any]) -> SchemaMetadata:
        """Convert an Apache Ossie semantic model dictionary to SchemaMetadata.

        Args:
            data: Parsed dictionary conforming to Ossie semantic model.

        Returns:
            Validated SchemaMetadata instance.

        Raises:
            SchemaValidationError: If dictionary structure fails validation.
        """
        if not isinstance(data, dict):
            raise SchemaValidationError(f"Expected dict, got {type(data).__name__}")

        try:
            title = data.get("name") or data.get("title")
            description = data.get("description")

            # 1. Parse datasets into TableMetadata & time dimensions
            tables: list[TableMetadata] = []
            dimensions: list[DimensionMetadata] = []
            dataset_list = data.get("datasets", [])
            if not isinstance(dataset_list, list):
                raise SchemaValidationError(
                    f"Expected list for 'datasets', got {type(dataset_list).__name__}"
                )

            for ds in dataset_list:
                if not isinstance(ds, dict):
                    raise SchemaValidationError(
                        f"Expected dict for dataset entry, got {type(ds).__name__}"
                    )
                table = cls._parse_dataset(ds)
                tables.append(table)
                cls._extract_time_dimensions(ds, table, dimensions)

            # 2. Parse relationships into TableMetadata.foreign_keys
            relationships_list = data.get("relationships", [])
            if isinstance(relationships_list, list):
                cls._apply_relationships(tables, relationships_list)

            # 3. Parse metrics into MetricDefinition
            metrics: list[MetricDefinition] = []
            metrics_list = data.get("metrics", [])
            if isinstance(metrics_list, list):
                for m in metrics_list:
                    if isinstance(m, dict):
                        metrics.append(cls._parse_metric(m))

            # 4. Parse custom_extensions into HierarchyRule & DimensionMetadata
            hierarchies: list[HierarchyRule] = []
            custom_extensions = data.get("custom_extensions", [])
            if isinstance(custom_extensions, list):
                for ext in custom_extensions:
                    if (
                        isinstance(ext, dict)
                        and ext.get("vendor_name") == "DETERMINISTIC_TEXT_TO_SQL"
                    ):
                        ext_data = ext.get("data", {})
                        if isinstance(ext_data, dict):
                            raw_hierarchies = ext_data.get("hierarchies", [])
                            if isinstance(raw_hierarchies, list):
                                for h in raw_hierarchies:
                                    if isinstance(h, dict):
                                        hierarchies.append(HierarchyRule.model_validate(h))
                            raw_dimensions = ext_data.get("dimensions", [])
                            if isinstance(raw_dimensions, list):
                                for d in raw_dimensions:
                                    if isinstance(d, dict):
                                        cls._upsert_dimension(
                                            dimensions, DimensionMetadata.model_validate(d)
                                        )

            # Optional top-level dimensions
            top_dims = data.get("dimensions", [])
            if isinstance(top_dims, list):
                for d in top_dims:
                    if isinstance(d, dict):
                        cls._upsert_dimension(dimensions, DimensionMetadata.model_validate(d))

            return SchemaMetadata(
                title=title,
                description=description,
                tables=tables,
                metrics=metrics,
                hierarchies=hierarchies,
                dimensions=dimensions,
            )
        except ValidationError as e:
            raise SchemaValidationError(f"Schema validation failed: {e}") from e
        except Exception as e:
            if isinstance(e, SchemaValidationError):
                raise
            raise SchemaValidationError(f"Failed to adapt Ossie schema: {e}") from e

    @classmethod
    def _parse_dataset(cls, ds: dict[str, Any]) -> TableMetadata:
        """Parse a single Ossie dataset definition into TableMetadata."""
        ds_name = ds.get("name")
        source = ds.get("source", "")
        if not ds_name and not source:
            raise SchemaValidationError("Dataset must specify 'name' or 'source'")

        tbl_name = ds_name if ds_name else source.split(".")[-1]

        # Extract primary key
        pk_raw = ds.get("primary_key", [])
        if isinstance(pk_raw, str):
            primary_keys = [pk_raw]
        elif isinstance(pk_raw, list):
            primary_keys = [str(k) for k in pk_raw]
        else:
            primary_keys = []

        # Extract synonyms from ai_context
        synonyms: list[str] = []
        ai_ctx = ds.get("ai_context")
        if isinstance(ai_ctx, dict):
            raw_syns = ai_ctx.get("synonyms", [])
            if isinstance(raw_syns, list):
                synonyms = [str(s) for s in raw_syns]

        # Build aliases list
        aliases_set: set[str] = set(synonyms)
        if source:
            source_tail = source.split(".")[-1]
            aliases_set.add(source)
            aliases_set.add(source_tail)
            aliases_set.add(source_tail.lower())
        if tbl_name:
            aliases_set.add(tbl_name)
            aliases_set.add(tbl_name.lower())

        aliases = list(aliases_set)

        # Parse fields into ColumnMetadata
        columns: list[ColumnMetadata] = []
        fields_list = ds.get("fields", [])
        if not isinstance(fields_list, list):
            raise SchemaValidationError(
                f"Expected list for 'fields' in dataset '{tbl_name}', "
                f"got {type(fields_list).__name__}"
            )

        for f in fields_list:
            if not isinstance(f, dict):
                raise SchemaValidationError(
                    f"Expected dict for field in dataset '{tbl_name}', got {type(f).__name__}"
                )
            col_name = f.get("name")
            if not col_name:
                raise SchemaValidationError(f"Field in dataset '{tbl_name}' is missing 'name'")

            datatype = f.get("datatype") or "VARCHAR"
            description = f.get("description") or ""

            # Explicit role fallback to primary_key check
            raw_role = f.get("role")
            col_role: ColumnRole
            if raw_role and str(raw_role) in VALID_ROLES:
                col_role = cast(ColumnRole, raw_role)
            else:
                col_role = "id" if col_name in primary_keys else "dimension"

            # Column synonyms
            col_synonyms: list[str] = []
            col_ai_ctx = f.get("ai_context")
            if isinstance(col_ai_ctx, dict):
                raw_col_syns = col_ai_ctx.get("synonyms", [])
                if isinstance(raw_col_syns, list):
                    col_synonyms = [str(s) for s in raw_col_syns]

            col_meta = ColumnMetadata(
                name=col_name,
                type=datatype,
                description=description,
                role=col_role,
                agg=f.get("agg"),
                values=f.get("values"),
                synonyms=col_synonyms,
            )
            columns.append(col_meta)

        return TableMetadata(
            name=tbl_name,
            description=ds.get("description") or "",
            aliases=aliases,
            synonyms=synonyms,
            primary_key=primary_keys,
            foreign_keys=[],
            columns=columns,
        )

    @classmethod
    def _apply_relationships(
        cls,
        tables: list[TableMetadata],
        relationships_list: list[Any],
    ) -> None:
        """Parse relationships and append ForeignKeyMetadata to the 'from' table."""
        # Index tables by name and aliases for robust lookup
        table_lookup: dict[str, TableMetadata] = {}
        for tbl in tables:
            table_lookup[tbl.name] = tbl
            table_lookup[tbl.name.lower()] = tbl
            for alias in tbl.aliases:
                table_lookup[alias] = tbl
                table_lookup[alias.lower()] = tbl

        for rel in relationships_list:
            if not isinstance(rel, dict):
                continue

            from_name = rel.get("from")
            to_name = rel.get("to")
            from_cols_raw = rel.get("from_columns", [])
            to_cols_raw = rel.get("to_columns", [])

            if not from_name or not to_name:
                continue

            # Resolve from_table
            from_table = table_lookup.get(from_name) or table_lookup.get(from_name.lower())
            if not from_table:
                # If from_name cannot be resolved, cannot attach foreign key
                continue

            # Format from_columns: string if single, list if composite
            if isinstance(from_cols_raw, str):
                col_spec: str | list[str] = from_cols_raw
            elif isinstance(from_cols_raw, list):
                if len(from_cols_raw) == 1:
                    col_spec = str(from_cols_raw[0])
                else:
                    col_spec = [str(c) for c in from_cols_raw]
            else:
                continue

            # Format to_columns: string if single, list if composite
            if isinstance(to_cols_raw, str):
                ref_col_spec: str | list[str] = to_cols_raw
            elif isinstance(to_cols_raw, list):
                if len(to_cols_raw) == 1:
                    ref_col_spec = str(to_cols_raw[0])
                else:
                    ref_col_spec = [str(c) for c in to_cols_raw]
            else:
                continue

            # Target table name: prefer resolved target table name if found, else original to_name
            target_table = table_lookup.get(to_name) or table_lookup.get(to_name.lower())
            target_table_name = target_table.name if target_table else to_name

            fk = ForeignKeyMetadata(
                column=col_spec,
                ref_table=target_table_name,
                ref_column=ref_col_spec,
            )
            from_table.foreign_keys.append(fk)

    @classmethod
    def _parse_metric(cls, m: dict[str, Any]) -> MetricDefinition:
        """Parse an Ossie metric dictionary into MetricDefinition."""
        name = m.get("name")
        if not name:
            raise SchemaValidationError("Metric definition is missing 'name'")

        description = m.get("description") or ""
        datatype = m.get("datatype") or "DOUBLE"

        # Resolve expression (prefer ANSI_SQL dialect)
        expr_raw = m.get("expression")
        expr_str = ""
        if isinstance(expr_raw, str):
            expr_str = expr_raw
        elif isinstance(expr_raw, dict):
            dialects = expr_raw.get("dialects", [])
            if isinstance(dialects, list):
                ansi_expr: str | None = None
                fallback_expr: str | None = None
                for d in dialects:
                    if isinstance(d, dict):
                        d_name = str(d.get("dialect", "")).upper()
                        d_expr = str(d.get("expression", ""))
                        if fallback_expr is None:
                            fallback_expr = d_expr
                        if d_name == "ANSI_SQL":
                            ansi_expr = d_expr
                            break
                expr_str = ansi_expr if ansi_expr is not None else (fallback_expr or "")

        # Synonyms
        synonyms: list[str] = []
        ai_ctx = m.get("ai_context")
        if isinstance(ai_ctx, dict):
            raw_syns = ai_ctx.get("synonyms", [])
            if isinstance(raw_syns, list):
                synonyms = [str(s) for s in raw_syns]

        return MetricDefinition(
            name=name,
            expression=expr_str,
            description=description,
            datatype=datatype,
            synonyms=synonyms,
        )

    @classmethod
    def _extract_time_dimensions(
        cls,
        ds: dict[str, Any],
        table: TableMetadata,
        dimensions: list[DimensionMetadata],
    ) -> None:
        """Extract time-based DimensionMetadata from dataset fields.

        Args:
            ds: Raw dataset dictionary from Ossie schema.
            table: Parsed TableMetadata for this dataset.
            dimensions: Target list to append extracted DimensionMetadata.
        """
        fields_list = ds.get("fields", [])
        if not isinstance(fields_list, list):
            return

        for f in fields_list:
            if not isinstance(f, dict):
                continue
            col_name = f.get("name")
            if not col_name:
                continue

            dim_obj = f.get("dimension")
            is_time = (f.get("is_time") is True) or (
                isinstance(dim_obj, dict)
                and (dim_obj.get("is_time") is True or dim_obj.get("type") in ("time", "date"))
            )

            # Extract granularities
            raw_grans: Any = f.get("time_granularities")
            if not raw_grans and isinstance(dim_obj, dict):
                raw_grans = dim_obj.get("time_granularities") or dim_obj.get("type_params", {}).get(
                    "time_granularities"
                )

            granularities: list[str] = []
            if isinstance(raw_grans, list):
                granularities = [str(g).strip().lower() for g in raw_grans if str(g).strip()]

            # If either is_time is True or time_granularities list exists
            if (is_time or bool(granularities)) and granularities:
                col_meta = table.get_column(col_name)
                col_synonyms = col_meta.synonyms if col_meta else []
                col_desc = f.get("description") or (col_meta.description if col_meta else "")

                for gran in granularities:
                    dim_name = f"{table.name}_{gran}"
                    if any(d.name == dim_name for d in dimensions):
                        dim_name = f"{table.name}_{col_name}_{gran}"

                    expression = f"EXTRACT({gran} FROM {table.name}.{col_name})"
                    synonyms = cls._generate_time_dimension_synonyms(
                        granularity=gran,
                        table_synonyms=table.synonyms,
                        col_synonyms=col_synonyms,
                        col_description=col_desc,
                    )
                    description = (
                        f"{col_desc} ({gran})"
                        if col_desc
                        else f"{table.name}.{col_name} based {gran} dimension"
                    )

                    dim_meta = DimensionMetadata(
                        name=dim_name,
                        expression=expression,
                        description=description,
                        synonyms=synonyms,
                        is_time=True,
                        time_granularity=gran,
                    )
                    cls._upsert_dimension(dimensions, dim_meta)

    @classmethod
    def _upsert_dimension(
        cls,
        dimensions: list[DimensionMetadata],
        new_dim: DimensionMetadata,
    ) -> None:
        """Insert or replace DimensionMetadata by name in list."""
        for i, existing in enumerate(dimensions):
            if existing.name == new_dim.name:
                dimensions[i] = new_dim
                return
        dimensions.append(new_dim)

    @staticmethod
    def _generate_time_dimension_synonyms(
        granularity: str,
        table_synonyms: list[str],
        col_synonyms: list[str],
        col_description: str = "",
    ) -> list[str]:
        """Generate natural language synonyms for a time dimension.

        Args:
            granularity: Time granularity (e.g. 'year', 'month').
            table_synonyms: Synonyms of the parent table.
            col_synonyms: Synonyms of the source column.
            col_description: Logical description of the source column.

        Returns:
            List of generated natural language synonyms.
        """
        gran_lower = granularity.lower()
        config = TIME_GRANULARITY_MAP.get(
            gran_lower,
            {"base_synonyms": [gran_lower], "extra_synonyms": [], "suffix": gran_lower},
        )

        synonyms: list[str] = list(config["base_synonyms"])

        base_terms: list[str] = []
        # 1. Extract base terms from column synonyms (e.g. "注文日" -> "注文")
        time_suffixes = ["日付", "日時", "日", "時期"]
        for s in col_synonyms:
            for suffix_to_strip in time_suffixes:
                if s.endswith(suffix_to_strip) and len(s) > len(suffix_to_strip):
                    cand = s[: -len(suffix_to_strip)]
                    if cand not in base_terms:
                        base_terms.append(cand)
            if s not in base_terms and not any(s.endswith(x) for x in time_suffixes):
                base_terms.append(s)

        # 2. Extract base terms from table synonyms (e.g. "注文", "オーダー")
        for ts in table_synonyms:
            if ts not in base_terms:
                base_terms.append(ts)

        # 3. Extract base terms from column description (e.g. "注文日" -> "注文")
        if col_description:
            for suffix_to_strip in ["日付", "日時", "日", "時期"]:
                if col_description.endswith(suffix_to_strip) and len(col_description) > len(
                    suffix_to_strip
                ):
                    cand = col_description[: -len(suffix_to_strip)]
                    if cand not in base_terms:
                        base_terms.append(cand)

        suffix = config["suffix"]
        for bt in base_terms:
            term = f"{bt}{suffix}"
            if term not in synonyms:
                synonyms.append(term)

        # 4. Append extra synonyms (e.g. "年度" for year)
        for extra in config["extra_synonyms"]:
            if extra not in synonyms:
                synonyms.append(extra)

        return synonyms
