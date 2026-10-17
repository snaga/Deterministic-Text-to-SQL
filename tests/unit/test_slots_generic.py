"""Detroit-school unit tests for generic slot binding based on semantic metadata and synonyms."""

from __future__ import annotations

import pytest

from deterministic_text_to_sql.jev.slots import parse_jev_response
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    HierarchyRule,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
)


@pytest.fixture
def flights_schema() -> SchemaMetadata:
    """Create a sample generic aviation/flight schema without TPC-H prefix conventions."""
    flight_table = TableMetadata(
        name="FLIGHT",
        description="定期便フライト情報",
        aliases=["フライト", "便", "flight"],
        synonyms=["フライト", "便", "flights"],
        columns=[
            ColumnMetadata(name="flight_id", type="INTEGER", description="フライトID", role="id"),
            ColumnMetadata(
                name="flight_no",
                type="VARCHAR(10)",
                description="便名",
                role="dimension",
                synonyms=["便名", "フライト番号"],
            ),
            ColumnMetadata(
                name="distance",
                type="INTEGER",
                description="飛行距離",
                role="metric",
                agg=["SUM", "AVG", "MAX"],
                synonyms=["飛行距離", "距離", "マイル"],
            ),
            ColumnMetadata(
                name="status",
                type="VARCHAR(20)",
                description="運航状況",
                role="dimension",
                values=["SCHEDULED", "ACTIVE", "LANDED", "CANCELLED"],
                synonyms=["状況", "ステータス"],
            ),
        ],
    )

    runway_table = TableMetadata(
        name="RUNWAY",
        description="空港滑走路データ",
        aliases=["滑走路", "runway"],
        synonyms=["滑走路", "滑走路情報"],
        columns=[
            ColumnMetadata(name="runway_id", type="INTEGER", description="滑走路ID", role="id"),
            ColumnMetadata(
                name="length",
                type="INTEGER",
                description="滑走路長",
                role="metric",
                agg=["AVG", "MAX"],
                synonyms=["長さ", "滑走路長"],
            ),
            ColumnMetadata(
                name="surface",
                type="VARCHAR(20)",
                description="舗装路面種別",
                role="dimension",
                values=["ASPHALT", "CONCRETE", "TURF"],
            ),
        ],
    )

    aircraft_table = TableMetadata(
        name="AIRCRAFT",
        description="航空機機材スペック",
        aliases=["機材", "飛行機", "航空機", "aircraft"],
        synonyms=["機材", "飛行機", "航空機"],
        columns=[
            ColumnMetadata(name="aircraft_id", type="INTEGER", description="機材ID", role="id"),
            ColumnMetadata(
                name="model_name",
                type="VARCHAR(50)",
                description="機体モデル名",
                role="dimension",
                synonyms=["型番", "モデル", "機種"],
            ),
            ColumnMetadata(
                name="nr_seats",
                type="INTEGER",
                description="座席定員数",
                role="metric",
                agg=["SUM", "AVG"],
                synonyms=["座席数", "定員"],
            ),
        ],
    )

    return SchemaMetadata(
        title="Flights Semantic Schema",
        tables=[flight_table, runway_table, aircraft_table],
        metrics=[
            MetricDefinition(
                name="avg_distance",
                expression="AVG(distance)",
                description="平均飛行距離",
                synonyms=["平均距離", "平均飛行マイル"],
            )
        ],
    )


@pytest.fixture
def ecommerce_schema() -> SchemaMetadata:
    """Create a sample generic e-commerce schema with hierarchies and synonyms."""
    user_table = TableMetadata(
        name="users",
        description="利用者・会員マスター",
        aliases=["会員", "ユーザー", "顧客", "user"],
        synonyms=["ユーザー", "会員", "顧客"],
        columns=[
            ColumnMetadata(name="user_id", type="INTEGER", description="ユーザーID", role="id"),
            ColumnMetadata(
                name="user_name",
                type="VARCHAR(50)",
                description="会員氏名",
                role="dimension",
                synonyms=["ユーザー名", "名前", "氏名"],
            ),
            ColumnMetadata(
                name="age",
                type="INTEGER",
                description="年齢",
                role="dimension",
                synonyms=["年齢", "歳"],
            ),
        ],
    )

    order_table = TableMetadata(
        name="orders",
        description="注文ヘッダー",
        aliases=["注文", "オーダー", "order"],
        synonyms=["注文", "オーダー"],
        columns=[
            ColumnMetadata(name="order_id", type="INTEGER", description="注文ID", role="id"),
            ColumnMetadata(
                name="amount",
                type="DECIMAL(12,2)",
                description="支払総額",
                role="metric",
                agg=["SUM", "AVG"],
                synonyms=["金額", "総額", "支払金額"],
            ),
            ColumnMetadata(
                name="order_status",
                type="VARCHAR(20)",
                description="注文ステータス",
                role="dimension",
                values=["PENDING", "COMPLETED", "CANCELLED"],
                synonyms=["注文状況", "ステータス"],
            ),
        ],
    )

    city_table = TableMetadata(
        name="city",
        description="市区町村マスター",
        columns=[
            ColumnMetadata(name="city_id", type="INTEGER", description="都市ID", role="id"),
            ColumnMetadata(
                name="city_name",
                type="VARCHAR(50)",
                description="都市名",
                role="dimension",
                values=["SHINJUKU", "UMEDA", "HAKATA"],
            ),
        ],
    )

    pref_table = TableMetadata(
        name="prefecture",
        description="都道府県マスター",
        columns=[
            ColumnMetadata(name="pref_id", type="INTEGER", description="都道府県ID", role="id"),
            ColumnMetadata(
                name="pref_name",
                type="VARCHAR(50)",
                description="都道府県名",
                role="dimension",
                values=["TOKYO", "OSAKA", "FUKUOKA"],
            ),
        ],
    )

    hierarchy = HierarchyRule(
        parent_table="prefecture",
        parent_column="pref_name",
        child_table="city",
        child_column="city_name",
        child_to_parent={
            "SHINJUKU": "TOKYO",
            "UMEDA": "OSAKA",
            "HAKATA": "FUKUOKA",
        },
    )

    return SchemaMetadata(
        title="E-Commerce Schema",
        tables=[user_table, order_table, city_table, pref_table],
        hierarchies=[hierarchy],
    )


class TestGenericSchemaNumericFilters:
    """Tests for binding numeric filters and fallback selection in generic schemas."""

    def test_numeric_column_binding_without_prefix(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify numeric column in generic schema without c_/o_/l_ prefix binds correctly."""
        raw_response = {
            "primary_table": "FLIGHT",
            "secondary_table": "none",
            "dimension": "flight_no",
            "metric": "count_records",
            "has_numeric_filter": 0.95,
            "numeric_column": "distance",
            "numeric_operator": "GT",
        }
        query = "飛行距離が1000より大きいフライト"
        parsed = parse_jev_response(query, raw_response, schema=flights_schema)

        assert parsed.primary_table == "FLIGHT"
        assert parsed.dimension == "flight_no"
        assert len(parsed.filter_slots) == 1
        fs = parsed.filter_slots[0]
        assert fs.table == "FLIGHT"
        assert fs.column == "distance"
        assert fs.operator == ">"
        assert fs.value == 1000.0

    def test_numeric_fallback_selects_metric_column(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify 7.4 numeric fallback picks a column with role == 'metric'."""
        raw_response = {
            "primary_table": "RUNWAY",
            "secondary_table": "none",
            "dimension": "none",
            "metric": "count_records",
        }
        query = "3000m以上の滑走路"
        parsed = parse_jev_response(query, raw_response, schema=flights_schema)

        assert parsed.primary_table == "RUNWAY"
        assert len(parsed.filter_slots) == 1
        fs = parsed.filter_slots[0]
        assert fs.table == "RUNWAY"
        assert fs.column == "length"  # Picked from role == "metric"
        assert fs.operator == ">="
        assert fs.value == 3000.0

    def test_numeric_fallback_selects_numeric_type_column_when_no_metric_role(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify 7.4 numeric fallback picks an INTEGER column when table has no metric role."""
        raw_response = {
            "primary_table": "users",
            "secondary_table": "none",
            "dimension": "user_name",
            "metric": "count_records",
        }
        query = "20歳以上の会員"
        parsed = parse_jev_response(query, raw_response, schema=ecommerce_schema)

        assert parsed.primary_table == "users"
        assert len(parsed.filter_slots) == 1
        fs = parsed.filter_slots[0]
        assert fs.table == "users"
        assert fs.column == "age"  # INTEGER column selected
        assert fs.operator == ">="
        assert fs.value == 20.0


class TestSynonymSlotResolution:
    """Tests for resolving tables, dimensions, metrics, and filter columns from synonyms."""

    def test_numeric_column_resolved_from_synonym(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify Jev answer with column synonym ('飛行距離')
        resolves to physical name ('distance').
        """
        raw_response = {
            "primary_table": "FLIGHT",
            "has_numeric_filter": 0.99,
            "numeric_column": "飛行距離",
            "numeric_operator": "GTE",
        }
        query = "飛行距離が 500 以上のフライト"
        parsed = parse_jev_response(query, raw_response, schema=flights_schema)

        assert len(parsed.filter_slots) == 1
        fs = parsed.filter_slots[0]
        assert fs.table == "FLIGHT"
        assert fs.column == "distance"
        assert fs.operator == ">="
        assert fs.value == 500.0

    def test_dimension_resolved_from_synonym(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify dimension given as synonym ('型番')
        resolves to physical column ('model_name').
        """
        raw_response = {
            "primary_table": "AIRCRAFT",
            "dimension": "型番",
            "metric": "count_records",
        }
        parsed = parse_jev_response("型番別の航空機", raw_response, schema=flights_schema)
        assert parsed.dimension == "model_name"

    def test_primary_table_resolved_from_synonym(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify primary_table given as synonym ('飛行機')
        resolves to physical table ('AIRCRAFT').
        """
        raw_response = {
            "primary_table": "飛行機",
            "dimension": "model_name",
            "metric": "count_records",
        }
        parsed = parse_jev_response("飛行機一覧", raw_response, schema=flights_schema)
        assert parsed.primary_table == "AIRCRAFT"

    def test_metric_resolved_from_synonym(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify metric synonym ('平均距離') resolves to metric name ('avg_distance')."""
        raw_response = {
            "primary_table": "FLIGHT",
            "metric": "平均距離",
        }
        parsed = parse_jev_response("平均距離", raw_response, schema=flights_schema)
        assert parsed.metric == "avg_distance"


class TestHierarchyMasterBinding:
    """Tests for hierarchical ontology rules (schema.hierarchies) binding."""

    def test_custom_hierarchy_child_binding(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify child entity in hierarchy (SHINJUKU) binds to child table and column."""
        raw_response = {
            "primary_table": "orders",
            "has_master_filter": 0.95,
            "master_nation": "SHINJUKU",
        }
        query = "新宿の注文一覧"
        parsed = parse_jev_response(query, raw_response, schema=ecommerce_schema)

        assert any(
            fs.table == "city" and fs.column == "city_name" and fs.value == "SHINJUKU"
            for fs in parsed.filter_slots
        )

    def test_custom_hierarchy_parent_binding(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify parent entity in hierarchy (TOKYO) binds to parent table and column."""
        raw_response = {
            "primary_table": "orders",
            "has_master_filter": 0.95,
            "master_region": "TOKYO",
        }
        query = "東京の注文一覧"
        parsed = parse_jev_response(query, raw_response, schema=ecommerce_schema)

        assert any(
            fs.table == "prefecture" and fs.column == "pref_name" and fs.value == "TOKYO"
            for fs in parsed.filter_slots
        )

    def test_hierarchy_candidate_extracted_from_query(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify query tokens matching hierarchy mapping values create FilterSlots."""
        raw_response = {
            "primary_table": "users",
        }
        query = "UMEDAの会員一覧"
        parsed = parse_jev_response(query, raw_response, schema=ecommerce_schema)

        assert any(
            fs.table == "city" and fs.column == "city_name" and fs.value == "UMEDA"
            for fs in parsed.filter_slots
        )


class TestWhitelistCategoricalValues:
    """Tests for column.values categorical whitelist binding (F-JEV-02)."""

    def test_column_values_whitelist_binding(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify value in column.values (ACTIVE) binds to corresponding table and column."""
        raw_response = {
            "primary_table": "FLIGHT",
        }
        query = "ACTIVE なフライト一覧を表示"
        parsed = parse_jev_response(query, raw_response, schema=flights_schema)

        assert any(
            fs.table == "FLIGHT" and fs.column == "status" and fs.value == "ACTIVE"
            for fs in parsed.filter_slots
        )

    def test_column_values_case_insensitive_matching(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify lowercase 'landed' in query binds to uppercase 'LANDED' from whitelist."""
        raw_response = {
            "primary_table": "FLIGHT",
        }
        query = "landed なフライト一覧"
        parsed = parse_jev_response(query, raw_response, schema=flights_schema)

        assert any(
            fs.table == "FLIGHT" and fs.column == "status" and fs.value == "LANDED"
            for fs in parsed.filter_slots
        )

    def test_injection_or_unlisted_value_not_bound(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify values not present in column.values or hierarchy rules are dropped."""
        raw_response = {
            "primary_table": "FLIGHT",
            "filters": ["'; DROP TABLE FLIGHT; --", "UNKNOWN_STATUS"],
        }
        query = "フライト一覧"
        parsed = parse_jev_response(query, raw_response, schema=flights_schema)

        # Injection and unlisted values must not be bound as FilterSlots
        assert len(parsed.filter_slots) == 0


class TestGenericExclusionFilter:
    """Tests for generic exclusion filter identification in custom schemas."""

    def test_exclusion_cancelled_identified_from_schema(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify 'cancelled' target binds to order_status != 'CANCELLED'."""
        raw_response = {
            "primary_table": "orders",
            "has_exclusion_filter": 0.90,
            "exclusion_target": "cancelled",
        }
        query = "キャンセルを除外した注文合計"
        parsed = parse_jev_response(query, raw_response, schema=ecommerce_schema)

        assert any(
            fs.table == "orders"
            and fs.column == "order_status"
            and fs.operator == "!="
            and fs.value == "CANCELLED"
            for fs in parsed.filter_slots
        )

    def test_exclusion_returns_bound_to_generic_table(self) -> None:
        """Verify 'returns' target binds to custom return column."""
        custom_schema = SchemaMetadata(
            tables=[
                TableMetadata(
                    name="sales_items",
                    description="売上明細",
                    columns=[
                        ColumnMetadata(
                            name="item_id",
                            type="INTEGER",
                            description="明細ID",
                            role="id",
                        ),
                        ColumnMetadata(
                            name="return_flag",
                            type="VARCHAR(10)",
                            description="返品区分",
                            role="dimension",
                            values=["RETURNED", "KEPT"],
                            synonyms=["返品フラグ", "返品"],
                        ),
                    ],
                )
            ]
        )
        raw_response = {
            "primary_table": "sales_items",
            "has_exclusion_filter": 0.95,
            "exclusion_target": "returns",
        }
        query = "返品を除外した売上明細"
        parsed = parse_jev_response(query, raw_response, schema=custom_schema)

        assert any(
            fs.table == "sales_items"
            and fs.column == "return_flag"
            and fs.operator == "!="
            and fs.value == "RETURNED"
            for fs in parsed.filter_slots
        )


class TestSchemaNoneBackwardCompatibility:
    """Tests guaranteeing 100% backward compatibility when schema is None."""

    def test_schema_none_tpch_prefix_resolution(self) -> None:
        """Verify c_ prefix binds to customer when schema is None."""
        raw_response = {
            "primary_table": "customer",
            "has_numeric_filter": 0.95,
            "numeric_column": "c_acctbal",
            "numeric_operator": "GTE",
        }
        query = "残高が 1000 以上の顧客"
        parsed = parse_jev_response(query, raw_response, schema=None)

        assert len(parsed.filter_slots) == 1
        fs = parsed.filter_slots[0]
        assert fs.table == "customer"
        assert fs.column == "c_acctbal"
        assert fs.operator == ">="
        assert fs.value == 1000.0

    def test_schema_none_tpch_nation_region_constants(self) -> None:
        """Verify TPCH_NATIONS and TPCH_REGIONS work when schema is None."""
        raw_response = {
            "primary_table": "customer",
            "has_master_filter": 0.95,
            "master_nation": "JAPAN",
        }
        query = "日本の顧客一覧"
        parsed = parse_jev_response(query, raw_response, schema=None)

        assert any(
            fs.table == "nation" and fs.column == "n_name" and fs.value == "JAPAN"
            for fs in parsed.filter_slots
        )

    def test_schema_none_exclusion_filters(self) -> None:
        """Verify default TPC-H exclusion filters when schema is None."""
        raw_response = {
            "primary_table": "lineitem",
            "has_exclusion_filter": 0.95,
            "exclusion_target": "returns",
        }
        query = "返品を除外した売上"
        parsed = parse_jev_response(query, raw_response, schema=None)

        assert any(
            fs.table == "lineitem"
            and fs.column == "l_returnflag"
            and fs.operator == "!="
            and fs.value == "R"
            for fs in parsed.filter_slots
        )

    def test_schema_none_numeric_fallback(self) -> None:
        """Verify orders defaults to o_totalprice when schema is None."""
        raw_response = {
            "primary_table": "orders",
        }
        query = "50000 以上の注文"
        parsed = parse_jev_response(query, raw_response, schema=None)

        assert len(parsed.filter_slots) == 1
        fs = parsed.filter_slots[0]
        assert fs.table == "orders"
        assert fs.column == "o_totalprice"
        assert fs.operator == ">="
        assert fs.value == 50000.0
