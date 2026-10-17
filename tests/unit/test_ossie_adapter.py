"""Unit tests for Apache Ossie semantic model adapter (OssieSchemaAdapter)."""

import json
from pathlib import Path
from typing import Any

import pytest

from deterministic_text_to_sql.schema import (
    HierarchyRule,
    OssieSchemaAdapter,
    SchemaLoader,
    SchemaMetadata,
    SchemaValidationError,
    load_schema,
    load_schema_from_yaml,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FLIGHTS_OSSIE_YAML = (
    Path(__file__).resolve().parent.parent.parent
    / "doc_internal"
    / "ossie"
    / "examples"
    / "flights.semantic_model.yaml"
)


class TestOssieSchemaAdapterDictLoading:
    """Tests for loading and converting Ossie schema dictionaries."""

    def test_minimal_ossie_dict_conversion(self) -> None:
        """Verify minimal valid Ossie model dictionary maps to SchemaMetadata."""
        data: dict[str, Any] = {
            "version": "0.2.0.dev0",
            "name": "Minimal Model",
            "description": "Minimal test model",
            "datasets": [
                {
                    "name": "USERS",
                    "source": "DB.SCHEMA.USERS",
                    "description": "User accounts",
                    "primary_key": ["user_id"],
                    "ai_context": {
                        "synonyms": ["ユーザー", "アカウント", "user"],
                    },
                    "fields": [
                        {
                            "name": "user_id",
                            "datatype": "INTEGER",
                            "description": "User identifier",
                            "ai_context": {"synonyms": ["ユーザーID", "UID"]},
                        },
                        {
                            "name": "username",
                            "datatype": "VARCHAR",
                            "description": "User login name",
                            "ai_context": {"synonyms": ["ユーザー名"]},
                        },
                    ],
                }
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(data)

        assert isinstance(schema, SchemaMetadata)
        assert schema.title == "Minimal Model"
        assert schema.description == "Minimal test model"
        assert len(schema.tables) == 1

        tbl = schema.tables[0]
        assert tbl.name == "USERS"
        assert tbl.description == "User accounts"
        assert tbl.primary_key == ["user_id"]
        assert "ユーザー" in tbl.synonyms
        assert "アカウント" in tbl.synonyms
        assert "user" in tbl.aliases
        assert "users" in tbl.aliases

        assert len(tbl.columns) == 2
        col_id = tbl.columns[0]
        assert col_id.name == "user_id"
        assert col_id.type == "INTEGER"
        assert col_id.role == "id"
        assert "ユーザーID" in col_id.synonyms

        col_name = tbl.columns[1]
        assert col_name.name == "username"
        assert col_name.type == "VARCHAR"
        assert col_name.role == "dimension"
        assert "ユーザー名" in col_name.synonyms

    def test_single_and_composite_relationships_conversion(self) -> None:
        """Verify single and composite foreign key relationships mapping."""
        data: dict[str, Any] = {
            "name": "Relational Model",
            "datasets": [
                {
                    "name": "ORDERS",
                    "primary_key": ["order_id"],
                    "fields": [
                        {"name": "order_id", "datatype": "INTEGER"},
                        {"name": "customer_id", "datatype": "INTEGER"},
                    ],
                },
                {
                    "name": "CUSTOMERS",
                    "primary_key": ["cust_id"],
                    "fields": [{"name": "cust_id", "datatype": "INTEGER"}],
                },
                {
                    "name": "LINEITEM",
                    "primary_key": ["order_id", "line_nr"],
                    "fields": [
                        {"name": "order_id", "datatype": "INTEGER"},
                        {"name": "line_nr", "datatype": "INTEGER"},
                        {"name": "part_id", "datatype": "INTEGER"},
                        {"name": "supp_id", "datatype": "INTEGER"},
                    ],
                },
                {
                    "name": "PARTSUPP",
                    "primary_key": ["ps_partkey", "ps_suppkey"],
                    "fields": [
                        {"name": "ps_partkey", "datatype": "INTEGER"},
                        {"name": "ps_suppkey", "datatype": "INTEGER"},
                    ],
                },
            ],
            "relationships": [
                {
                    "name": "order_to_customer",
                    "from": "ORDERS",
                    "to": "CUSTOMERS",
                    "from_columns": ["customer_id"],
                    "to_columns": ["cust_id"],
                },
                {
                    "name": "lineitem_to_partsupp_composite",
                    "from": "LINEITEM",
                    "to": "PARTSUPP",
                    "from_columns": ["part_id", "supp_id"],
                    "to_columns": ["ps_partkey", "ps_suppkey"],
                },
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(data)

        order_tbl = schema.get_table("ORDERS")
        assert order_tbl is not None
        assert len(order_tbl.foreign_keys) == 1
        fk_order = order_tbl.foreign_keys[0]
        assert fk_order.column == "customer_id"
        assert fk_order.ref_table == "CUSTOMERS"
        assert fk_order.ref_column == "cust_id"

        lineitem_tbl = schema.get_table("LINEITEM")
        assert lineitem_tbl is not None
        assert len(lineitem_tbl.foreign_keys) == 1
        fk_comp = lineitem_tbl.foreign_keys[0]
        assert fk_comp.column == ["part_id", "supp_id"]
        assert fk_comp.ref_table == "PARTSUPP"
        assert fk_comp.ref_column == ["ps_partkey", "ps_suppkey"]

    def test_metrics_ansi_sql_and_synonyms_extraction(self) -> None:
        """Verify ANSI_SQL expression extraction and fallback for metrics."""
        data: dict[str, Any] = {
            "name": "Metric Model",
            "datasets": [
                {
                    "name": "SALES",
                    "fields": [{"name": "revenue", "datatype": "DOUBLE"}],
                }
            ],
            "metrics": [
                {
                    "name": "total_revenue",
                    "description": "Sum of all sales revenue",
                    "datatype": "DOUBLE",
                    "expression": {
                        "dialects": [
                            {"dialect": "SNOWFLAKE", "expression": "SUM(revenue::float)"},
                            {"dialect": "ANSI_SQL", "expression": "SUM(revenue)"},
                        ]
                    },
                    "ai_context": {"synonyms": ["売上合計", "総売上", "revenue"]},
                },
                {
                    "name": "raw_expr_metric",
                    "datatype": "BIGINT",
                    "expression": "COUNT(1)",
                    "ai_context": {"synonyms": ["件数", "total_count"]},
                },
                {
                    "name": "fallback_metric",
                    "expression": {
                        "dialects": [
                            {"dialect": "DUCKDB", "expression": "AVG(revenue)"},
                        ]
                    },
                },
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(data)
        assert len(schema.metrics) == 3

        m1 = schema.get_metric("total_revenue")
        assert m1 is not None
        assert m1.expression == "SUM(revenue)"
        assert m1.datatype == "DOUBLE"
        assert "売上合計" in m1.synonyms
        assert "総売上" in m1.synonyms

        m2 = schema.get_metric("raw_expr_metric")
        assert m2 is not None
        assert m2.expression == "COUNT(1)"
        assert m2.datatype == "BIGINT"
        assert "件数" in m2.synonyms

        m3 = schema.get_metric("fallback_metric")
        assert m3 is not None
        assert m3.expression == "AVG(revenue)"
        assert m3.datatype == "DOUBLE"

    def test_custom_extensions_hierarchy_rules_extraction(self) -> None:
        """Verify custom_extensions extraction into HierarchyRule list."""
        data: dict[str, Any] = {
            "name": "Hierarchy Model",
            "datasets": [
                {
                    "name": "nation",
                    "fields": [{"name": "n_name", "datatype": "VARCHAR"}],
                },
                {
                    "name": "region",
                    "fields": [{"name": "r_name", "datatype": "VARCHAR"}],
                },
            ],
            "custom_extensions": [
                {
                    "vendor_name": "OTHER_VENDOR",
                    "data": {"some_key": "some_value"},
                },
                {
                    "vendor_name": "DETERMINISTIC_TEXT_TO_SQL",
                    "data": {
                        "hierarchies": [
                            {
                                "parent_table": "region",
                                "parent_column": "r_name",
                                "child_table": "nation",
                                "child_column": "n_name",
                                "child_to_parent": {
                                    "JAPAN": "ASIA",
                                    "CHINA": "ASIA",
                                    "GERMANY": "EUROPE",
                                },
                            }
                        ]
                    },
                },
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(data)
        assert len(schema.hierarchies) == 1
        h = schema.hierarchies[0]
        assert isinstance(h, HierarchyRule)
        assert h.parent_table == "region"
        assert h.parent_column == "r_name"
        assert h.child_table == "nation"
        assert h.child_column == "n_name"
        assert h.child_to_parent["JAPAN"] == "ASIA"
        assert h.child_to_parent["GERMANY"] == "EUROPE"


class TestOssieSchemaAdapterFileLoading:
    """Tests for loading Ossie models from YAML and JSON files."""

    def test_load_from_yaml_file(self, tmp_path: Path) -> None:
        """Verify loading an Ossie YAML file."""
        yaml_content = """
version: 0.2.0.dev0
name: Test YAML Model
description: Loaded from yaml
datasets:
  - name: T1
    source: DB.SCHEMA.T1
    primary_key: [id]
    fields:
      - name: id
        datatype: INTEGER
"""
        yaml_file = tmp_path / "model.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        schema = OssieSchemaAdapter.load_from_yaml(yaml_file)
        assert schema.title == "Test YAML Model"
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "T1"

    def test_load_from_json_file(self, tmp_path: Path) -> None:
        """Verify loading an Ossie JSON file."""
        data = {
            "name": "Test JSON Model",
            "datasets": [
                {
                    "name": "T2",
                    "fields": [{"name": "col_a", "datatype": "VARCHAR"}],
                }
            ],
        }
        json_file = tmp_path / "model.json"
        json_file.write_text(json.dumps(data), encoding="utf-8")

        schema = OssieSchemaAdapter.load_from_json(json_file)
        assert schema.title == "Test JSON Model"
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "T2"

    def test_load_official_flights_semantic_model_yaml(self) -> None:
        """Verify loading the official flights.semantic_model.yaml example."""
        assert FLIGHTS_OSSIE_YAML.exists(), f"Missing example file: {FLIGHTS_OSSIE_YAML}"

        schema = OssieSchemaAdapter.load_from_yaml(FLIGHTS_OSSIE_YAML)

        assert isinstance(schema, SchemaMetadata)
        assert schema.title == "Flights semantic model"
        # Check datasets: RUNWAY, AIRCRAFT, AIRPORT, FLIGHT, CARRIER, ROUTE
        assert len(schema.tables) == 6

        runway = schema.get_table("RUNWAY")
        assert runway is not None
        assert runway.name == "RUNWAY"
        assert len(runway.columns) == 4
        assert runway.get_column("airport_code") is not None
        assert runway.get_column("length") is not None

        aircraft = schema.get_table("AIRCRAFT")
        assert aircraft is not None
        assert aircraft.get_column("serial_nr") is not None
        assert aircraft.get_column("nr_seats") is not None

        airport = schema.get_table("AIRPORT")
        assert airport is not None
        assert airport.get_column("code") is not None
        assert airport.get_column("city_nm") is not None

        flight = schema.get_table("FLIGHT")
        assert flight is not None
        assert flight.get_column("id") is not None
        assert flight.get_column("dep_delay") is not None
        assert flight.get_column("arr_delay") is not None

        carrier = schema.get_table("CARRIER")
        assert carrier is not None
        assert carrier.get_column("code") is not None
        assert carrier.get_column("name") is not None

        route = schema.get_table("ROUTE")
        assert route is not None
        assert route.get_column("orig_airport_code") is not None
        assert route.get_column("dest_airport_code") is not None


class TestOssieSchemaAdapterValidationAndErrors:
    """Tests for error handling and schema validation."""

    def test_non_existent_file_raises_schema_validation_error(self, tmp_path: Path) -> None:
        """Verify missing file raises SchemaValidationError."""
        missing_path = tmp_path / "non_existent.yaml"
        with pytest.raises(SchemaValidationError, match="Schema file not found"):
            OssieSchemaAdapter.load_from_yaml(missing_path)

        with pytest.raises(SchemaValidationError, match="Schema file not found"):
            OssieSchemaAdapter.load_from_json(tmp_path / "non_existent.json")

    def test_directory_path_raises_schema_validation_error(self, tmp_path: Path) -> None:
        """Verify directory path raises SchemaValidationError."""
        with pytest.raises(SchemaValidationError, match="Schema path is not a file"):
            OssieSchemaAdapter.load_from_yaml(tmp_path)

        with pytest.raises(SchemaValidationError, match="Schema path is not a file"):
            OssieSchemaAdapter.load_from_json(tmp_path)

    def test_invalid_yaml_syntax_raises_schema_validation_error(self, tmp_path: Path) -> None:
        """Verify broken YAML syntax raises SchemaValidationError."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("datasets: [unclosed list", encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="Invalid YAML"):
            OssieSchemaAdapter.load_from_yaml(bad_yaml)

    def test_invalid_json_syntax_raises_schema_validation_error(self, tmp_path: Path) -> None:
        """Verify broken JSON syntax raises SchemaValidationError."""
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{not a valid json", encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="Invalid JSON"):
            OssieSchemaAdapter.load_from_json(bad_json)

    def test_non_dict_root_raises_schema_validation_error(self, tmp_path: Path) -> None:
        """Verify root not being a dictionary raises SchemaValidationError."""
        list_yaml = tmp_path / "list.yaml"
        list_yaml.write_text("- item1\n- item2", encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="Expected YAML mapping at root"):
            OssieSchemaAdapter.load_from_yaml(list_yaml)

        list_json = tmp_path / "list.json"
        list_json.write_text("['item1']", encoding="utf-8")
        with pytest.raises(SchemaValidationError, match="Invalid JSON|Expected JSON object"):
            OssieSchemaAdapter.load_from_json(list_json)

        with pytest.raises(SchemaValidationError, match="Expected dict"):
            OssieSchemaAdapter.load_from_dict(["not a dict"])  # type: ignore[arg-type]

    def test_dataset_missing_name_and_source_raises_error(self) -> None:
        """Verify dataset without name or source raises SchemaValidationError."""
        data = {
            "datasets": [
                {
                    "description": "No name or source",
                    "fields": [{"name": "c1"}],
                }
            ]
        }
        with pytest.raises(SchemaValidationError, match="Dataset must specify 'name' or 'source'"):
            OssieSchemaAdapter.load_from_dict(data)

    def test_field_missing_name_raises_error(self) -> None:
        """Verify field without name raises SchemaValidationError."""
        data = {
            "datasets": [
                {
                    "name": "T1",
                    "fields": [{"datatype": "INTEGER"}],
                }
            ]
        }
        with pytest.raises(SchemaValidationError, match="missing 'name'"):
            OssieSchemaAdapter.load_from_dict(data)


class TestLoadSchemaAutomaticFormatDetection:
    """Tests for load_schema and SchemaLoader format dispatching."""

    def test_load_schema_yaml_and_yml_extensions(self, tmp_path: Path) -> None:
        """Verify .yaml and .yml files are parsed via OssieSchemaAdapter."""
        yaml_content = """
name: YAML Schema
datasets:
  - name: test_tbl
    fields:
      - name: col1
        datatype: TEXT
"""
        yaml_file = tmp_path / "s1.yaml"
        yml_file = tmp_path / "s2.yml"
        yaml_file.write_text(yaml_content, encoding="utf-8")
        yml_file.write_text(yaml_content, encoding="utf-8")

        s1 = load_schema(yaml_file)
        assert s1.title == "YAML Schema"
        assert len(s1.tables) == 1

        s2 = SchemaLoader.load(yml_file)
        assert s2.title == "YAML Schema"
        assert len(s2.tables) == 1

    def test_load_schema_json_ossie_vs_native(self, tmp_path: Path) -> None:
        """Verify .json automatically distinguishes Ossie vs Native schema."""
        # 1. Ossie JSON (contains "datasets")
        ossie_json = {
            "name": "Ossie in JSON",
            "datasets": [
                {
                    "name": "ossie_tbl",
                    "fields": [{"name": "id", "datatype": "INTEGER"}],
                }
            ],
        }
        f_ossie = tmp_path / "ossie.json"
        f_ossie.write_text(json.dumps(ossie_json), encoding="utf-8")

        s_ossie = load_schema(f_ossie)
        assert s_ossie.title == "Ossie in JSON"
        assert s_ossie.get_table("ossie_tbl") is not None

        # 2. Native SchemaMetadata JSON (contains "tables")
        native_json = {
            "title": "Native Schema",
            "tables": [
                {
                    "name": "native_tbl",
                    "description": "native table",
                    "columns": [
                        {
                            "name": "col_native",
                            "type": "VARCHAR",
                            "description": "native col",
                            "role": "dimension",
                        }
                    ],
                }
            ],
        }
        f_native = tmp_path / "native.json"
        f_native.write_text(json.dumps(native_json), encoding="utf-8")

        s_native = load_schema(f_native)
        assert s_native.title == "Native Schema"
        assert s_native.get_table("native_tbl") is not None

    def test_load_schema_unsupported_extension_raises_error(self, tmp_path: Path) -> None:
        """Verify unsupported extensions raise SchemaValidationError."""
        txt_file = tmp_path / "schema.txt"
        txt_file.write_text("dummy", encoding="utf-8")

        with pytest.raises(SchemaValidationError, match="Unsupported schema file extension"):
            load_schema(txt_file)

    def test_schema_loader_methods(self, tmp_path: Path) -> None:
        """Verify SchemaLoader static methods."""
        yaml_content = """
name: Loader Static
datasets:
  - name: t1
    fields:
      - name: id
"""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        s_from_yaml = SchemaLoader.load_from_yaml(yaml_file)
        assert s_from_yaml.title == "Loader Static"

        s_from_yaml_fn = load_schema_from_yaml(yaml_file)
        assert s_from_yaml_fn.title == "Loader Static"
