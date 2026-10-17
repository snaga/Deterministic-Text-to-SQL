"""Human-in-the-Loop (HITL) ambiguity detection, slash syntax parsing, and prompt synthesis."""

import re
from typing import Any

from deterministic_text_to_sql.hitl.models import HITLIssue, HITLOption, ParsedQueryInput
from deterministic_text_to_sql.jev.models import ParsedSlots


class HITLManager:
    """Manager for Human-in-the-Loop ambiguity detection, query parsing, and context synthesis."""

    # Pattern for slash directive at query end (e.g. " / Aで", " / A", "/明細", "/ lineitem")
    # Excludes embedded dates where slash is followed immediately by digits without preceding space
    _SLASH_SYNTAX_PATTERN = re.compile(r"(?:\s+/\s*|/(?!\d))([^/]+)$")

    @classmethod
    def parse_input(
        cls,
        raw_input: str,
        clarify_arg: str | None = None,
    ) -> ParsedQueryInput:
        """Parse raw query input separating slash clarification syntax from the base query.

        Supports trailing slash syntax (e.g. 'アジアの顧客の売上トップ5 / Aで')
        as well as explicit CLI clarify argument (`--clarify A`).

        Args:
            raw_input: Raw query string provided by the user.
            clarify_arg: Optional explicit clarify argument from CLI flag.

        Returns:
            ParsedQueryInput with normalized base_query and clarification token.
        """
        trimmed = raw_input.strip()
        if not trimmed:
            normalized_arg = clarify_arg.strip() if clarify_arg and clarify_arg.strip() else None
            return ParsedQueryInput(base_query="", clarification=normalized_arg)

        # Check for trailing slash directive
        match = cls._SLASH_SYNTAX_PATTERN.search(trimmed)
        slash_clarify: str | None = None
        base_query = trimmed

        if match:
            candidate = match.group(1).strip()
            # Guard against date-like patterns (e.g. "2024/01/01", "10/25/2024")
            # and ratios (e.g. "100 / 200")
            if not candidate.isdigit():
                slash_clarify = candidate
                base_query = trimmed[: match.start()].strip()

        # Sanitize single trailing slashes or leftover delimiters
        base_query = base_query.rstrip("/").strip()

        # Prioritize explicit CLI argument if provided, otherwise fallback to slash syntax
        effective_clarification = (
            clarify_arg.strip()
            if clarify_arg and clarify_arg.strip()
            else slash_clarify
        )

        return ParsedQueryInput(
            base_query=base_query,
            clarification=effective_clarification,
        )

    @classmethod
    def check_ambiguity(
        cls,
        slots: ParsedSlots,
        clarification: str | None = None,
    ) -> HITLIssue | None:
        """Check if slots contain ambiguity that requires user intervention.

        Intervention is skipped if clarification has already been supplied.
        Ambiguity is triggered when `slots.needs_clarification` is True, or when
        revenue metric ('sum_revenue') has competing table probabilities between
        lineitem and orders.

        Args:
            slots: Validated ParsedSlots from Jev slot extractor.
            clarification: Optional existing clarification from slash syntax or CLI.

        Returns:
            HITLIssue if ambiguity detected, otherwise None.
        """
        if clarification is not None and clarification.strip():
            return None

        is_ambiguous = slots.needs_clarification

        raw = slots.raw_response or {}
        table_probs = raw.get("table_probs") or raw.get("probabilities") or raw.get("scores") or {}
        if isinstance(table_probs, dict) and "lineitem" in table_probs and "orders" in table_probs:
            try:
                p_line = float(table_probs["lineitem"])
                p_ord = float(table_probs["orders"])
                if abs(p_line - p_ord) <= 0.25:
                    is_ambiguous = True
            except (ValueError, TypeError):
                pass

        if raw.get("ambiguity") in ("table", "revenue", "metric") or raw.get("conflict"):
            is_ambiguous = True

        if not is_ambiguous:
            return None

        # Disambiguation for revenue definitions (lineitem vs orders)
        if slots.metric == "sum_revenue" or "revenue" in str(raw.get("ambiguity", "")).lower():
            return HITLIssue(
                title="売上の集計基準の確認 (Ambiguous Revenue Definition)",
                question="「売上」の集計対象として、商品明細単位（lineitem）と注文伝票単位（orders）のどちらを採用しますか？",
                options=[
                    HITLOption(
                        key="A",
                        label="商品明細売上 (lineitem.l_extendedprice)",
                        hint="明細単位の数量や値引き、顧客別商品売上の厳密な集計に適しています",
                        clarification_text="lineitem",
                    ),
                    HITLOption(
                        key="B",
                        label="注文伝票売上 (orders.o_totalprice)",
                        hint="注文伝票ごとの総額（伝票確定値）を集計します",
                        clarification_text="orders",
                    ),
                ],
                base_query=str(raw.get("query", "")),
            )

        # Generic ambiguity issue
        return HITLIssue(
            title="クエリ解釈の確認 (Query Clarification Required)",
            question="複数の解釈候補が存在します。意図する条件を選択してください。",
            options=[
                HITLOption(
                    key="A",
                    label=f"主要テーブル: {slots.primary_table}",
                    hint=f"{slots.primary_table} を基準に集計します",
                    clarification_text=slots.primary_table,
                ),
                HITLOption(
                    key="B",
                    label=(
                        f"関連テーブル: {slots.secondary_table}"
                        if slots.secondary_table != "none"
                        else "関連テーブル: その他"
                    ),
                    hint="関連テーブルを基準に集計します",
                    clarification_text=(
                        slots.secondary_table
                        if slots.secondary_table != "none"
                        else "その他"
                    ),
                ),
            ],
            base_query=str(raw.get("query", "")),
        )

    @classmethod
    def synthesize_eval_state(
        cls,
        base_query: str,
        clarification: str | None,
    ) -> str:
        """Synthesize natural language prompt for Jev System 1 with clarification context.

        Args:
            base_query: Original base query without slash directive.
            clarification: Disambiguation choice or free-form specification.

        Returns:
            Disambiguated natural language query prompt.
        """
        if clarification is None or not clarification.strip():
            return base_query

        c = clarification.strip()
        c_lower = c.lower()

        # Check for Option A (lineitem / 明細売上)
        is_option_a = (
            c_lower in ("a", "aで", "a です", "aを選択")
            or "明細" in c_lower
            or "lineitem" in c_lower
            or c_lower.startswith("a ")
            or c_lower.startswith("aで")
        )
        if is_option_a:
            return f"{base_query} (集計基準: lineitem 商品明細売上)"

        # Check for Option B (orders / 伝票売上)
        is_option_b = (
            c_lower in ("b", "bで", "b です", "bを選択")
            or "伝票" in c_lower
            or "orders" in c_lower
            or "注文" in c_lower
            or c_lower.startswith("b ")
            or c_lower.startswith("bで")
        )
        if is_option_b:
            return f"{base_query} (集計基準: orders 注文伝票売上)"

        # Free-form clarification directive
        return f"{base_query} (補足指定: {c})"

    @classmethod
    def format_hitl_summary(cls, slots: ParsedSlots) -> dict[str, Any]:
        """Format parsed slots into a structured summary dictionary for CLI and AI agents.

        Args:
            slots: Validated ParsedSlots instance.

        Returns:
            Human-readable and machine-readable slot summary dictionary.
        """
        filters_summary = [
            {
                "table": f.table,
                "column": f.column,
                "operator": f.operator,
                "value": f.value,
                "display": f.display_label or f"{f.table}.{f.column} {f.operator} {f.value}",
            }
            for f in slots.filter_slots
        ]

        return {
            "primary_table": slots.primary_table,
            "secondary_table": slots.secondary_table,
            "dimension": slots.dimension,
            "metric": slots.metric,
            "ranking": slots.ranking,
            "filters": filters_summary,
            "needs_clarification": slots.needs_clarification,
        }
