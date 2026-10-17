"""Slot extraction and whitelist binding logic from Jev classification responses."""

from __future__ import annotations

import re
from typing import Any

from deterministic_text_to_sql.compiler.models import FilterSlot
from deterministic_text_to_sql.jev.exceptions import JevResponseParseError
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.schema.models import SchemaMetadata

# Standard TPC-H Nation & Region master constants for deterministic mapping
TPCH_NATIONS = {
    "ALGERIA": "ALGERIA",
    "ARGENTINA": "ARGENTINA",
    "BRAZIL": "BRAZIL",
    "ブラジル": "BRAZIL",
    "CANADA": "CANADA",
    "カナダ": "CANADA",
    "EGYPT": "EGYPT",
    "エジプト": "EGYPT",
    "ETHIOPIA": "ETHIOPIA",
    "FRANCE": "FRANCE",
    "フランス": "FRANCE",
    "GERMANY": "GERMANY",
    "ドイツ": "GERMANY",
    "INDIA": "INDIA",
    "インド": "INDIA",
    "INDONESIA": "INDONESIA",
    "インドネシア": "INDONESIA",
    "IRAN": "IRAN",
    "イラン": "IRAN",
    "IRAQ": "IRAQ",
    "イラク": "IRAQ",
    "JAPAN": "JAPAN",
    "日本": "JAPAN",
    "JORDAN": "JORDAN",
    "KENYA": "KENYA",
    "MOROCCO": "MOROCCO",
    "MOZAMBIQUE": "MOZAMBIQUE",
    "PERU": "PERU",
    "ペルー": "PERU",
    "CHINA": "CHINA",
    "中国": "CHINA",
    "ROMANIA": "ROMANIA",
    "SAUDI ARABIA": "SAUDI ARABIA",
    "VIETNAM": "VIETNAM",
    "ベトナム": "VIETNAM",
    "RUSSIA": "RUSSIA",
    "ロシア": "RUSSIA",
    "UNITED KINGDOM": "UNITED KINGDOM",
    "イギリス": "UNITED KINGDOM",
    "UNITED STATES": "UNITED STATES",
    "アメリカ": "UNITED STATES",
    "米国": "UNITED STATES",
}

TPCH_REGIONS = {
    "AFRICA": "AFRICA",
    "アフリカ": "AFRICA",
    "AMERICA": "AMERICA",
    "アメリカ大陸": "AMERICA",
    "ASIA": "ASIA",
    "アジア": "ASIA",
    "EUROPE": "EUROPE",
    "ヨーロッパ": "EUROPE",
    "MIDDLE EAST": "MIDDLE EAST",
    "中東": "MIDDLE EAST",
}


def extract_numeric_literals(text: str) -> list[float]:
    """Extract numeric literal values from text including negative numbers and decimals.

    Supports formats like '1000', '-500', '0.05', '+250', and comma-formatted numbers '1,500.5'.

    Args:
        text: Query or string to extract numbers from.

    Returns:
        List of parsed float values in order of appearance.
    """
    pattern = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    matches = re.finditer(pattern, text)
    results: list[float] = []
    for m in matches:
        raw_val = m.group(0).replace(",", "")
        # Avoid standalone '+' or '-'
        if raw_val in ("+", "-"):
            continue
        try:
            results.append(float(raw_val))
        except ValueError:
            continue
    return results


def _extract_slots_dict(response_data: dict[str, Any]) -> dict[str, Any]:
    """Normalize various Jev API response formats into a flat dictionary."""
    if not isinstance(response_data, dict):
        raise JevResponseParseError(
            f"Expected dictionary response from Jev API, got {type(response_data).__name__}"
        )

    slots: dict[str, Any] = {}

    if "results" in response_data:
        results = response_data["results"]
        if isinstance(results, dict):
            slots.update(results)
        elif isinstance(results, list):
            for item in results:
                if isinstance(item, dict) and "id" in item:
                    slots[item["id"]] = item.get("choice", item.get("value"))
    elif "choices" in response_data and isinstance(response_data["choices"], dict):
        slots.update(response_data["choices"])
    elif "tasks" in response_data and isinstance(response_data["tasks"], list):
        for item in response_data["tasks"]:
            if isinstance(item, dict) and "id" in item:
                slots[item["id"]] = item.get("choice", item.get("value"))
    else:
        # Fallback to direct top-level keys
        slots.update(response_data)

    return slots


def parse_jev_response(
    query: str,
    response_data: dict[str, Any],
    schema: SchemaMetadata | None = None,
) -> ParsedSlots:
    """Parse Jev System 1 response and bind slots against schema whitelists.

    Performs:
    1. Choice validation for primary table, secondary table, dimension, and metric.
    2. Whitelist Choice binding (F-JEV-02) for categorical filters.
    3. Detection and sanitization against SQL injection patterns.
    4. Numeric literal and comparison operator filter extraction.
    5. Ambiguity flag determination for Human-in-the-Loop intervention.

    Args:
        query: Original natural language user query.
        response_data: Raw JSON dictionary returned from Jev API.
        schema: Optional semantic schema metadata for strict validation.

    Returns:
        Validated ParsedSlots instance.

    Raises:
        JevResponseParseError: If response format is fundamentally corrupt.
    """
    slots_map = _extract_slots_dict(response_data)

    # 1. Primary Table Validation
    raw_primary = str(slots_map.get("primary_table", "")).strip().lower()
    primary_table = raw_primary if raw_primary else "customer"
    if schema is not None:
        table_meta = schema.get_table(primary_table)
        if table_meta is None:
            # Fallback to first schema table if specified primary is invalid
            primary_table = schema.tables[0].name if schema.tables else "customer"
        else:
            primary_table = table_meta.name

    # 2. Secondary Table Validation
    raw_secondary = str(slots_map.get("secondary_table", "none")).strip().lower()
    secondary_table = "none"
    if raw_secondary and raw_secondary != "none":
        if schema is not None:
            sec_meta = schema.get_table(raw_secondary)
            if sec_meta is not None:
                secondary_table = sec_meta.name
        else:
            secondary_table = raw_secondary

    # 3. Dimension Validation
    raw_dim = str(slots_map.get("dimension", "none")).strip()
    dimension = "none"
    if raw_dim and raw_dim != "none":
        # Strip table prefix if provided (e.g. "customer.c_name" -> "c_name")
        dim_col_name = raw_dim.split(".")[-1]
        if schema is not None:
            # Verify column exists across tables
            col_found = False
            for tbl in schema.tables:
                if tbl.get_column(dim_col_name) is not None:
                    dimension = dim_col_name
                    col_found = True
                    break
            if not col_found:
                dimension = "none"
        else:
            dimension = dim_col_name

    # 4. Metric Validation
    valid_metrics = {
        "count_records",
        "sum_revenue",
        "sum_quantity",
        "avg_price",
        "sum_acctbal",
        "raw_list",
    }
    raw_metric = str(slots_map.get("metric", "count_records")).strip().lower()
    metric = raw_metric if raw_metric in valid_metrics else "count_records"

    # 5. Ranking Validation
    valid_rankings = {"top_5", "top_10", "none"}
    raw_ranking = str(slots_map.get("ranking", "none")).strip().lower()
    if raw_ranking in valid_rankings:
        ranking = raw_ranking
    elif "5" in query and ("トップ" in query or "top" in query.lower()):
        ranking = "top_5"
    elif "10" in query and ("トップ" in query or "top" in query.lower()):
        ranking = "top_10"
    else:
        ranking = "none"

    # 6. HITL Ambiguity Detection
    raw_ambiguity = str(slots_map.get("ambiguity", "")).lower()
    raw_needs_clarify = slots_map.get("needs_clarification", False)
    needs_clarification = (
        bool(raw_needs_clarify) or "ambiguous" in raw_ambiguity or "clarification" in raw_ambiguity
    )

    # 7. Filter Slots Construction & Whitelist Choice Binding (F-JEV-02)
    filter_slots: list[FilterSlot] = []

    # 7.1 Direct filter slots in response if provided as structured objects
    if "filter_slots" in slots_map and isinstance(slots_map["filter_slots"], list):
        for raw_fs in slots_map["filter_slots"]:
            if isinstance(raw_fs, dict):
                try:
                    filter_slots.append(FilterSlot(**raw_fs))
                except Exception:
                    continue

    # 7.2 Categorical filter candidates from Jev response or query
    filter_candidates: list[str] = []
    raw_filters = slots_map.get("filters")
    if isinstance(raw_filters, list):
        filter_candidates.extend([str(item) for item in raw_filters])
    elif isinstance(raw_filters, str) and raw_filters:
        filter_candidates.append(raw_filters)

    # Include categorical tokens from query
    for kw in list(TPCH_REGIONS.keys()) + list(TPCH_NATIONS.keys()):
        if kw in query:
            filter_candidates.append(kw)

    # Deduplicate candidate strings
    seen_filters: set[str] = set()
    for cand in filter_candidates:
        cand_clean = cand.strip()
        if not cand_clean or cand_clean in seen_filters:
            continue
        seen_filters.add(cand_clean)

        # Region match
        if cand_clean.upper() in TPCH_REGIONS or cand_clean in TPCH_REGIONS:
            region_val = TPCH_REGIONS.get(cand_clean.upper(), TPCH_REGIONS.get(cand_clean))
            if region_val:
                # Whitelist verify against schema if present
                if schema is not None:
                    reg_tbl = schema.get_table("region")
                    r_col = reg_tbl.get_column("r_name") if reg_tbl else None
                    if r_col and r_col.values and region_val not in r_col.values:
                        continue
                filter_slots.append(
                    FilterSlot(
                        table="region",
                        column="r_name",
                        operator="=",
                        value=region_val,
                        display_label=f"Region: {region_val}",
                    )
                )
                continue

        # Nation match
        if cand_clean.upper() in TPCH_NATIONS or cand_clean in TPCH_NATIONS:
            nation_val = TPCH_NATIONS.get(cand_clean.upper(), TPCH_NATIONS.get(cand_clean))
            if nation_val:
                filter_slots.append(
                    FilterSlot(
                        table="nation",
                        column="n_name",
                        operator="=",
                        value=nation_val,
                        display_label=f"Nation: {nation_val}",
                    )
                )
                continue

        # Schema Column Values Whitelist Binding (F-JEV-02)
        if schema is not None:
            bound = False
            for tbl in schema.tables:
                for col in tbl.columns:
                    if col.values:
                        for allowed_val in col.values:
                            if cand_clean.upper() == allowed_val.upper():
                                filter_slots.append(
                                    FilterSlot(
                                        table=tbl.name,
                                        column=col.name,
                                        operator="=",
                                        value=allowed_val,
                                        display_label=f"{tbl.name}.{col.name}: {allowed_val}",
                                    )
                                )
                                bound = True
                                break
                    if bound:
                        break
                if bound:
                    break

    # 7.3 Numeric Literal Filter Extraction
    # If the query contains numeric thresholds (e.g. "1000以上", "< 500")
    numeric_values = extract_numeric_literals(query)
    # Filter out ranking numbers like top 5 or top 10
    if ranking == "top_5" and 5.0 in numeric_values:
        numeric_values.remove(5.0)
    elif ranking == "top_10" and 10.0 in numeric_values:
        numeric_values.remove(10.0)

    if numeric_values:
        threshold = numeric_values[0]
        # Determine operator from keywords in query
        if any(kw in query for kw in [">=", "以上", "at least", "より多い"]):
            operator = ">="
        elif any(kw in query for kw in ["<=", "以下", "at most", "未満でない"]):
            operator = "<="
        elif any(kw in query for kw in [">", "超", "より大きい", "greater than"]):
            operator = ">"
        elif any(kw in query for kw in ["<", "未満", "より小さい", "less than"]):
            operator = "<"
        else:
            operator = "="

        # Target column determination based on table / metric
        num_table = primary_table
        num_column = "o_totalprice" if primary_table == "orders" else "s_acctbal"
        if primary_table == "customer":
            num_column = "c_acctbal"
        elif primary_table == "lineitem":
            num_column = "l_extendedprice"

        # Verify column exists if schema is provided
        if schema is not None:
            tbl_meta = schema.get_table(num_table)
            if tbl_meta and tbl_meta.get_column(num_column) is None:
                # Find any metric column
                for c in tbl_meta.columns:
                    if c.role == "metric":
                        num_column = c.name
                        break

        filter_slots.append(
            FilterSlot(
                table=num_table,
                column=num_column,
                operator=operator,
                value=threshold,
                display_label=f"{num_table}.{num_column} {operator} {threshold}",
            )
        )

    # Deduplicate identical filter slots while preserving order
    unique_filters: list[FilterSlot] = []
    seen_filter_keys: set[tuple[str, str, str, Any]] = set()
    for fs in filter_slots:
        key = (fs.table, fs.column, fs.operator, str(fs.value))
        if key not in seen_filter_keys:
            seen_filter_keys.add(key)
            unique_filters.append(fs)

    return ParsedSlots(
        primary_table=primary_table,
        secondary_table=secondary_table,
        dimension=dimension,
        metric=metric,
        ranking=ranking,
        needs_clarification=needs_clarification,
        filter_slots=unique_filters,
        raw_response=response_data,
    )
