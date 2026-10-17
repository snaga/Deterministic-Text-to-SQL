"""Comprehensive unit tests for metadata-driven dynamic Jev prompt generator.

Tests both backward compatibility with legacy TPC-H constants and dynamic generation
using Apache Ossie semantic models (TPC-H and Flights multi-domain models).
"""

from pathlib import Path

import pytest

from deterministic_text_to_sql.jev.prompts import (
    EXCLUSION_TARGET_CRITERIA,
    MASTER_NATION_CRITERIA,
    MASTER_REGION_CRITERIA,
    METRIC_CRITERIA,
    NUMERIC_COLUMN_CRITERIA,
    NUMERIC_OPERATOR_CRITERIA,
    RANKING_CRITERIA,
    TPCH_DIMENSION_CRITERIA,
    TPCH_PRIMARY_CRITERIA,
    TPCH_SECONDARY_CRITERIA,
    build_jev_payload,
)
from deterministic_text_to_sql.schema import (
    ColumnMetadata,
    OssieSchemaAdapter,
    SchemaMetadata,
    TableMetadata,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
TPCH_OSSIE_YAML = FIXTURES_DIR / "tpch_ossie.yaml"
FLIGHTS_OSSIE_YAML = (
    Path(__file__).resolve().parent.parent.parent
    / "doc_internal"
    / "ossie"
    / "examples"
    / "flights.semantic_model.yaml"
)

EXPECTED_14_QUESTION_KEYS = {
    "primary_table",
    "secondary_table",
    "dimension",
    "metric",
    "ranking",
    "needs_clarification",
    "has_numeric_filter",
    "numeric_column",
    "numeric_operator",
    "has_exclusion_filter",
    "exclusion_target",
    "has_master_filter",
    "master_nation",
    "master_region",
}


@pytest.fixture(scope="module")
def tpch_ossie_schema() -> SchemaMetadata:
    """Fixture providing loaded TPC-H Ossie SchemaMetadata."""
    assert TPCH_OSSIE_YAML.exists(), f"Missing fixture file: {TPCH_OSSIE_YAML}"
    return OssieSchemaAdapter.load_from_yaml(TPCH_OSSIE_YAML)


@pytest.fixture(scope="module")
def flights_ossie_schema() -> SchemaMetadata:
    """Fixture providing loaded Flights Ossie SchemaMetadata."""
    assert FLIGHTS_OSSIE_YAML.exists(), f"Missing example file: {FLIGHTS_OSSIE_YAML}"
    return OssieSchemaAdapter.load_from_yaml(FLIGHTS_OSSIE_YAML)


class TestBackwardCompatibility:
    """Tests ensuring schema=None maintains 100% backward compatibility with TPC-H constants."""

    def test_schema_none_payload_structure(self) -> None:
        """Verify payload top-level keys and structure when schema is omitted."""
        payload = build_jev_payload("顧客ごとの売上合計")
        assert payload["state"] == "顧客ごとの売上合計"
        assert payload["model"] == "jev-latest"
        assert isinstance(payload["questions"], dict)
        assert set(payload["questions"].keys()) == EXPECTED_14_QUESTION_KEYS

    def test_schema_none_criteria_exact_match(self) -> None:
        """Verify all choice questions use the legacy TPC-H criteria constants exactly."""
        payload = build_jev_payload("部品別の在庫数量", schema=None)
        q = payload["questions"]

        assert q["primary_table"]["criteria"] == TPCH_PRIMARY_CRITERIA
        assert q["secondary_table"]["criteria"] == TPCH_SECONDARY_CRITERIA
        assert q["dimension"]["criteria"] == TPCH_DIMENSION_CRITERIA
        assert q["metric"]["criteria"] == METRIC_CRITERIA
        assert q["ranking"]["criteria"] == RANKING_CRITERIA
        assert q["numeric_column"]["criteria"] == NUMERIC_COLUMN_CRITERIA
        assert q["numeric_operator"]["criteria"] == NUMERIC_OPERATOR_CRITERIA
        assert q["exclusion_target"]["criteria"] == EXCLUSION_TARGET_CRITERIA
        assert q["master_nation"]["criteria"] == MASTER_NATION_CRITERIA
        assert q["master_region"]["criteria"] == MASTER_REGION_CRITERIA


class TestDynamicTpchOssiePayload:
    """Tests dynamic criteria generation using TPC-H Ossie semantic model."""

    def test_tpch_ossie_primary_and_secondary_tables(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify ontology parent (region) is excluded from primary but present in secondary."""
        payload = build_jev_payload("顧客別売上", schema=tpch_ossie_schema)
        q = payload["questions"]

        primary_criteria = q["primary_table"]["criteria"]
        secondary_criteria = q["secondary_table"]["criteria"]

        # region is an ontology parent, so it should be excluded from primary_table
        assert "region" not in primary_criteria
        # Core business tables should be present
        for tbl in ["customer", "orders", "lineitem", "part", "supplier", "partsupp"]:
            assert tbl in primary_criteria

        # secondary_table should contain all 8 tables plus 'none'
        for tbl in [
            "customer",
            "orders",
            "lineitem",
            "part",
            "supplier",
            "partsupp",
            "nation",
            "region",
            "none",
        ]:
            assert tbl in secondary_criteria

    def test_tpch_ossie_dimensions_and_metrics(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify dynamic extraction of dimensions and semantic metrics from SchemaMetadata."""
        payload = build_jev_payload("国別の売上合計", schema=tpch_ossie_schema)
        q = payload["questions"]

        dim_criteria = q["dimension"]["criteria"]
        # Dimension columns from tables
        assert "c_name" in dim_criteria
        assert "c_custkey" in dim_criteria
        assert "n_name" in dim_criteria
        assert "r_name" in dim_criteria
        assert "o_orderpriority" in dim_criteria
        assert "none" in dim_criteria

        metric_criteria = q["metric"]["criteria"]
        # Defined semantic metrics in tpch_ossie.yaml
        assert "total_revenue" in metric_criteria
        assert "total_quantity" in metric_criteria
        assert "avg_price" in metric_criteria
        assert "total_acctbal" in metric_criteria
        # Standard generic fallbacks
        assert "count_records" in metric_criteria
        assert "raw_list" in metric_criteria

    def test_tpch_ossie_numeric_and_master_filters(
        self, tpch_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify numeric columns and hierarchy ontology mapping in criteria."""
        payload = build_jev_payload("残高1000以上の顧客", schema=tpch_ossie_schema)
        q = payload["questions"]

        num_criteria = q["numeric_column"]["criteria"]
        assert "c_acctbal" in num_criteria
        assert "ps_availqty" in num_criteria
        assert "l_discount" in num_criteria
        assert "l_quantity" in num_criteria
        assert "o_totalprice" in num_criteria
        assert "p_retailprice" in num_criteria
        assert "none" in num_criteria

        nation_criteria = q["master_nation"]["criteria"]
        assert "JAPAN" in nation_criteria
        assert "UNITED STATES" in nation_criteria
        assert "GERMANY" in nation_criteria
        assert "none" in nation_criteria

        region_criteria = q["master_region"]["criteria"]
        assert "ASIA" in region_criteria
        assert "EUROPE" in region_criteria
        assert "AMERICA" in region_criteria
        assert "none" in region_criteria

        ex_criteria = q["exclusion_target"]["criteria"]
        assert "returns" in ex_criteria
        assert "cancelled" in ex_criteria
        assert "none" in ex_criteria


class TestDynamicFlightsSemanticModelPayload:
    """Tests multi-domain generalization using Flights semantic model."""

    def test_flights_domain_tables_and_isolation(
        self, flights_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify flights tables are extracted and TPC-H tables are completely isolated."""
        payload = build_jev_payload("航空会社別の便数", schema=flights_ossie_schema)
        q = payload["questions"]

        primary_criteria = q["primary_table"]["criteria"]
        secondary_criteria = q["secondary_table"]["criteria"]

        # Flights tables should be present
        for tbl in ["RUNWAY", "AIRCRAFT", "FLIGHT", "CARRIER", "ROUTE"]:
            assert tbl in primary_criteria
            assert tbl in secondary_criteria

        # TPC-H tables must NOT be leaked
        tpch_tables = [
            "customer",
            "orders",
            "lineitem",
            "part",
            "partsupp",
            "supplier",
            "nation",
            "region",
        ]
        for tbl in tpch_tables:
            assert tbl not in primary_criteria
            assert tbl not in secondary_criteria

    def test_flights_domain_columns_and_numeric_columns(
        self, flights_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify flight columns are extracted into dimension and numeric_column."""
        payload = build_jev_payload("座席数200以上の機体", schema=flights_ossie_schema)
        q = payload["questions"]

        dim_criteria = q["dimension"]["criteria"]
        # Flight dimension columns
        assert "tail_nr" in dim_criteria
        assert "scheduled_arrival" in dim_criteria
        assert "airport_code" in dim_criteria
        assert "none" in dim_criteria

        # TPC-H columns must NOT be present
        for tpch_col in ["c_custkey", "c_name", "o_orderdate", "l_extendedprice"]:
            assert tpch_col not in dim_criteria

        num_criteria = q["numeric_column"]["criteria"]
        # Numeric columns in flights (via type or numeric name heuristic)
        assert "nr_seats" in num_criteria
        assert "capacity" in num_criteria
        assert "length" in num_criteria
        assert "distance" in num_criteria
        assert "none" in num_criteria

        for tpch_col in ["c_acctbal", "l_discount", "ps_availqty", "p_retailprice"]:
            assert tpch_col not in num_criteria

    def test_flights_master_and_exclusion_no_tpch_leak(
        self, flights_ossie_schema: SchemaMetadata
    ) -> None:
        """Verify nation/region criteria contains only 'none' for domains without hierarchy."""
        payload = build_jev_payload("欠航フライトの一覧", schema=flights_ossie_schema)
        q = payload["questions"]

        # Flights schema has no nation/region hierarchies
        nation_criteria = q["master_nation"]["criteria"]
        assert list(nation_criteria.keys()) == ["none"]

        region_criteria = q["master_region"]["criteria"]
        assert list(region_criteria.keys()) == ["none"]

        # TPC-H returns filter (lineitem.l_returnflag) must not be in flights
        ex_criteria = q["exclusion_target"]["criteria"]
        assert "returns" not in ex_criteria
        # FLIGHT has 'cancelled' column
        assert "cancelled" in ex_criteria
        assert "none" in ex_criteria


class TestEmptyAndMinimalSchemaFallback:
    """Tests handling of empty or edge-case schemas."""

    def test_empty_schema_metadata_falls_back_to_defaults(self) -> None:
        """Verify SchemaMetadata() with empty tables falls back to backward compatible defaults."""
        empty_schema = SchemaMetadata()
        payload = build_jev_payload("テスト質問", schema=empty_schema)
        q = payload["questions"]

        assert q["primary_table"]["criteria"] == TPCH_PRIMARY_CRITERIA
        assert q["secondary_table"]["criteria"] == TPCH_SECONDARY_CRITERIA
        assert q["dimension"]["criteria"] == TPCH_DIMENSION_CRITERIA

    def test_minimal_custom_schema_generation(self) -> None:
        """Verify minimal schema with a single table generates valid payload without error."""
        minimal_schema = SchemaMetadata(
            tables=[
                TableMetadata(
                    name="USERS",
                    description="User accounts",
                    columns=[
                        ColumnMetadata(
                            name="user_id",
                            type="INTEGER",
                            description="User identifier",
                            role="id",
                        ),
                        ColumnMetadata(
                            name="age",
                            type="INTEGER",
                            description="User age",
                            role="dimension",
                        ),
                    ],
                )
            ]
        )
        payload = build_jev_payload("ユーザーの年齢", schema=minimal_schema)
        q = payload["questions"]

        assert set(q.keys()) == EXPECTED_14_QUESTION_KEYS
        assert "USERS" in q["primary_table"]["criteria"]
        assert "USERS" in q["secondary_table"]["criteria"]
        assert "none" in q["secondary_table"]["criteria"]
        assert "user_id" in q["dimension"]["criteria"]
        assert "age" in q["dimension"]["criteria"]
        assert "none" in q["dimension"]["criteria"]
        assert "count_records" in q["metric"]["criteria"]
        assert "raw_list" in q["metric"]["criteria"]
        assert "age" in q["numeric_column"]["criteria"]
        assert "none" in q["numeric_column"]["criteria"]
        assert list(q["master_nation"]["criteria"].keys()) == ["none"]
        assert list(q["master_region"]["criteria"].keys()) == ["none"]
