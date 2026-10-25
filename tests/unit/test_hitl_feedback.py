"""Detroit-style unit tests for Human-in-the-Loop (HITL) ambiguity management and query parsing."""

import pytest
from pydantic import ValidationError

from deterministic_text_to_sql.compiler.models import FilterSlot
from deterministic_text_to_sql.hitl import (
    HITLIssue,
    HITLManager,
    HITLOption,
    ParsedQueryInput,
)
from deterministic_text_to_sql.jev.models import ParsedSlots


class TestHITLModels:
    """Unit tests for HITL data models."""

    def test_hitl_option_creation_and_immutability(self) -> None:
        opt = HITLOption(
            key="A",
            label="商品明細売上 (lineitem.l_extendedprice)",
            hint="明細単位の厳密な売上",
            clarification_text="lineitem",
        )
        assert opt.key == "A"
        assert opt.label == "商品明細売上 (lineitem.l_extendedprice)"
        assert opt.hint == "明細単位の厳密な売上"
        assert opt.clarification_text == "lineitem"

        with pytest.raises(ValidationError):
            opt.key = "B"

    def test_hitl_issue_creation_and_immutability(self) -> None:
        opt_a = HITLOption(key="A", label="Option A", hint="Hint A", clarification_text="a")
        opt_b = HITLOption(key="B", label="Option B", hint="Hint B", clarification_text="b")
        issue = HITLIssue(
            title="売上定義の確認",
            question="どちらを集計しますか？",
            options=[opt_a, opt_b],
            base_query="アジアの顧客の売上",
        )
        assert issue.title == "売上定義の確認"
        assert issue.question == "どちらを集計しますか？"
        assert len(issue.options) == 2
        assert issue.base_query == "アジアの顧客の売上"

        with pytest.raises(ValidationError):
            issue.title = "変更"

    def test_parsed_query_input_creation_and_immutability(self) -> None:
        parsed = ParsedQueryInput(base_query="顧客の売上トップ5", clarification="Aで")
        assert parsed.base_query == "顧客の売上トップ5"
        assert parsed.clarification == "Aで"

        with pytest.raises(ValidationError):
            parsed.base_query = "変更"


class TestHITLManagerParseInput:
    """Unit tests for HITLManager.parse_input slash syntax parsing."""

    def test_parse_trailing_slash_with_space(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5 / Aで")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification == "Aで"

    def test_parse_trailing_slash_without_space(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5/B")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification == "B"

    def test_parse_trailing_slash_with_japanese_keyword(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5 / 明細")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification == "明細"

    def test_parse_query_without_slash(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification is None

    def test_parse_with_clarify_arg(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5", clarify_arg="A")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification == "A"

    def test_clarify_arg_overrides_slash_syntax(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5 / Aで", clarify_arg="B")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification == "B"

    def test_date_slash_is_not_parsed_as_clarification(self) -> None:
        res = HITLManager.parse_input("日付 2024/01/01 の売上")
        assert res.base_query == "日付 2024/01/01 の売上"
        assert res.clarification is None

    def test_us_date_and_ratio_not_parsed_as_clarification(self) -> None:
        res_date = HITLManager.parse_input("日付 10/25/2024")
        assert res_date.base_query == "日付 10/25/2024"
        assert res_date.clarification is None

        res_ratio = HITLManager.parse_input("比率 100 / 200")
        assert res_ratio.base_query == "比率 100 / 200"
        assert res_ratio.clarification is None

    def test_trailing_single_slash_sanitization(self) -> None:
        res = HITLManager.parse_input("アジアの顧客の売上トップ5 /")
        assert res.base_query == "アジアの顧客の売上トップ5"
        assert res.clarification is None

        res_spaces = HITLManager.parse_input("アジアの顧客の売上トップ5 /  ")
        assert res_spaces.base_query == "アジアの顧客の売上トップ5"
        assert res_spaces.clarification is None

    def test_date_query_with_trailing_slash_directive(self) -> None:
        res = HITLManager.parse_input("日付 2024/01/01 の売上 / Aで")
        assert res.base_query == "日付 2024/01/01 の売上"
        assert res.clarification == "Aで"

    def test_empty_and_whitespace_input(self) -> None:
        res1 = HITLManager.parse_input("")
        assert res1.base_query == ""
        assert res1.clarification is None

        res2 = HITLManager.parse_input("   ", clarify_arg="  A  ")
        assert res2.base_query == ""
        assert res2.clarification == "A"


class TestHITLManagerCheckAmbiguity:
    """Unit tests for HITLManager.check_ambiguity."""

    def test_skip_when_clarification_provided(self) -> None:
        slots = ParsedSlots(
            primary_table="orders",
            metric="sum_revenue",
            needs_clarification=True,
        )
        res = HITLManager.check_ambiguity(slots, clarification="Aで")
        assert res is None

    def test_trigger_on_needs_clarification_flag_for_revenue(self) -> None:
        slots = ParsedSlots(
            primary_table="orders",
            metric="sum_revenue",
            needs_clarification=True,
            raw_response={"query": "アジアの顧客の売上"},
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is not None
        assert "売上の集計基準の確認" in issue.title
        assert len(issue.options) == 2
        assert issue.options[0].key == "A"
        assert "lineitem" in issue.options[0].clarification_text
        assert issue.options[1].key == "B"
        assert "orders" in issue.options[1].clarification_text
        assert issue.base_query == "アジアの顧客の売上"

    def test_trigger_on_competing_probabilities_for_revenue(self) -> None:
        # needs_clarification is False, but probabilities are tied (diff <= 0.25)
        slots = ParsedSlots(
            primary_table="lineitem",
            metric="sum_revenue",
            needs_clarification=False,
            raw_response={
                "query": "顧客の売上",
                "table_probs": {
                    "lineitem": 0.48,
                    "orders": 0.45,
                },
            },
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is not None
        assert "売上の集計基準の確認" in issue.title

    def test_no_trigger_when_probabilities_have_clear_winner(self) -> None:
        slots = ParsedSlots(
            primary_table="lineitem",
            metric="sum_revenue",
            needs_clarification=False,
            raw_response={
                "query": "顧客の売上",
                "table_probs": {
                    "lineitem": 0.85,
                    "orders": 0.10,
                },
            },
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is None

    def test_no_trigger_when_third_table_wins_and_lineitem_orders_are_zero(self) -> None:
        """Verify no false positive HITL issue when another table wins decisively."""
        slots = ParsedSlots(
            primary_table="customer",
            metric="count_records",
            needs_clarification=False,
            raw_response={
                "query": "日本の顧客一覧",
                "table_probs": {
                    "customer": 0.96,
                    "nation": 0.04,
                    "lineitem": 0.0,
                    "orders": 0.0,
                    "supplier": 0.0,
                },
            },
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is None

    def test_trigger_generic_ambiguity_issue(self) -> None:
        slots = ParsedSlots(
            primary_table="customer",
            secondary_table="nation",
            metric="count_records",
            needs_clarification=True,
            raw_response={"query": "顧客と国"},
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is not None
        assert "クエリ解釈の確認" in issue.title
        assert issue.options[0].key == "A"
        assert issue.options[1].key == "B"
        assert issue.options[1].clarification_text == "nation"

    def test_trigger_generic_ambiguity_issue_with_none_secondary(self) -> None:
        slots = ParsedSlots(
            primary_table="customer",
            secondary_table="none",
            metric="count_records",
            needs_clarification=True,
            raw_response={"query": "顧客"},
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is not None
        assert "クエリ解釈の確認" in issue.title
        assert issue.options[0].key == "A"
        assert issue.options[1].key == "B"
        assert issue.options[1].clarification_text == "その他"
        assert "その他" in issue.options[1].label

    def test_no_issue_when_no_ambiguity(self) -> None:
        slots = ParsedSlots(
            primary_table="customer",
            metric="count_records",
            needs_clarification=False,
        )
        issue = HITLManager.check_ambiguity(slots, clarification=None)
        assert issue is None


class TestHITLManagerSynthesizeEvalState:
    """Unit tests for HITLManager.synthesize_eval_state."""

    def test_synthesize_without_clarification(self) -> None:
        res = HITLManager.synthesize_eval_state("アジアの顧客の売上トップ5", None)
        assert res == "アジアの顧客の売上トップ5"

        res_empty = HITLManager.synthesize_eval_state("アジアの顧客の売上トップ5", "")
        assert res_empty == "アジアの顧客の売上トップ5"

    def test_synthesize_option_a_variants(self) -> None:
        base = "アジアの顧客の売上トップ5"
        expected = "アジアの顧客の売上トップ5 (集計基準: lineitem 商品明細売上)"

        assert HITLManager.synthesize_eval_state(base, "A") == expected
        assert HITLManager.synthesize_eval_state(base, "a") == expected
        assert HITLManager.synthesize_eval_state(base, "Aで") == expected
        assert HITLManager.synthesize_eval_state(base, "明細") == expected
        assert HITLManager.synthesize_eval_state(base, "lineitem") == expected

    def test_synthesize_option_b_variants(self) -> None:
        base = "アジアの顧客の売上トップ5"
        expected = "アジアの顧客の売上トップ5 (集計基準: orders 注文伝票売上)"

        assert HITLManager.synthesize_eval_state(base, "B") == expected
        assert HITLManager.synthesize_eval_state(base, "b") == expected
        assert HITLManager.synthesize_eval_state(base, "Bで") == expected
        assert HITLManager.synthesize_eval_state(base, "伝票") == expected
        assert HITLManager.synthesize_eval_state(base, "orders") == expected
        assert HITLManager.synthesize_eval_state(base, "注文") == expected

    def test_synthesize_free_form_clarification(self) -> None:
        base = "顧客の売上"
        res = HITLManager.synthesize_eval_state(base, "2024年のデータのみ")
        assert res == "顧客の売上 (補足指定: 2024年のデータのみ)"


class TestHITLManagerFormatSummary:
    """Unit tests for HITLManager.format_hitl_summary."""

    def test_format_hitl_summary_structure(self) -> None:
        slots = ParsedSlots(
            primary_table="customer",
            secondary_table="nation",
            dimension="c_name",
            metric="sum_revenue",
            ranking="top_5",
            needs_clarification=False,
            filter_slots=[
                FilterSlot(
                    table="nation",
                    column="n_name",
                    operator="=",
                    value="JAPAN",
                    display_label="国: 日本",
                ),
            ],
        )

        summary = HITLManager.format_hitl_summary(slots)
        assert summary["primary_table"] == "customer"
        assert summary["secondary_table"] == "nation"
        assert summary["dimension"] == "c_name"
        assert summary["metric"] == "sum_revenue"
        assert summary["ranking"] == "top_5"
        assert summary["needs_clarification"] is False
        assert len(summary["filters"]) == 1
        assert summary["filters"][0]["table"] == "nation"
        assert summary["filters"][0]["column"] == "n_name"
        assert summary["filters"][0]["operator"] == "="
        assert summary["filters"][0]["value"] == "JAPAN"
        assert summary["filters"][0]["display"] == "国: 日本"
