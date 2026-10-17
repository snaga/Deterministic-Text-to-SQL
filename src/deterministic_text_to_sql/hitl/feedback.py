"""Human-in-the-Loop (HITL) ambiguity detection, slash syntax parsing, and prompt synthesis."""

import re
from typing import Any

from deterministic_text_to_sql.hitl.models import HITLIssue, HITLOption, ParsedQueryInput
from deterministic_text_to_sql.jev.models import ParsedSlots


def _extract_competing_candidates(
    probs: dict[str, Any], threshold: float = 0.35
) -> tuple[str, str, float, float] | None:
    """Extract top two competing candidates if absolute probability difference is <= threshold.

    Args:
        probs: Dictionary mapping candidate string names to numeric probabilities.
        threshold: Maximum difference threshold to consider candidates competing (default 0.35).

    Returns:
        Tuple of (cand1, cand2, p1, p2) sorted by descending probability, or None.
    """
    valid: list[tuple[str, float]] = []
    for k, v in probs.items():
        try:
            val = float(v)
            if val > 0:
                valid.append((str(k), val))
        except (ValueError, TypeError):
            continue

    if len(valid) < 2:
        return None

    valid.sort(key=lambda x: x[1], reverse=True)
    c1, p1 = valid[0]
    c2, p2 = valid[1]
    if abs(p1 - p2) <= threshold:
        return (c1, c2, p1, p2)
    return None


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
    def _build_table_issue(
        cls,
        cand1: str,
        cand2: str,
        schema: Any | None,
        base_query: str,
    ) -> HITLIssue:
        """Build dynamic HITLIssue for competing tables."""
        t1 = schema.get_table(cand1) if schema and hasattr(schema, "get_table") else None
        t2 = schema.get_table(cand2) if schema and hasattr(schema, "get_table") else None

        label_a = f"{t1.description} ({cand1})" if (t1 and t1.description) else f"テーブル: {cand1}"
        hint_a = t1.description if (t1 and t1.description) else f"{cand1} を基準に集計します"

        label_b = f"{t2.description} ({cand2})" if (t2 and t2.description) else f"テーブル: {cand2}"
        hint_b = t2.description if (t2 and t2.description) else f"{cand2} を基準に集計します"

        return HITLIssue(
            title=f"対象テーブルの確認 ({cand1} vs {cand2})",
            question=f"「{cand1}」と「{cand2}」のどちらを基準に集計しますか？",
            options=[
                HITLOption(
                    key="A",
                    label=label_a,
                    hint=hint_a,
                    clarification_text=cand1,
                ),
                HITLOption(
                    key="B",
                    label=label_b,
                    hint=hint_b,
                    clarification_text=cand2,
                ),
            ],
            base_query=base_query,
        )

    @classmethod
    def _build_metric_issue(
        cls,
        m1: str,
        m2: str,
        schema: Any | None,
        base_query: str,
    ) -> HITLIssue:
        """Build dynamic HITLIssue for competing metrics."""
        met1 = schema.get_metric(m1) if schema and hasattr(schema, "get_metric") else None
        met2 = schema.get_metric(m2) if schema and hasattr(schema, "get_metric") else None

        label_a = f"{met1.description} ({m1})" if (met1 and met1.description) else f"指標: {m1}"
        if met1 and met1.expression:
            desc_part = f" ({met1.description})" if met1.description else ""
            hint_a = f"計算式: {met1.expression}{desc_part}"
        elif met1 and met1.description:
            hint_a = met1.description
        else:
            hint_a = f"{m1} を計算対象として集計します"

        label_b = f"{met2.description} ({m2})" if (met2 and met2.description) else f"指標: {m2}"
        if met2 and met2.expression:
            desc_part = f" ({met2.description})" if met2.description else ""
            hint_b = f"計算式: {met2.expression}{desc_part}"
        elif met2 and met2.description:
            hint_b = met2.description
        else:
            hint_b = f"{m2} を計算対象として集計します"

        return HITLIssue(
            title=f"集計指標の確認 ({m1} vs {m2})",
            question="計算対象の指標としてどちらを採用しますか？",
            options=[
                HITLOption(
                    key="A",
                    label=label_a,
                    hint=hint_a,
                    clarification_text=m1,
                ),
                HITLOption(
                    key="B",
                    label=label_b,
                    hint=hint_b,
                    clarification_text=m2,
                ),
            ],
            base_query=base_query,
        )

    @classmethod
    def check_ambiguity(
        cls,
        slots: ParsedSlots,
        clarification: str | None = None,
        schema: Any | None = None,
    ) -> HITLIssue | None:
        """Check if slots contain ambiguity that requires user intervention.

        Intervention is skipped if clarification has already been supplied.
        Ambiguity is triggered when `slots.needs_clarification` is True, or when
        competing probabilities (abs(p1 - p2) <= 0.35) or synonym collisions exist
        between candidate tables or metrics.

        Args:
            slots: Validated ParsedSlots from Jev slot extractor.
            clarification: Optional existing clarification from slash syntax or CLI.
            schema: Optional semantic SchemaMetadata for dynamic option enrichment.

        Returns:
            HITLIssue if ambiguity detected, otherwise None.
        """
        if clarification is not None and clarification.strip():
            return None

        raw = slots.raw_response or {}
        base_query = str(raw.get("query", ""))
        is_ambiguous = slots.needs_clarification

        # 1. Resolve metric probabilities from various possible payload formats
        metric_probs: dict[str, Any] = {}
        if isinstance(raw.get("metric_probs"), dict):
            metric_probs = raw["metric_probs"]
        elif isinstance(raw.get("metrics_probs"), dict):
            metric_probs = raw["metrics_probs"]
        elif (
            isinstance(raw.get("answers"), dict)
            and isinstance(raw["answers"].get("metric"), dict)
            and isinstance(raw["answers"]["metric"].get("probabilities"), dict)
        ):
            metric_probs = raw["answers"]["metric"]["probabilities"]
        elif (
            isinstance(raw.get("slots"), dict)
            and isinstance(raw["slots"].get("_item_metric"), dict)
            and isinstance(raw["slots"]["_item_metric"].get("probabilities"), dict)
        ):
            metric_probs = raw["slots"]["_item_metric"]["probabilities"]

        # 2. Resolve table probabilities from various possible payload formats
        table_probs: dict[str, Any] = {}
        if isinstance(raw.get("table_probs"), dict):
            table_probs = raw["table_probs"]
        elif isinstance(raw.get("primary_probs"), dict):
            table_probs = raw["primary_probs"]
        elif (
            isinstance(raw.get("answers"), dict)
            and isinstance(raw["answers"].get("primary_table"), dict)
            and isinstance(raw["answers"]["primary_table"].get("probabilities"), dict)
        ):
            table_probs = raw["answers"]["primary_table"]["probabilities"]
        elif (
            isinstance(raw.get("slots"), dict)
            and isinstance(raw["slots"].get("_item_primary_table"), dict)
            and isinstance(raw["slots"]["_item_primary_table"].get("probabilities"), dict)
        ):
            table_probs = raw["slots"]["_item_primary_table"]["probabilities"]
        elif isinstance(raw.get("probabilities"), dict):
            probs_candidate = raw["probabilities"]
            if schema and hasattr(schema, "get_metric") and hasattr(schema, "get_table"):
                m_count = sum(1 for k in probs_candidate if schema.get_metric(k) is not None)
                t_count = sum(1 for k in probs_candidate if schema.get_table(k) is not None)
                if m_count > t_count:
                    metric_probs = probs_candidate
                else:
                    table_probs = probs_candidate
            else:
                table_probs = probs_candidate
        elif isinstance(raw.get("scores"), dict):
            table_probs = raw["scores"]

        # 3. Detect metric competition
        competing_metric: tuple[str, str, float, float] | None = None
        if metric_probs:
            competing_metric = _extract_competing_candidates(metric_probs)
            if competing_metric:
                is_ambiguous = True

        explicit_metric_conflict: list[str] = []
        if isinstance(raw.get("metric_conflict"), list) and len(raw["metric_conflict"]) >= 2:
            explicit_metric_conflict = [str(x) for x in raw["metric_conflict"][:2]]
            is_ambiguous = True

        synonym_metric_conflict: list[str] = []
        is_metric_ambiguity = is_ambiguous or raw.get("ambiguity") in ("metric", "metrics")
        if schema and hasattr(schema, "metrics") and is_metric_ambiguity:
            matched_synonym_metrics: list[str] = []
            target_metric_token = slots.metric
            for m in schema.metrics:
                if m.name == target_metric_token or target_metric_token in m.synonyms:
                    matched_synonym_metrics.append(m.name)
            if len(matched_synonym_metrics) >= 2:
                synonym_metric_conflict = matched_synonym_metrics[:2]
                is_ambiguous = True

        # 4. Detect table competition
        competing_table: tuple[str, str, float, float] | None = None
        if table_probs:
            # Check legacy lineitem vs orders explicitly for 100% backward compat
            if "lineitem" in table_probs and "orders" in table_probs:
                try:
                    p_line = float(table_probs["lineitem"])
                    p_ord = float(table_probs["orders"])
                    if abs(p_line - p_ord) <= 0.35:
                        is_ambiguous = True
                        competing_table = ("lineitem", "orders", p_line, p_ord)
                except (ValueError, TypeError):
                    pass
            if not competing_table:
                competing_table = _extract_competing_candidates(table_probs)
                if competing_table:
                    is_ambiguous = True

        explicit_table_conflict: list[str] = []
        if isinstance(raw.get("table_conflict"), list) and len(raw["table_conflict"]) >= 2:
            explicit_table_conflict = [str(x) for x in raw["table_conflict"][:2]]
            is_ambiguous = True
        elif isinstance(raw.get("conflict"), list) and len(raw["conflict"]) >= 2:
            conflict_cands = [str(x) for x in raw["conflict"][:2]]
            if (
                schema is not None
                and hasattr(schema, "get_metric")
                and all(schema.get_metric(c) for c in conflict_cands)
            ):
                explicit_metric_conflict = conflict_cands
            else:
                explicit_table_conflict = conflict_cands
            is_ambiguous = True

        ambiguity_type = str(raw.get("ambiguity", "")).lower()
        if ambiguity_type in ("table", "revenue", "metric", "metrics") or raw.get("conflict"):
            is_ambiguous = True

        if not is_ambiguous:
            return None

        # 5. Build HITLIssue
        # 5.1 Metric Issue Disambiguation
        if (
            competing_metric
            or explicit_metric_conflict
            or synonym_metric_conflict
            or ambiguity_type in ("metric", "metrics")
        ):
            m1, m2 = "", ""
            if competing_metric:
                m1, m2 = competing_metric[0], competing_metric[1]
            elif explicit_metric_conflict:
                m1, m2 = explicit_metric_conflict[0], explicit_metric_conflict[1]
            elif synonym_metric_conflict:
                m1, m2 = synonym_metric_conflict[0], synonym_metric_conflict[1]
            elif schema and hasattr(schema, "metrics") and len(schema.metrics) >= 2:
                m1, m2 = schema.metrics[0].name, schema.metrics[1].name

            if m1 and m2:
                return cls._build_metric_issue(m1, m2, schema, base_query)

        # 5.2 Table Issue Disambiguation
        # Check for legacy TPC-H sum_revenue lineitem vs orders
        is_revenue_ambiguity = (
            slots.metric == "sum_revenue"
            or "revenue" in ambiguity_type
        )
        if is_revenue_ambiguity:
            cand1 = competing_table[0] if competing_table else "lineitem"
            cand2 = competing_table[1] if competing_table else "orders"
            if {cand1.lower(), cand2.lower()} == {"lineitem", "orders"}:
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
                    base_query=base_query,
                )

        if competing_table or explicit_table_conflict or ambiguity_type == "table":
            t1, t2 = "", ""
            if competing_table:
                t1, t2 = competing_table[0], competing_table[1]
            elif explicit_table_conflict:
                t1, t2 = explicit_table_conflict[0], explicit_table_conflict[1]
            elif slots.primary_table and slots.secondary_table and slots.secondary_table != "none":
                t1, t2 = slots.primary_table, slots.secondary_table

            if t1 and t2:
                return cls._build_table_issue(t1, t2, schema, base_query)

        # 5.3 Fallback to generic ambiguity issue
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
            base_query=base_query,
        )

    @classmethod
    def synthesize_eval_state(
        cls,
        base_query: str,
        clarification: str | None,
        issue: HITLIssue | None = None,
    ) -> str:
        """Synthesize natural language prompt for Jev System 1 with clarification context.

        Args:
            base_query: Original base query without slash directive.
            clarification: Disambiguation choice or free-form specification.
            issue: Optional active HITLIssue to dynamically map selected option to context.

        Returns:
            Disambiguated natural language query prompt.
        """
        if clarification is None or not clarification.strip():
            return base_query

        c = clarification.strip()
        c_lower = c.lower()

        # 1. If issue is provided, match against issue options dynamically
        if issue and issue.options:
            matched_option: HITLOption | None = None
            for opt in issue.options:
                k_lower = opt.key.lower()
                is_key_match = (
                    c_lower == k_lower
                    or c_lower == f"{k_lower}で"
                    or c_lower == f"{k_lower} です"
                    or c_lower == f"{k_lower}を選択"
                    or c_lower.startswith(f"{k_lower} ")
                    or c_lower.startswith(f"{k_lower}で")
                )
                if is_key_match:
                    matched_option = opt
                    break

                if opt.clarification_text and opt.clarification_text.lower() in c_lower:
                    matched_option = opt
                    break

            if matched_option is not None:
                # TPC-H legacy wording backward-compatibility
                c_text = matched_option.clarification_text
                lbl = matched_option.label
                if c_text == "lineitem" and "商品明細売上" in lbl:
                    return f"{base_query} (集計基準: lineitem 商品明細売上)"
                if c_text == "orders" and "注文伝票売上" in lbl:
                    return f"{base_query} (集計基準: orders 注文伝票売上)"

                chosen_text = matched_option.clarification_text or matched_option.label
                return f"{base_query} (集計基準: {chosen_text})"

        # 2. Legacy fallback when issue is None:
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

        # 3. Free-form clarification directive
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
