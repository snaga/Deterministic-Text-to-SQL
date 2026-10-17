"""Unit tests for schema metadata models and schema loader."""

from pathlib import Path

import pytest

from deterministic_text_to_sql.schema import (
    ColumnMetadata,
    ForeignKeyMetadata,
    SchemaError,
    SchemaLoader,
    SchemaMetadata,
    SchemaValidationError,
    TableMetadata,
    load_schema_from_dict,
    load_schema_from_json,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


class TestSchemaModels:
    """Tests for metadata models."""

    def test_column_metadata_initialization(self) -> None:
        col = ColumnMetadata(
            name="r_name",
            type="CHAR(25)",
            description="Region name",
            role="dimension",
            values=["ASIA", "EUROPE"],
        )
        assert col.name == "r_name"
        assert col.type == "CHAR(25)"
        assert col.description == "Region name"
        assert col.role == "dimension"
        assert col.agg is None
        assert col.values == ["ASIA", "EUROPE"]

    def test_foreign_key_metadata_single_and_composite(self) -> None:
        fk_single = ForeignKeyMetadata(
            column="n_regionkey",
            ref_table="region",
            ref_column="r_regionkey",
        )
        assert fk_single.column == "n_regionkey"
        assert fk_single.ref_table == "region"
        assert fk_single.ref_column == "r_regionkey"

        fk_composite = ForeignKeyMetadata(
            column=["l_partkey", "l_suppkey"],
            ref_table="partsupp",
            ref_column=["ps_partkey", "ps_suppkey"],
        )
        assert fk_composite.column == ["l_partkey", "l_suppkey"]
        assert fk_composite.ref_table == "partsupp"
        assert fk_composite.ref_column == ["ps_partkey", "ps_suppkey"]

    def test_table_metadata_get_column(self) -> None:
        col1 = ColumnMetadata(
            name="c_custkey",
            type="BIGINT",
            description="Customer ID",
            role="id",
        )
        col2 = ColumnMetadata(
            name="c_name",
            type="VARCHAR(25)",
            description="Customer name",
            role="dimension",
        )
        table = TableMetadata(
            name="customer",
            description="Customer table",
            aliases=["顧客", "カスタマー"],
            primary_key=["c_custkey"],
            columns=[col1, col2],
        )

        # Exact match
        assert table.get_column("c_custkey") == col1
        # Case-insensitive match
        assert table.get_column("C_NAME") == col2
        # Non-existent column
        assert table.get_column("unknown_col") is None

    def test_schema_metadata_get_table(self) -> None:
        table_region = TableMetadata(
            name="region",
            description="Region table",
            aliases=["地域", "エリア", "リージョン"],
        )
        table_nation = TableMetadata(
            name="nation",
            description="Nation table",
            aliases=["国", "国家"],
        )
        schema = SchemaMetadata(
            title="Test Schema",
            description="Schema for testing",
            tables=[table_region, table_nation],
        )

        # Exact table name
        assert schema.get_table("region") == table_region
        # Case-insensitive table name
        assert schema.get_table("NATION") == table_nation
        # Alias exact match
        assert schema.get_table("地域") == table_region
        # Alias case-insensitive match (if ascii)
        assert schema.get_table("エリア") == table_region
        # Unknown table
        assert schema.get_table("orders") is None


class TestSchemaLoader:
    """Tests for schema loading and validation."""

    def test_load_tpch_schema_fixture(self) -> None:
        assert FIXTURE_PATH.is_file(), f"Fixture not found at {FIXTURE_PATH}"
        schema = load_schema_from_json(FIXTURE_PATH)

        assert isinstance(schema, SchemaMetadata)
        assert schema.title == "TPC-H Schema Metadata for Jev Classification"
        assert len(schema.tables) == 8

        # Verify region table
        region = schema.get_table("region")
        assert region is not None
        assert region.name == "region"
        assert "地域" in region.aliases
        assert region.primary_key == ["r_regionkey"]

        r_name = region.get_column("r_name")
        assert r_name is not None
        assert r_name.role == "dimension"
        assert r_name.values == ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"]

        # Verify composite foreign keys in lineitem
        lineitem = schema.get_table("lineitem")
        assert lineitem is not None
        assert len(lineitem.foreign_keys) == 4

        composite_fk = [fk for fk in lineitem.foreign_keys if fk.ref_table == "partsupp"]
        assert len(composite_fk) == 1
        assert composite_fk[0].column == ["l_partkey", "l_suppkey"]
        assert composite_fk[0].ref_column == ["ps_partkey", "ps_suppkey"]

    def test_schema_loader_class_methods(self) -> None:
        schema = SchemaLoader.load_from_json(FIXTURE_PATH)
        assert len(schema.tables) == 8

        data = {"tables": [{"name": "test", "description": "test table"}]}
        schema_dict = SchemaLoader.load_from_dict(data)
        assert len(schema_dict.tables) == 1
        assert schema_dict.tables[0].name == "test"

    def test_file_not_found(self, tmp_path: Path) -> None:
        non_existent = tmp_path / "does_not_exist.json"
        with pytest.raises(SchemaValidationError, match="Schema file not found"):
            load_schema_from_json(non_existent)

    def test_path_is_directory(self, tmp_path: Path) -> None:
        with pytest.raises(SchemaValidationError, match="Schema path is not a file"):
            load_schema_from_json(tmp_path)

    def test_invalid_json(self, tmp_path: Path) -> None:
        bad_json = tmp_path / "broken.json"
        bad_json.write_text("{broken json", encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="Invalid JSON"):
            load_schema_from_json(bad_json)

    def test_load_from_dict_non_dict_input(self) -> None:
        with pytest.raises(SchemaValidationError, match="Expected dict"):
            load_schema_from_dict(["not a dict"])  # type: ignore[arg-type]

    def test_missing_required_fields_in_table(self) -> None:
        invalid_data = {
            "tables": [
                {
                    # "name" is missing
                    "description": "missing name",
                }
            ]
        }
        with pytest.raises(SchemaValidationError, match="Schema validation failed"):
            load_schema_from_dict(invalid_data)

    def test_invalid_column_role(self) -> None:
        invalid_data = {
            "tables": [
                {
                    "name": "sample",
                    "description": "sample table",
                    "columns": [
                        {
                            "name": "col1",
                            "type": "INT",
                            "description": "col desc",
                            "role": "unsupported_role",
                        }
                    ],
                }
            ]
        }
        with pytest.raises(SchemaValidationError, match="Schema validation failed"):
            load_schema_from_dict(invalid_data)

    def test_schema_error_inheritance(self) -> None:
        assert issubclass(SchemaValidationError, SchemaError)
        assert issubclass(SchemaError, Exception)
