"""Detroit-style unit tests for generic dynamic HITL ambiguity detection and prompt synthesis."""

import pytest

from deterministic_text_to_sql.hitl import (
    HITLIssue,
    HITLManager,
    HITLOption,
)
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.schema.models import (
    ColumnMetadata,
    MetricDefinition,
    SchemaMetadata,
    TableMetadata,
)


@pytest.fixture
def flights_schema() -> SchemaMetadata:
    """Fixture for Flights schema metadata with dynamic tables."""
    return SchemaMetadata(
        title="Flights Schema",
        tables=[
            TableMetadata(
                name="FLIGHT",
                description="運航便明細情報",
                aliases=["flight", "flights", "便"],
                columns=[
                    ColumnMetadata(
                        name="flight_id", type="INTEGER", description="便ID", role="id"
                    ),
                    ColumnMetadata(
                        name="route_id", type="INTEGER", description="路線ID", role="fk"
                    ),
                    ColumnMetadata(
                        name="distance", type="DOUBLE", description="飛行距離", role="metric"
                    ),
                ],
            ),
            TableMetadata(
                name="ROUTE",
                description="路線基本情報",
                aliases=["route", "routes", "路線"],
                columns=[
                    ColumnMetadata(
                        name="route_id", type="INTEGER", description="路線ID", role="id"
                    ),
                    ColumnMetadata(
                        name="origin", type="VARCHAR", description="出発空港", role="dimension"
                    ),
                    ColumnMetadata(
                        name="destination", type="VARCHAR", description="到着空港", role="dimension"
                    ),
                ],
            ),
        ],
        metrics=[
            MetricDefinition(
                name="flight_count",
                expression="COUNT(flight_id)",
                description="総運航便数",
                synonyms=["便数", "運航数"],
            ),
        ],
    )


@pytest.fixture
def ecommerce_schema() -> SchemaMetadata:
    """Fixture for E-commerce schema with dynamic metrics having calculation expressions."""
    return SchemaMetadata(
        title="E-Commerce Schema",
        tables=[
            TableMetadata(
                name="order_items",
                description="受注明細データ",
                aliases=["order_item", "明細"],
                columns=[
                    ColumnMetadata(
                        name="item_id", type="INTEGER", description="明細ID", role="id"
                    ),
                    ColumnMetadata(
                        name="order_id", type="INTEGER", description="注文ID", role="fk"
                    ),
                    ColumnMetadata(
                        name="price", type="DOUBLE", description="単価", role="metric"
                    ),
                ],
            ),
            TableMetadata(
                name="orders",
                description="注文ヘッダデータ",
                aliases=["order", "注文"],
                columns=[
                    ColumnMetadata(
                        name="order_id", type="INTEGER", description="注文ID", role="id"
                    ),
                    ColumnMetadata(
                        name="customer_id", type="INTEGER", description="顧客ID", role="fk"
                    ),
                ],
            ),
        ],
        metrics=[
            MetricDefinition(
                name="gross_revenue",
                expression="SUM(price * quantity)",
                description="総売上金額（割引前）",
                synonyms=["売上", "総売上"],
            ),
            MetricDefinition(
                name="net_revenue",
                expression="SUM(price * quantity * (1 - discount_rate))",
                description="純売上金額（割引後）",
                synonyms=["売上", "純売上"],
            ),
            MetricDefinition(
                name="order_count",
                expression="COUNT(DISTINCT order_id)",
                description="注文件数",
                synonyms=["件数", "注文数"],
            ),
        ],
    )


class TestGenericTableAmbiguityDetection:
    """Tests for dynamic table competition detection and option generation across domains."""

    def test_flights_competing_table_probabilities_generates_dynamic_issue(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify competing table probabilities dynamically generate dynamic HITLIssue."""
        slots = ParsedSlots(
            primary_table="FLIGHT",
            metric="flight_count",
            needs_clarification=False,
            raw_response={
                "query": "羽田発の路線の便数を集計",
                "table_probs": {
                    "FLIGHT": 0.52,
                    "ROUTE": 0.48,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=flights_schema)
        assert issue is not None
        assert issue.title == "対象テーブルの確認 (FLIGHT vs ROUTE)"
        assert issue.question == "「FLIGHT」と「ROUTE」のどちらを基準に集計しますか？"
        assert len(issue.options) == 2

        opt_a = issue.options[0]
        assert opt_a.key == "A"
        assert opt_a.clarification_text == "FLIGHT"
        assert "運航便明細情報" in opt_a.label
        assert "FLIGHT" in opt_a.label
        assert opt_a.hint == "運航便明細情報"

        opt_b = issue.options[1]
        assert opt_b.key == "B"
        assert opt_b.clarification_text == "ROUTE"
        assert "路線基本情報" in opt_b.label
        assert "ROUTE" in opt_b.label
        assert opt_b.hint == "路線基本情報"

    def test_ecommerce_order_items_vs_orders_competition(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify generic table competition between order_items and orders."""
        slots = ParsedSlots(
            primary_table="order_items",
            metric="gross_revenue",
            needs_clarification=False,
            raw_response={
                "query": "各顧客の売上",
                "primary_probs": {
                    "order_items": 0.50,
                    "orders": 0.46,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=ecommerce_schema)
        assert issue is not None
        assert issue.title == "対象テーブルの確認 (order_items vs orders)"
        assert "order_items" in issue.question and "orders" in issue.question
        assert issue.options[0].clarification_text == "order_items"
        assert issue.options[1].clarification_text == "orders"

    def test_no_issue_when_table_probabilities_have_decisive_winner(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify no ambiguity issue when probability difference is greater than 0.35."""
        slots = ParsedSlots(
            primary_table="FLIGHT",
            metric="flight_count",
            needs_clarification=False,
            raw_response={
                "query": "便一覧",
                "table_probs": {
                    "FLIGHT": 0.88,
                    "ROUTE": 0.10,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=flights_schema)
        assert issue is None

    def test_explicit_table_conflict_in_raw_response(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify ambiguity triggered when table_conflict list is provided in raw_response."""
        slots = ParsedSlots(
            primary_table="FLIGHT",
            metric="flight_count",
            needs_clarification=True,
            raw_response={
                "query": "便と路線",
                "table_conflict": ["FLIGHT", "ROUTE"],
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=flights_schema)
        assert issue is not None
        assert issue.title == "対象テーブルの確認 (FLIGHT vs ROUTE)"
        assert issue.options[0].clarification_text == "FLIGHT"
        assert issue.options[1].clarification_text == "ROUTE"


class TestGenericMetricAmbiguityDetection:
    """Tests for dynamic metric competition detection and option generation with expressions."""

    def test_competing_metric_probabilities_with_expression_hints(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify competing metric probabilities generate dynamic issue with expressions."""
        slots = ParsedSlots(
            primary_table="order_items",
            metric="gross_revenue",
            needs_clarification=False,
            raw_response={
                "query": "先月の売上を集計",
                "metric_probs": {
                    "gross_revenue": 0.51,
                    "net_revenue": 0.49,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=ecommerce_schema)
        assert issue is not None
        assert issue.title == "集計指標の確認 (gross_revenue vs net_revenue)"
        assert issue.question == "計算対象の指標としてどちらを採用しますか？"
        assert len(issue.options) == 2

        opt_a = issue.options[0]
        assert opt_a.key == "A"
        assert opt_a.clarification_text == "gross_revenue"
        assert "総売上金額" in opt_a.label
        assert "SUM(price * quantity)" in opt_a.hint

        opt_b = issue.options[1]
        assert opt_b.key == "B"
        assert opt_b.clarification_text == "net_revenue"
        assert "純売上金額" in opt_b.label
        assert "SUM(price * quantity * (1 - discount_rate))" in opt_b.hint

    def test_competing_metrics_between_sales_and_orders(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify metric competition between gross_revenue and order_count."""
        slots = ParsedSlots(
            primary_table="orders",
            metric="gross_revenue",
            needs_clarification=False,
            raw_response={
                "query": "注文実績の集計",
                "metric_probs": {
                    "gross_revenue": 0.48,
                    "order_count": 0.44,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=ecommerce_schema)
        assert issue is not None
        assert issue.title == "集計指標の確認 (gross_revenue vs order_count)"
        assert issue.options[0].clarification_text == "gross_revenue"
        assert issue.options[1].clarification_text == "order_count"
        assert "COUNT(DISTINCT order_id)" in issue.options[1].hint

    def test_synonym_collision_triggers_metric_issue(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify when slots.metric matches multiple schema metrics with shared synonym '売上'."""
        slots = ParsedSlots(
            primary_table="order_items",
            metric="売上",  # Raw synonym slot
            needs_clarification=True,
            raw_response={
                "query": "売上の推移",
                "ambiguity": "metric",
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=ecommerce_schema)
        assert issue is not None
        assert issue.title == "集計指標の確認 (gross_revenue vs net_revenue)"
        assert issue.options[0].clarification_text == "gross_revenue"
        assert issue.options[1].clarification_text == "net_revenue"

    def test_no_issue_when_metric_probabilities_have_clear_winner(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify no ambiguity issue when metric probability difference exceeds 0.35."""
        slots = ParsedSlots(
            primary_table="order_items",
            metric="gross_revenue",
            needs_clarification=False,
            raw_response={
                "query": "売上",
                "metric_probs": {
                    "gross_revenue": 0.90,
                    "net_revenue": 0.05,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=ecommerce_schema)
        assert issue is None


class TestGenericPromptSynthesis:
    """Tests for dynamic prompt synthesis driven by active HITLIssue options."""

    def test_synthesize_table_option_a_selection(self) -> None:
        issue = HITLIssue(
            title="対象テーブルの確認 (FLIGHT vs ROUTE)",
            question="どちらを基準に集計しますか？",
            options=[
                HITLOption(
                    key="A", label="運航便 (FLIGHT)", hint="便明細", clarification_text="FLIGHT"
                ),
                HITLOption(
                    key="B", label="路線 (ROUTE)", hint="路線情報", clarification_text="ROUTE"
                ),
            ],
            base_query="路線の便数を集計",
        )

        assert (
            HITLManager.synthesize_eval_state("路線の便数を集計", "A", issue=issue)
            == "路線の便数を集計 (集計基準: FLIGHT)"
        )
        assert (
            HITLManager.synthesize_eval_state("路線の便数を集計", "Aで", issue=issue)
            == "路線の便数を集計 (集計基準: FLIGHT)"
        )
        assert (
            HITLManager.synthesize_eval_state("路線の便数を集計", "FLIGHT", issue=issue)
            == "路線の便数を集計 (集計基準: FLIGHT)"
        )

    def test_synthesize_table_option_b_selection(self) -> None:
        issue = HITLIssue(
            title="対象テーブルの確認 (FLIGHT vs ROUTE)",
            question="どちらを基準に集計しますか？",
            options=[
                HITLOption(
                    key="A", label="運航便 (FLIGHT)", hint="便明細", clarification_text="FLIGHT"
                ),
                HITLOption(
                    key="B", label="路線 (ROUTE)", hint="路線情報", clarification_text="ROUTE"
                ),
            ],
            base_query="路線の便数を集計",
        )

        assert (
            HITLManager.synthesize_eval_state("路線の便数を集計", "B", issue=issue)
            == "路線の便数を集計 (集計基準: ROUTE)"
        )
        assert (
            HITLManager.synthesize_eval_state("路線の便数を集計", "Bで", issue=issue)
            == "路線の便数を集計 (集計基準: ROUTE)"
        )
        assert (
            HITLManager.synthesize_eval_state("路線の便数を集計", "ROUTE", issue=issue)
            == "路線の便数を集計 (集計基準: ROUTE)"
        )

    def test_synthesize_metric_option_selection(self) -> None:
        issue = HITLIssue(
            title="集計指標の確認 (gross_revenue vs net_revenue)",
            question="どちらの指標を採用しますか？",
            options=[
                HITLOption(
                    key="A",
                    label="総売上 (gross_revenue)",
                    hint="割引前",
                    clarification_text="gross_revenue",
                ),
                HITLOption(
                    key="B",
                    label="純売上 (net_revenue)",
                    hint="割引後",
                    clarification_text="net_revenue",
                ),
            ],
            base_query="売上を集計",
        )

        assert (
            HITLManager.synthesize_eval_state("売上を集計", "A", issue=issue)
            == "売上を集計 (集計基準: gross_revenue)"
        )
        assert (
            HITLManager.synthesize_eval_state("売上を集計", "Bで", issue=issue)
            == "売上を集計 (集計基準: net_revenue)"
        )

    def test_synthesize_free_form_clarification_with_issue(self) -> None:
        issue = HITLIssue(
            title="対象テーブルの確認",
            question="選択してください",
            options=[
                HITLOption(key="A", label="FLIGHT", hint="", clarification_text="FLIGHT"),
                HITLOption(key="B", label="ROUTE", hint="", clarification_text="ROUTE"),
            ],
            base_query="便数を集計",
        )

        res = HITLManager.synthesize_eval_state("便数を集計", "成田空港発のみ", issue=issue)
        assert res == "便数を集計 (補足指定: 成田空港発のみ)"


class TestSlashSyntaxAndClarifyArgFlow:
    """Tests for slash clarification syntax and explicit clarify CLI arg integration."""

    def test_slash_syntax_skips_ambiguity_check(
        self, flights_schema: SchemaMetadata
    ) -> None:
        """Verify trailing slash clarification skips ambiguity detection."""
        parsed = HITLManager.parse_input("路線の便数 / Aで")
        assert parsed.base_query == "路線の便数"
        assert parsed.clarification == "Aで"

        slots = ParsedSlots(
            primary_table="FLIGHT",
            metric="flight_count",
            needs_clarification=True,
            raw_response={"query": parsed.base_query, "table_probs": {"FLIGHT": 0.5, "ROUTE": 0.5}},
        )

        # check_ambiguity must return None because clarification has already been supplied
        issue = HITLManager.check_ambiguity(
            slots, clarification=parsed.clarification, schema=flights_schema
        )
        assert issue is None

    def test_cli_clarify_arg_skips_ambiguity_check(
        self, ecommerce_schema: SchemaMetadata
    ) -> None:
        """Verify --clarify CLI argument skips ambiguity detection."""
        parsed = HITLManager.parse_input("売上集計", clarify_arg="B")
        assert parsed.base_query == "売上集計"
        assert parsed.clarification == "B"

        slots = ParsedSlots(
            primary_table="order_items",
            metric="gross_revenue",
            needs_clarification=True,
            raw_response={"metric_probs": {"gross_revenue": 0.5, "net_revenue": 0.5}},
        )

        issue = HITLManager.check_ambiguity(
            slots, clarification=parsed.clarification, schema=ecommerce_schema
        )
        assert issue is None


class TestBackwardCompatibilityAndSchemaNone:
    """Tests ensuring full backward compatibility when schema=None or with legacy TPC-H schema."""

    def test_schema_none_table_competition(self) -> None:
        """Verify dynamic table ambiguity works without schema (fallback labels and hints)."""
        slots = ParsedSlots(
            primary_table="custom_tab_1",
            metric="count",
            needs_clarification=False,
            raw_response={
                "query": "データ集計",
                "table_probs": {
                    "custom_tab_1": 0.52,
                    "custom_tab_2": 0.48,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=None)
        assert issue is not None
        assert issue.title == "対象テーブルの確認 (custom_tab_1 vs custom_tab_2)"
        assert issue.options[0].key == "A"
        assert issue.options[0].label == "テーブル: custom_tab_1"
        assert issue.options[0].hint == "custom_tab_1 を基準に集計します"
        assert issue.options[1].key == "B"
        assert issue.options[1].label == "テーブル: custom_tab_2"

    def test_schema_none_metric_competition(self) -> None:
        """Verify dynamic metric ambiguity works without schema (fallback labels and hints)."""
        slots = ParsedSlots(
            primary_table="tab",
            metric="metric_a",
            needs_clarification=False,
            raw_response={
                "query": "指標集計",
                "metric_probs": {
                    "metric_a": 0.51,
                    "metric_b": 0.49,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=None)
        assert issue is not None
        assert issue.title == "集計指標の確認 (metric_a vs metric_b)"
        assert issue.options[0].key == "A"
        assert issue.options[0].label == "指標: metric_a"
        assert issue.options[1].key == "B"
        assert issue.options[1].label == "指標: metric_b"

    def test_legacy_tpch_revenue_exact_wording_preserved(self) -> None:
        """Verify legacy TPC-H sum_revenue ambiguity preserves exact title and wording."""
        slots = ParsedSlots(
            primary_table="lineitem",
            metric="sum_revenue",
            needs_clarification=True,
            raw_response={
                "query": "アジアの顧客の売上",
                "table_probs": {
                    "lineitem": 0.50,
                    "orders": 0.50,
                },
            },
        )

        issue = HITLManager.check_ambiguity(slots, clarification=None, schema=None)
        assert issue is not None
        assert issue.title == "売上の集計基準の確認 (Ambiguous Revenue Definition)"
        assert "商品明細単位（lineitem）と注文伝票単位（orders）" in issue.question
        assert issue.options[0].label == "商品明細売上 (lineitem.l_extendedprice)"
        assert issue.options[1].label == "注文伝票売上 (orders.o_totalprice)"

        # Verify synthesize_eval_state legacy exact output
        res_a = HITLManager.synthesize_eval_state("アジアの顧客の売上", "A", issue=issue)
        assert res_a == "アジアの顧客の売上 (集計基準: lineitem 商品明細売上)"
        res_b = HITLManager.synthesize_eval_state("アジアの顧客の売上", "B", issue=issue)
        assert res_b == "アジアの顧客の売上 (集計基準: orders 注文伝票売上)"
