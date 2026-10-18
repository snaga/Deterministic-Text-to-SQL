"""Unit tests for DimensionMetadata, HavingSlot, and Ossie time dimension extraction."""

import pytest
from pydantic import ValidationError

from deterministic_text_to_sql.compiler.models import HavingSlot, QueryIntent
from deterministic_text_to_sql.schema.models import DimensionMetadata, SchemaMetadata
from deterministic_text_to_sql.schema.ossie import OssieSchemaAdapter


class TestDimensionMetadata:
    """Tests for DimensionMetadata Pydantic model."""

    def test_default_values(self) -> None:
        dim = DimensionMetadata(name="order_year")
        assert dim.name == "order_year"
        assert dim.expression is None
        assert dim.description == ""
        assert dim.synonyms == []
        assert dim.is_time is False
        assert dim.time_granularity is None

    def test_full_instantiation(self) -> None:
        dim = DimensionMetadata(
            name="orders_year",
            expression="EXTRACT(year FROM orders.o_orderdate)",
            description="注文年（年別集約軸）",
            synonyms=["年", "年別", "注文年", "年度"],
            is_time=True,
            time_granularity="year",
        )
        assert dim.name == "orders_year"
        assert dim.expression == "EXTRACT(year FROM orders.o_orderdate)"
        assert dim.description == "注文年（年別集約軸）"
        assert dim.synonyms == ["年", "年別", "注文年", "年度"]
        assert dim.is_time is True
        assert dim.time_granularity == "year"

    def test_immutability_frozen(self) -> None:
        dim = DimensionMetadata(name="order_year")
        with pytest.raises(ValidationError):
            # Attempt to modify a frozen model attribute
            dim.name = "new_year"

    def test_extra_fields_ignored(self) -> None:
        dim = DimensionMetadata.model_validate({"name": "order_year", "extra_field": "ignored_val"})
        assert dim.name == "order_year"
        assert not hasattr(dim, "extra_field")


class TestHavingSlot:
    """Tests for HavingSlot Pydantic model."""

    def test_instantiation(self) -> None:
        having = HavingSlot(metric="total_revenue", operator=">=", value=1000000)
        assert having.metric == "total_revenue"
        assert having.operator == ">="
        assert having.value == 1000000

    def test_immutability_frozen(self) -> None:
        having = HavingSlot(metric="total_revenue", operator=">=", value=1000000)
        with pytest.raises(ValidationError):
            having.value = 500000

    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            HavingSlot.model_validate({"metric": "total_revenue"})


class TestQueryIntentWithHaving:
    """Tests for QueryIntent integration with HavingSlot."""

    def test_default_having_is_empty_list(self) -> None:
        intent = QueryIntent(primary_table="orders")
        assert intent.having == []

    def test_having_slots_assignment(self) -> None:
        having1 = HavingSlot(metric="total_revenue", operator=">=", value=1000000)
        having2 = HavingSlot(metric="order_count", operator=">", value=5)
        intent = QueryIntent(
            primary_table="orders",
            dimensions=["orders_year"],
            having=[having1, having2],
        )
        assert len(intent.having) == 2
        assert intent.having[0].metric == "total_revenue"
        assert intent.having[1].value == 5


class TestSchemaMetadataDimensionLookup:
    """Tests for SchemaMetadata dimensions list and get_dimension lookup."""

    @pytest.fixture
    def sample_schema(self) -> SchemaMetadata:
        dim_year = DimensionMetadata(
            name="orders_year",
            expression="EXTRACT(year FROM orders.o_orderdate)",
            description="Year of order",
            synonyms=["年", "年別", "注文年", "年度"],
            is_time=True,
            time_granularity="year",
        )
        dim_month = DimensionMetadata(
            name="orders_month",
            expression="EXTRACT(month FROM orders.o_orderdate)",
            description="Month of order",
            synonyms=["月", "月別", "注文月"],
            is_time=True,
            time_granularity="month",
        )
        dim_calc = DimensionMetadata(
            name="customer_vip_tier",
            expression="CASE WHEN c_acctbal > 5000 THEN 'VIP' ELSE 'REGULAR' END",
            description="Customer tier calculation",
            synonyms=["会員ランク", "VIPランク", "顧客区分"],
            is_time=False,
        )
        return SchemaMetadata(
            title="Test Schema",
            dimensions=[dim_year, dim_month, dim_calc],
        )

    def test_get_dimension_by_exact_name(self, sample_schema: SchemaMetadata) -> None:
        dim = sample_schema.get_dimension("orders_year")
        assert dim is not None
        assert dim.name == "orders_year"
        assert dim.time_granularity == "year"

    def test_get_dimension_by_case_insensitive_name(self, sample_schema: SchemaMetadata) -> None:
        dim = sample_schema.get_dimension("ORDERS_YEAR")
        assert dim is not None
        assert dim.name == "orders_year"

    def test_get_dimension_by_exact_synonym(self, sample_schema: SchemaMetadata) -> None:
        dim = sample_schema.get_dimension("注文年")
        assert dim is not None
        assert dim.name == "orders_year"

        dim_month = sample_schema.get_dimension("月別")
        assert dim_month is not None
        assert dim_month.name == "orders_month"

        dim_tier = sample_schema.get_dimension("会員ランク")
        assert dim_tier is not None
        assert dim_tier.name == "customer_vip_tier"

    def test_get_dimension_by_case_insensitive_synonym(self) -> None:
        dim_calc = DimensionMetadata(
            name="status_code",
            synonyms=["StatusCode", "STATUS_FLAG"],
        )
        schema = SchemaMetadata(dimensions=[dim_calc])
        dim = schema.get_dimension("statuscode")
        assert dim is not None
        assert dim.name == "status_code"

    def test_get_dimension_not_found(self, sample_schema: SchemaMetadata) -> None:
        assert sample_schema.get_dimension("nonexistent") is None
        assert sample_schema.get_dimension("日別") is None


class TestOssieTimeDimensionExtraction:
    """Tests for OssieSchemaAdapter extracting time dimensions and custom extensions."""

    def test_time_granularities_extraction_with_is_time(self) -> None:
        raw_schema = {
            "name": "sales_model",
            "datasets": [
                {
                    "name": "orders",
                    "description": "受注伝票テーブル",
                    "ai_context": {"synonyms": ["注文", "オーダー"]},
                    "fields": [
                        {
                            "name": "o_orderdate",
                            "datatype": "DATE",
                            "description": "注文日",
                            "is_time": True,
                            "time_granularities": ["year", "month"],
                            "ai_context": {"synonyms": ["注文日", "オーダー日"]},
                        },
                        {
                            "name": "o_totalprice",
                            "datatype": "DOUBLE",
                            "role": "metric",
                        },
                    ],
                }
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(raw_schema)
        assert len(schema.dimensions) == 2

        dim_year = schema.get_dimension("orders_year")
        assert dim_year is not None
        assert dim_year.name == "orders_year"
        assert dim_year.expression == "EXTRACT(year FROM orders.o_orderdate)"
        assert dim_year.is_time is True
        assert dim_year.time_granularity == "year"
        # Verify required synonyms from prompt/task spec
        for expected in ["年", "年別", "注文年", "年度"]:
            assert expected in dim_year.synonyms, f"Missing '{expected}' in {dim_year.synonyms}"

        dim_month = schema.get_dimension("orders_month")
        assert dim_month is not None
        assert dim_month.name == "orders_month"
        assert dim_month.expression == "EXTRACT(month FROM orders.o_orderdate)"
        assert dim_month.is_time is True
        assert dim_month.time_granularity == "month"
        for expected in ["月", "月別", "注文月"]:
            assert expected in dim_month.synonyms, f"Missing '{expected}' in {dim_month.synonyms}"

    def test_time_dimension_in_nested_dimension_dict(self) -> None:
        raw_schema = {
            "name": "log_model",
            "datasets": [
                {
                    "name": "events",
                    "fields": [
                        {
                            "name": "event_timestamp",
                            "datatype": "TIMESTAMP",
                            "dimension": {
                                "is_time": True,
                                "type_params": {
                                    "time_granularities": ["day", "hour"],
                                },
                            },
                        }
                    ],
                }
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(raw_schema)
        assert len(schema.dimensions) == 2
        day_dim = schema.get_dimension("events_day")
        assert day_dim is not None
        assert day_dim.expression == "EXTRACT(day FROM events.event_timestamp)"
        assert day_dim.time_granularity == "day"

        hour_dim = schema.get_dimension("events_hour")
        assert hour_dim is not None
        assert hour_dim.expression == "EXTRACT(hour FROM events.event_timestamp)"
        assert hour_dim.time_granularity == "hour"

    def test_custom_extensions_dimensions(self) -> None:
        raw_schema = {
            "name": "custom_model",
            "datasets": [
                {
                    "name": "customer",
                    "fields": [{"name": "c_custkey", "datatype": "INTEGER"}],
                }
            ],
            "custom_extensions": [
                {
                    "vendor_name": "DETERMINISTIC_TEXT_TO_SQL",
                    "data": {
                        "dimensions": [
                            {
                                "name": "customer_age_group",
                                "expression": "FLOOR(c_age / 10) * 10",
                                "description": "10歳刻みの年齢層",
                                "synonyms": ["年代", "年齢層", "世代"],
                                "is_time": False,
                            }
                        ]
                    },
                }
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(raw_schema)
        dim = schema.get_dimension("年代")
        assert dim is not None
        assert dim.name == "customer_age_group"
        assert dim.expression == "FLOOR(c_age / 10) * 10"
        assert dim.is_time is False

    def test_custom_extensions_overwrites_auto_generated_dimension(self) -> None:
        raw_schema = {
            "name": "override_model",
            "datasets": [
                {
                    "name": "orders",
                    "fields": [
                        {
                            "name": "o_orderdate",
                            "datatype": "DATE",
                            "is_time": True,
                            "time_granularities": ["year"],
                        }
                    ],
                }
            ],
            "custom_extensions": [
                {
                    "vendor_name": "DETERMINISTIC_TEXT_TO_SQL",
                    "data": {
                        "dimensions": [
                            {
                                "name": "orders_year",
                                "expression": "STRFTIME('%Y', orders.o_orderdate)",
                                "description": "SQLite specific year extraction",
                                "synonyms": ["年度別", "会計年"],
                                "is_time": True,
                                "time_granularity": "year",
                            }
                        ]
                    },
                }
            ],
        }

        schema = OssieSchemaAdapter.load_from_dict(raw_schema)
        assert len(schema.dimensions) == 1
        dim = schema.get_dimension("orders_year")
        assert dim is not None
        assert dim.expression == "STRFTIME('%Y', orders.o_orderdate)"
        assert "会計年" in dim.synonyms
