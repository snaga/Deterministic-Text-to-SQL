"""Unit tests for extended schema metadata models.

Validates MetricDefinition, HierarchyRule, ColumnMetadata/TableMetadata synonyms,
and SchemaMetadata helpers in accordance with TASK-11.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from deterministic_text_to_sql.schema import (
    ColumnMetadata,
    HierarchyRule,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
    load_schema_from_json,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "tpch_schema.json"


class TestMetricDefinition:
    """Tests for MetricDefinition model."""

    def test_default_values(self) -> None:
        metric = MetricDefinition(
            name="total_revenue",
            expression="SUM(l_extendedprice * (1 - l_discount))",
        )
        assert metric.name == "total_revenue"
        assert metric.expression == "SUM(l_extendedprice * (1 - l_discount))"
        assert metric.description == ""
        assert metric.datatype == "DOUBLE"
        assert metric.synonyms == []

    def test_custom_values_and_roundtrip(self) -> None:
        metric = MetricDefinition(
            name="order_count",
            expression="COUNT(o_orderkey)",
            description="Total count of customer orders",
            datatype="BIGINT",
            synonyms=["注文数", "受注件数", "total orders"],
        )
        assert metric.name == "order_count"
        assert metric.expression == "COUNT(o_orderkey)"
        assert metric.description == "Total count of customer orders"
        assert metric.datatype == "BIGINT"
        assert metric.synonyms == ["注文数", "受注件数", "total orders"]

        dumped = metric.model_dump()
        reloaded = MetricDefinition.model_validate(dumped)
        assert reloaded == metric

    def test_extra_fields_ignored(self) -> None:
        data = {
            "name": "avg_price",
            "expression": "AVG(l_extendedprice)",
            "unknown_extra_field": "ignore_me",
        }
        metric = MetricDefinition.model_validate(data)
        assert metric.name == "avg_price"
        assert not hasattr(metric, "unknown_extra_field")

    def test_validation_error_on_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            MetricDefinition.model_validate({"name": "missing_expression"})


class TestHierarchyRule:
    """Tests for HierarchyRule model."""

    def test_creation_and_defaults(self) -> None:
        rule = HierarchyRule(
            parent_table="region",
            parent_column="r_name",
            child_table="nation",
            child_column="n_name",
        )
        assert rule.parent_table == "region"
        assert rule.parent_column == "r_name"
        assert rule.child_table == "nation"
        assert rule.child_column == "n_name"
        assert rule.child_to_parent == {}

    def test_creation_with_mapping_and_roundtrip(self) -> None:
        mapping = {"JAPAN": "ASIA", "GERMANY": "EUROPE"}
        rule = HierarchyRule(
            parent_table="region",
            parent_column="r_name",
            child_table="nation",
            child_column="n_name",
            child_to_parent=mapping,
        )
        assert rule.child_to_parent == mapping

        dumped = rule.model_dump()
        reloaded = HierarchyRule.model_validate(dumped)
        assert reloaded == rule

    def test_immutability_frozen(self) -> None:
        rule = HierarchyRule(
            parent_table="region",
            parent_column="r_name",
            child_table="nation",
            child_column="n_name",
            child_to_parent={"JAPAN": "ASIA"},
        )
        with pytest.raises(ValidationError, match="Instance is frozen"):
            rule.parent_table = "continent"

        with pytest.raises(ValidationError, match="Instance is frozen"):
            rule.child_to_parent = {"FRANCE": "EUROPE"}

    def test_extra_fields_ignored(self) -> None:
        data = {
            "parent_table": "region",
            "parent_column": "r_name",
            "child_table": "nation",
            "child_column": "n_name",
            "vendor_metadata": "some_extra_info",
        }
        rule = HierarchyRule.model_validate(data)
        assert rule.parent_table == "region"
        assert not hasattr(rule, "vendor_metadata")


class TestColumnAndTableSynonyms:
    """Tests for ColumnMetadata and TableMetadata synonym support."""

    def test_column_metadata_synonyms_default_and_custom(self) -> None:
        col_default = ColumnMetadata(
            name="c_acctbal",
            type="DECIMAL(15,2)",
            description="Account balance",
            role="dimension",
        )
        assert col_default.synonyms == []

        col_with_synonyms = ColumnMetadata(
            name="c_acctbal",
            type="DECIMAL(15,2)",
            description="Account balance",
            role="dimension",
            synonyms=["残高", "口座残高", "balance"],
        )
        assert col_with_synonyms.synonyms == ["残高", "口座残高", "balance"]

    def test_table_metadata_synonyms_default_and_custom(self) -> None:
        tbl_default = TableMetadata(
            name="customer",
            description="Customer master",
        )
        assert tbl_default.synonyms == []
        assert tbl_default.aliases == []

        tbl_with_synonyms = TableMetadata(
            name="customer",
            description="Customer master",
            aliases=["顧客"],
            synonyms=["お得意様", "クライアント", "client"],
        )
        assert tbl_with_synonyms.aliases == ["顧客"]
        assert tbl_with_synonyms.synonyms == ["お得意様", "クライアント", "client"]

    def test_table_get_column_by_name_and_synonym(self) -> None:
        col_key = ColumnMetadata(
            name="c_custkey",
            type="BIGINT",
            description="Customer key",
            role="id",
            synonyms=["顧客ID", "顧客コード"],
        )
        col_name = ColumnMetadata(
            name="c_name",
            type="VARCHAR(25)",
            description="Customer name",
            role="dimension",
            synonyms=["顧客名", "氏名", "customer name"],
        )
        tbl = TableMetadata(
            name="customer",
            description="Customer table",
            columns=[col_key, col_name],
        )

        # 1. Exact match on name
        assert tbl.get_column("c_custkey") == col_key
        # 2. Case-insensitive match on name
        assert tbl.get_column("C_NAME") == col_name
        # 3. Exact match on synonym
        assert tbl.get_column("顧客ID") == col_key
        assert tbl.get_column("customer name") == col_name
        # 4. Case-insensitive match on synonym
        assert tbl.get_column("CUSTOMER NAME") == col_name
        # 5. Non-existent column or synonym
        assert tbl.get_column("non_existent") is None

    def test_table_find_column_by_synonym(self) -> None:
        col = ColumnMetadata(
            name="l_extendedprice",
            type="DECIMAL(15,2)",
            description="Line item price",
            role="metric",
            synonyms=["価格", "単価", "extended price"],
        )
        tbl = TableMetadata(
            name="lineitem",
            description="Lineitem table",
            columns=[col],
        )

        assert tbl.find_column_by_synonym("価格") == col
        assert tbl.find_column_by_synonym("EXTENDED PRICE") == col
        assert tbl.find_column_by_synonym("存在しないシノニム") is None


class TestSchemaMetadataExtended:
    """Tests for SchemaMetadata with metrics, hierarchies, and lookup helpers."""

    def test_schema_defaults(self) -> None:
        schema = SchemaMetadata(title="Test", description="Test schema")
        assert schema.tables == []
        assert schema.metrics == []
        assert schema.hierarchies == []

    def test_schema_with_metrics_and_hierarchies(self) -> None:
        metric = MetricDefinition(
            name="total_revenue",
            expression="SUM(l_extendedprice * (1 - l_discount))",
            description="Gross revenue after discount",
            synonyms=["売上", "総売上", "revenue"],
        )
        rule = HierarchyRule(
            parent_table="region",
            parent_column="r_name",
            child_table="nation",
            child_column="n_name",
            child_to_parent={"JAPAN": "ASIA"},
        )
        tbl = TableMetadata(
            name="lineitem",
            description="Lineitem facts",
            synonyms=["明細", "注文明細"],
        )
        schema = SchemaMetadata(
            title="Extended Schema",
            tables=[tbl],
            metrics=[metric],
            hierarchies=[rule],
        )

        assert len(schema.metrics) == 1
        assert schema.metrics[0].name == "total_revenue"
        assert len(schema.hierarchies) == 1
        assert schema.hierarchies[0].parent_table == "region"

        dumped = schema.model_dump()
        reloaded = SchemaMetadata.model_validate(dumped)
        assert reloaded == schema

    def test_get_metric_lookup(self) -> None:
        metric1 = MetricDefinition(
            name="total_revenue",
            expression="SUM(l_extendedprice * (1 - l_discount))",
            synonyms=["売上", "総売上", "total revenue"],
        )
        metric2 = MetricDefinition(
            name="item_count",
            expression="COUNT(l_orderkey)",
            synonyms=["明細行数", "items count"],
        )
        schema = SchemaMetadata(metrics=[metric1, metric2])

        # 1. Exact match on metric name
        assert schema.get_metric("total_revenue") == metric1
        assert schema.get_metric("item_count") == metric2
        # 2. Case-insensitive match on metric name
        assert schema.get_metric("TOTAL_REVENUE") == metric1
        assert schema.get_metric("Item_Count") == metric2
        # 3. Exact match on synonym
        assert schema.get_metric("売上") == metric1
        assert schema.get_metric("総売上") == metric1
        assert schema.get_metric("明細行数") == metric2
        # 4. Case-insensitive match on synonym
        assert schema.get_metric("TOTAL REVENUE") == metric1
        assert schema.get_metric("ITEMS COUNT") == metric2
        # 5. Unknown metric
        assert schema.get_metric("unknown_metric") is None

    def test_get_table_with_synonyms(self) -> None:
        tbl = TableMetadata(
            name="customer",
            description="Customer table",
            aliases=["顧客"],
            synonyms=["クライアント", "client"],
        )
        schema = SchemaMetadata(tables=[tbl])

        # Exact table name
        assert schema.get_table("customer") == tbl
        # Case-insensitive table name
        assert schema.get_table("CUSTOMER") == tbl
        # Aliases
        assert schema.get_table("顧客") == tbl
        # Synonyms
        assert schema.get_table("クライアント") == tbl
        assert schema.get_table("CLIENT") == tbl
        # Unknown
        assert schema.get_table("orders") is None


class TestBackwardCompatibility:
    """Verify that existing TPC-H schema fixture loads seamlessly."""

    def test_load_tpch_schema_fixture_backward_compat(self) -> None:
        assert FIXTURE_PATH.is_file(), f"Fixture not found at {FIXTURE_PATH}"
        schema = load_schema_from_json(FIXTURE_PATH)

        assert isinstance(schema, SchemaMetadata)
        assert len(schema.tables) == 8
        # Newly added fields default to empty lists
        assert schema.metrics == []
        assert schema.hierarchies == []

        # Customer table column checks
        cust = schema.get_table("customer")
        assert cust is not None
        assert cust.synonyms == []
        custkey_col = cust.get_column("c_custkey")
        assert custkey_col is not None
        assert custkey_col.synonyms == []
