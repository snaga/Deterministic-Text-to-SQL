"""Slot extraction and whitelist binding logic from Jev classification responses."""

from __future__ import annotations

import contextlib
import re
from typing import Any

from deterministic_text_to_sql.compiler.models import (
    FilterSlot,
    HavingSlot,
    OrderBySlot,
    WindowSlot,
)
from deterministic_text_to_sql.jev.exceptions import JevResponseParseError
from deterministic_text_to_sql.jev.models import ParsedSlots
from deterministic_text_to_sql.schema.models import ColumnMetadata, SchemaMetadata

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

    Supports formats like '1000', '-500', '0.05', '+250', comma-formatted numbers '1,500.5',
    and Japanese unit multipliers like '1000万' (10,000,000) or '1億' (100,000,000).

    Args:
        text: Query or string to extract numbers from.

    Returns:
        List of parsed float values in order of appearance.
    """
    pattern = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s*(?:万|億))?"
    matches = re.finditer(pattern, text)
    results: list[float] = []
    for m in matches:
        raw_val = m.group(0).replace(",", "").strip()
        # Avoid standalone '+' or '-'
        if raw_val in ("+", "-"):
            continue
        multiplier = 1.0
        if raw_val.endswith("億"):
            multiplier = 100_000_000.0
            raw_val = raw_val[:-1].strip()
        elif raw_val.endswith("万"):
            multiplier = 10_000.0
            raw_val = raw_val[:-1].strip()
        try:
            results.append(float(raw_val) * multiplier)
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

    if "answers" in response_data and isinstance(response_data["answers"], dict):
        answers = response_data["answers"]
        for k, v in answers.items():
            if isinstance(v, dict):
                # Choice or noul value
                val = v.get("choice")
                if val is None:
                    val = v.get("noul")
                if val is None:
                    val = v.get("value")
                slots[k] = val
                slots[f"_item_{k}"] = v
            else:
                slots[k] = v
        slots["_answers"] = answers
        if "filter_slots" in response_data and isinstance(response_data["filter_slots"], list):
            slots["filter_slots"] = response_data["filter_slots"]
    elif "results" in response_data:
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


def _is_numeric_column(col: ColumnMetadata) -> bool:
    """Determine whether a column represents a numeric quantity or metric."""
    if col.role in ("id", "fk"):
        return False
    if col.role == "metric":
        return True
    type_upper = col.type.upper()
    numeric_types = (
        "INT",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "INTEGER",
        "DECIMAL",
        "NUMERIC",
        "FLOAT",
        "DOUBLE",
        "REAL",
        "NUMBER",
    )
    if any(nt in type_upper for nt in numeric_types):
        return True
    name_lower = col.name.lower()
    numeric_hints = (
        "nr_",
        "_nr",
        "num_",
        "_num",
        "count",
        "qty",
        "price",
        "cost",
        "amount",
        "bal",
        "seats",
        "capacity",
        "length",
        "delay",
        "distance",
        "duration",
    )
    return any(hint in name_lower for hint in numeric_hints)


def _resolve_column_and_table(
    col_name_or_synonym: str,
    primary_table: str,
    schema: SchemaMetadata | None,
) -> tuple[str, str]:
    """Resolve physical table name and physical column name from column name or synonym.

    Args:
        col_name_or_synonym: Raw column name or semantic synonym.
        primary_table: Preferred default / primary table name.
        schema: Optional SchemaMetadata for lookup.

    Returns:
        Tuple of (physical_table_name, physical_column_name).
    """
    raw_col = col_name_or_synonym.strip()
    clean_col = raw_col.split(".")[-1]

    if schema is not None:
        # 1. Primary table priority
        pt = schema.get_table(primary_table)
        if pt is not None:
            col_meta = pt.get_column(clean_col)
            if col_meta is not None:
                return pt.name, col_meta.name

        # 2. Search other schema tables sequentially
        for tbl in schema.tables:
            if pt is not None and tbl.name == pt.name:
                continue
            col_meta = tbl.get_column(clean_col)
            if col_meta is not None:
                return tbl.name, col_meta.name

        # 3. Check if clean_col matches a metric synonym/name
        metric_def = schema.get_metric(clean_col)
        if metric_def is not None:
            return primary_table, metric_def.name

        # 4. Fallback to primary table and given column name
        return primary_table, clean_col

    # schema is None: legacy TPC-H prefix heuristics
    target_tbl = primary_table
    if clean_col.startswith("c_"):
        target_tbl = "customer"
    elif clean_col.startswith("ps_"):
        target_tbl = "partsupp"
    elif clean_col.startswith("l_"):
        target_tbl = "lineitem"
    elif clean_col.startswith("o_"):
        target_tbl = "orders"
    elif clean_col.startswith("p_"):
        target_tbl = "part"
    elif clean_col.startswith("s_"):
        target_tbl = "supplier"
    elif clean_col.startswith("n_"):
        target_tbl = "nation"
    elif clean_col.startswith("r_"):
        target_tbl = "region"
    return target_tbl, clean_col


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
    4. Numeric literal, comparison operator, master code, and exclusion filter extraction.
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
    raw_primary = str(slots_map.get("primary_table", "")).strip()
    primary_table = raw_primary if raw_primary else "customer"
    if schema is not None:
        table_meta = schema.get_table(primary_table)
        if table_meta is None:
            # Fallback to first schema table if specified primary is invalid
            primary_table = schema.tables[0].name if schema.tables else "customer"
        else:
            primary_table = table_meta.name
    else:
        primary_table = primary_table.lower()

    # 2. Secondary Table Validation
    raw_secondary = str(slots_map.get("secondary_table", "none")).strip()
    secondary_table = "none"
    if raw_secondary and raw_secondary.lower() != "none":
        if schema is not None:
            sec_meta = schema.get_table(raw_secondary)
            if sec_meta is not None:
                secondary_table = sec_meta.name
        else:
            secondary_table = raw_secondary.lower()

    # 3. Dimension Validation (Supports multi-dimension and single-dimension backward compatibility)
    raw_dims_input = slots_map.get("dimensions")
    raw_dim_input = slots_map.get("dimension")
    dim_candidates: list[str] = []

    if isinstance(raw_dims_input, list):
        for d in raw_dims_input:
            if isinstance(d, str):
                dim_candidates.append(d)
    elif isinstance(raw_dims_input, str) and raw_dims_input.strip():
        for part in re.split(r"[,、\s]+", raw_dims_input.strip()):
            if part:
                dim_candidates.append(part)

    if isinstance(raw_dim_input, list):
        for d in raw_dim_input:
            if isinstance(d, str) and d not in dim_candidates:
                dim_candidates.append(d)
    elif isinstance(raw_dim_input, str) and raw_dim_input.strip():
        for part in re.split(r"[,、\s]+", raw_dim_input.strip()):
            if part and part not in dim_candidates:
                dim_candidates.append(part)

    resolved_dimensions: list[str] = []
    for cand in dim_candidates:
        cand_clean = cand.strip()
        if not cand_clean or cand_clean.lower() == "none":
            continue

        if schema is not None:
            # A. Semantic dimension lookup in schema.dimensions (e.g. orders_year)
            dim_meta = schema.get_dimension(cand_clean)
            if dim_meta is not None:
                if dim_meta.name not in resolved_dimensions:
                    resolved_dimensions.append(dim_meta.name)
                continue

            # B. Table column lookup
            clean_col = cand_clean.split(".")[-1]
            dim_tbl, dim_col = _resolve_column_and_table(clean_col, primary_table, schema)
            resolved_tbl = schema.get_table(dim_tbl)
            if resolved_tbl and resolved_tbl.get_column(dim_col) is not None:
                formatted_dim = f"{dim_tbl}.{dim_col}" if "." in cand_clean else dim_col
                if formatted_dim not in resolved_dimensions:
                    resolved_dimensions.append(formatted_dim)
        else:
            clean_col = cand_clean.split(".")[-1]
            target_dim = cand_clean if "." in cand_clean else clean_col
            if target_dim not in resolved_dimensions:
                resolved_dimensions.append(target_dim)

    dimension = resolved_dimensions[0] if resolved_dimensions else "none"

    # 4. Metric Validation
    valid_metrics = {
        "count_records",
        "sum_revenue",
        "sum_quantity",
        "sum_availqty",
        "avg_retailprice",
        "avg_price",
        "sum_acctbal",
        "raw_list",
    }
    raw_metric = str(slots_map.get("metric", "count_records")).strip().lower()
    if schema is not None:
        metric_def = schema.get_metric(raw_metric) if schema.metrics else None
        if metric_def is not None:
            metric = metric_def.name
        elif (
            raw_metric in valid_metrics
            or any(raw_metric.startswith(p) for p in ("sum_", "avg_", "count_", "min_", "max_"))
            or (
                schema.get_table(primary_table)
                and schema.get_table(primary_table).get_column(raw_metric)  # type: ignore[union-attr]
            )
        ):
            metric = raw_metric
        else:
            metric = "count_records"
    else:
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
    raw_needs_clarify = slots_map.get("needs_clarification")
    needs_clarification = False
    if isinstance(raw_needs_clarify, (int, float)):
        if float(raw_needs_clarify) >= 0.45:
            needs_clarification = True
    elif isinstance(raw_needs_clarify, bool) and raw_needs_clarify:
        needs_clarification = True

    raw_ambiguity = str(slots_map.get("ambiguity", "")).lower()
    if "ambiguous" in raw_ambiguity or "clarification" in raw_ambiguity:
        needs_clarification = True

    # Check probabilities difference for sum_revenue between lineitem and orders
    pt_item = slots_map.get("_item_primary_table")
    primary_probs: dict[str, float] = {}
    if (
        isinstance(pt_item, dict)
        and "probabilities" in pt_item
        and isinstance(pt_item["probabilities"], dict)
    ):
        primary_probs = pt_item["probabilities"]
    elif "probabilities" in response_data and isinstance(response_data["probabilities"], dict):
        primary_probs = response_data["probabilities"]
    elif "table_probs" in response_data and isinstance(response_data["table_probs"], dict):
        primary_probs = response_data["table_probs"]

    if metric == "sum_revenue" and primary_probs:
        try:
            lineitem_p = float(primary_probs.get("lineitem", 0.0))
            orders_p = float(primary_probs.get("orders", 0.0))
            if lineitem_p > 0 and orders_p > 0 and abs(lineitem_p - orders_p) < 0.35:
                needs_clarification = True
        except (ValueError, TypeError):
            pass

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

    # 7.2 Structured Jev answers extraction (numeric, exclusion, master nation/region)
    # 7.2.A Numeric filter
    has_num = slots_map.get("has_numeric_filter")
    num_col = str(slots_map.get("numeric_column", "none")).strip()
    num_op = str(slots_map.get("numeric_operator", "none")).strip()
    num_col_item = slots_map.get("_item_numeric_column") or {}
    num_col_conf = (
        float(num_col_item.get("confidence", 0.0)) if isinstance(num_col_item, dict) else 0.0
    )
    has_num_val = (
        float(has_num)
        if isinstance(has_num, (int, float))
        else (0.5 if has_num is True else 0.0)
    )

    op_map = {"GT": ">", "GTE": ">=", "LT": "<", "LTE": "<=", "EQ": "="}

    if (
        num_col != "none"
        and num_op in op_map
        and (has_num_val > 0.35 or num_col_conf > 0.6 or has_num is None)
    ):
        nums = extract_numeric_literals(query)
        if ranking == "top_5" and 5.0 in nums:
            nums.remove(5.0)
        elif ranking == "top_10" and 10.0 in nums:
            nums.remove(10.0)
        val = nums[0] if nums else 0.0

        target_tbl, actual_col = _resolve_column_and_table(num_col, primary_table, schema)

        filter_slots.append(
            FilterSlot(
                table=target_tbl,
                column=actual_col,
                operator=op_map[num_op],
                value=val,
                display_label=f"{target_tbl}.{actual_col} {op_map[num_op]} {val}",
            )
        )

    # 7.2.B Exclusion filter
    has_ex = slots_map.get("has_exclusion_filter")
    target = str(slots_map.get("exclusion_target", "none")).strip()
    ex_item = slots_map.get("_item_exclusion_target") or {}
    ex_conf = float(ex_item.get("confidence", 0.0)) if isinstance(ex_item, dict) else 0.0
    has_ex_val = (
        float(has_ex)
        if isinstance(has_ex, (int, float))
        else (0.5 if has_ex is True else 0.0)
    )

    if target != "none" and (has_ex_val > 0.35 or ex_conf > 0.6 or has_ex is None):
        if schema is not None:
            bound_ex = False
            if target == "returns":
                # Look for lineitem or table with return column or values containing return
                for t in schema.tables:
                    for col in t.columns:
                        col_l = col.name.lower()
                        col_clean = col_l.replace("_", "")
                        has_return_val = bool(
                            col.values
                            and any("RETURN" in v.upper() or v.upper() == "R" for v in col.values)
                        )
                        if (
                            "return" in col_l
                            or "returnflag" in col_clean
                            or any("返品" in s for s in col.synonyms)
                            or has_return_val
                        ):
                            val_ex: Any = "R"
                            if col.values:
                                return_cand = next(
                                    (
                                        v
                                        for v in col.values
                                        if "RETURN" in v.upper() or v.upper() == "R"
                                    ),
                                    col.values[0],
                                )
                                val_ex = return_cand
                            filter_slots.append(
                                FilterSlot(
                                    table=t.name,
                                    column=col.name,
                                    operator="!=",
                                    value=val_ex,
                                    display_label=f"{t.name}.{col.name} != '{val_ex}'",
                                )
                            )
                            bound_ex = True
                            break
                    if bound_ex:
                        break
            elif target == "cancelled":
                # Look for orders or table with cancel/status column or values containing cancel
                for t in schema.tables:
                    for col in t.columns:
                        col_l = col.name.lower()
                        col_clean = col_l.replace("_", "")
                        has_cancel_val = bool(
                            col.values and any("CANCEL" in v.upper() for v in col.values)
                        )
                        if (
                            "cancel" in col_l
                            or "status" in col_l
                            or "orderstatus" in col_clean
                            or any("キャンセル" in s for s in col.synonyms)
                            or has_cancel_val
                        ):
                            val_can: Any = "P"
                            if col.values:
                                cancel_cand = next(
                                    (v for v in col.values if "CANCEL" in v.upper()),
                                    col.values[0],
                                )
                                val_can = cancel_cand
                            filter_slots.append(
                                FilterSlot(
                                    table=t.name,
                                    column=col.name,
                                    operator="!=",
                                    value=val_can,
                                    display_label=f"{t.name}.{col.name} != '{val_can}'",
                                )
                            )
                            bound_ex = True
                            break
                    if bound_ex:
                        break

            if not bound_ex:
                # Fallback to TPC-H defaults if no specific column found
                if target == "returns":
                    filter_slots.append(
                        FilterSlot(
                            table="lineitem",
                            column="l_returnflag",
                            operator="!=",
                            value="R",
                            display_label="lineitem.l_returnflag != 'R'",
                        )
                    )
                elif target == "cancelled":
                    filter_slots.append(
                        FilterSlot(
                            table="orders",
                            column="o_orderstatus",
                            operator="!=",
                            value="P",
                            display_label="orders.o_orderstatus != 'P'",
                        )
                    )
        else:
            # schema is None: legacy TPC-H hardcode
            if target == "returns":
                filter_slots.append(
                    FilterSlot(
                        table="lineitem",
                        column="l_returnflag",
                        operator="!=",
                        value="R",
                        display_label="lineitem.l_returnflag != 'R'",
                    )
                )
            elif target == "cancelled":
                filter_slots.append(
                    FilterSlot(
                        table="orders",
                        column="o_orderstatus",
                        operator="!=",
                        value="P",
                        display_label="orders.o_orderstatus != 'P'",
                    )
                )

    # 7.2.C Master code filter (nation / region or generic hierarchy / values)
    has_mf = slots_map.get("has_master_filter")
    nation = str(slots_map.get("master_nation", "none")).strip()
    region = str(slots_map.get("master_region", "none")).strip()
    nat_item = slots_map.get("_item_master_nation") or {}
    reg_item = slots_map.get("_item_master_region") or {}
    nat_conf = float(nat_item.get("confidence", 0.0)) if isinstance(nat_item, dict) else 0.0
    reg_conf = float(reg_item.get("confidence", 0.0)) if isinstance(reg_item, dict) else 0.0
    has_mf_val = (
        float(has_mf)
        if isinstance(has_mf, (int, float))
        else (0.5 if has_mf is True else 0.0)
    )

    if (nation != "none" or region != "none") and (
        has_mf_val > 0.35 or nat_conf > 0.6 or reg_conf > 0.6 or has_mf is None
    ):
        target_val = nation if nation != "none" else region
        is_nation = nation != "none"
        if schema is not None:
            bound_master = False
            # 1. Search in hierarchies
            for h in schema.hierarchies:
                if is_nation:
                    # Match child
                    for child_key in h.child_to_parent:
                        if child_key.upper() == target_val.upper():
                            filter_slots.append(
                                FilterSlot(
                                    table=h.child_table,
                                    column=h.child_column,
                                    operator="=",
                                    value=child_key,
                                    display_label=f"{h.child_table}.{h.child_column}: {child_key}",
                                )
                            )
                            bound_master = True
                            break
                else:
                    # Match parent
                    for parent_val in h.child_to_parent.values():
                        if parent_val.upper() == target_val.upper():
                            lbl = f"{h.parent_table}.{h.parent_column}: {parent_val}"
                            filter_slots.append(
                                FilterSlot(
                                    table=h.parent_table,
                                    column=h.parent_column,
                                    operator="=",
                                    value=parent_val,
                                    display_label=lbl,
                                )
                            )
                            bound_master = True
                            break
                if bound_master:
                    break

            # 2. Search in column values
            if not bound_master:
                for tbl in schema.tables:
                    for col in tbl.columns:
                        if col.values:
                            for v in col.values:
                                if v.upper() == target_val.upper():
                                    filter_slots.append(
                                        FilterSlot(
                                            table=tbl.name,
                                            column=col.name,
                                            operator="=",
                                            value=v,
                                            display_label=f"{tbl.name}.{col.name}: {v}",
                                        )
                                    )
                                    bound_master = True
                                    break
                        if bound_master:
                            break
                    if bound_master:
                        break

            # 3. Fallback for schema if nation/region table exists (backward compatibility)
            if not bound_master:
                if is_nation and schema.get_table("nation") is not None:
                    filter_slots.append(
                        FilterSlot(
                            table="nation",
                            column="n_name",
                            operator="=",
                            value=target_val,
                            display_label=f"Nation: {target_val}",
                        )
                    )
                elif not is_nation and schema.get_table("region") is not None:
                    filter_slots.append(
                        FilterSlot(
                            table="region",
                            column="r_name",
                            operator="=",
                            value=target_val,
                            display_label=f"Region: {target_val}",
                        )
                    )
        else:
            # schema is None: legacy TPC-H hardcode
            if is_nation:
                filter_slots.append(
                    FilterSlot(
                        table="nation",
                        column="n_name",
                        operator="=",
                        value=nation,
                        display_label=f"Nation: {nation}",
                    )
                )
            else:
                filter_slots.append(
                    FilterSlot(
                        table="region",
                        column="r_name",
                        operator="=",
                        value=region,
                        display_label=f"Region: {region}",
                    )
                )

    # 7.3 Categorical filter candidates from Jev response or query (whitelist fallback)
    filter_candidates: list[str] = []
    raw_filters = slots_map.get("filters")
    if isinstance(raw_filters, list):
        filter_candidates.extend([str(item) for item in raw_filters])
    elif isinstance(raw_filters, str) and raw_filters:
        filter_candidates.append(raw_filters)

    # Dynamically extract filter candidates from query based on schema
    if schema is not None:
        # A. From schema.hierarchies child_to_parent keys & values
        for h in schema.hierarchies:
            for child_key, parent_val in h.child_to_parent.items():
                if child_key.lower() in query.lower():
                    filter_candidates.append(child_key)
                if parent_val.lower() in query.lower():
                    filter_candidates.append(parent_val)

        # B. From schema tables columns values
        for tbl in schema.tables:
            for col in tbl.columns:
                if col.values:
                    for val_item in col.values:
                        if val_item.lower() in query.lower():
                            filter_candidates.append(val_item)

        # C. Backward compatibility for TPC-H schema (nation/region tables)
        if schema.get_table("nation") is not None or schema.get_table("region") is not None:
            for kw in list(TPCH_REGIONS.keys()) + list(TPCH_NATIONS.keys()):
                if kw in query:
                    filter_candidates.append(kw)
    else:
        # schema is None: legacy TPC-H keywords
        for kw in list(TPCH_REGIONS.keys()) + list(TPCH_NATIONS.keys()):
            if kw in query:
                filter_candidates.append(kw)

    # Deduplicate candidate strings
    seen_filters: set[str] = set()
    for cand in filter_candidates:
        cand_clean = cand.strip()
        if not cand_clean or cand_clean.lower() in seen_filters:
            continue
        seen_filters.add(cand_clean.lower())

        if schema is not None:
            bound = False
            # 1. Match against schema.hierarchies
            for h in schema.hierarchies:
                for child_key, parent_val in h.child_to_parent.items():
                    if cand_clean.upper() == child_key.upper():
                        filter_slots.append(
                            FilterSlot(
                                table=h.child_table,
                                column=h.child_column,
                                operator="=",
                                value=child_key,
                                display_label=f"{h.child_table}.{h.child_column}: {child_key}",
                            )
                        )
                        bound = True
                        break
                    elif cand_clean.upper() == parent_val.upper():
                        filter_slots.append(
                            FilterSlot(
                                table=h.parent_table,
                                column=h.parent_column,
                                operator="=",
                                value=parent_val,
                                display_label=f"{h.parent_table}.{h.parent_column}: {parent_val}",
                            )
                        )
                        bound = True
                        break
                if bound:
                    break

            # 2. Match against schema column values (F-JEV-02)
            if not bound:
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

            # 3. Backward compatibility with TPC-H if schema has nation/region tables
            if not bound:
                if cand_clean.upper() in TPCH_REGIONS or cand_clean in TPCH_REGIONS:
                    region_val = TPCH_REGIONS.get(cand_clean.upper(), TPCH_REGIONS.get(cand_clean))
                    if region_val and schema.get_table("region") is not None:
                        reg_tbl = schema.get_table("region")
                        r_col = reg_tbl.get_column("r_name") if reg_tbl else None
                        if not (r_col and r_col.values and region_val not in r_col.values):
                            filter_slots.append(
                                FilterSlot(
                                    table="region",
                                    column="r_name",
                                    operator="=",
                                    value=region_val,
                                    display_label=f"Region: {region_val}",
                                )
                            )
                            bound = True

                if not bound and (cand_clean.upper() in TPCH_NATIONS or cand_clean in TPCH_NATIONS):
                    nation_val = TPCH_NATIONS.get(cand_clean.upper(), TPCH_NATIONS.get(cand_clean))
                    if nation_val and schema.get_table("nation") is not None:
                        filter_slots.append(
                            FilterSlot(
                                table="nation",
                                column="n_name",
                                operator="=",
                                value=nation_val,
                                display_label=f"Nation: {nation_val}",
                            )
                        )
                        bound = True

        else:
            # schema is None: legacy TPC-H logic
            # Region match
            if cand_clean.upper() in TPCH_REGIONS or cand_clean in TPCH_REGIONS:
                region_val = TPCH_REGIONS.get(cand_clean.upper(), TPCH_REGIONS.get(cand_clean))
                if region_val:
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

    # 7.4 Having Slots Construction (F-JEV-03: Aggregate Filter Conditions)
    having_slots: list[HavingSlot] = []

    # 1. Direct having_slots or having list in raw response / slots_map
    raw_having = slots_map.get("having_slots")
    if raw_having is None:
        raw_having = slots_map.get("having")

    if isinstance(raw_having, list):
        for item in raw_having:
            if isinstance(item, HavingSlot):
                having_slots.append(item)
            elif isinstance(item, dict):
                m = str(item.get("metric", "")).strip()
                op = str(item.get("operator", "=")).strip()
                val_val: Any = item.get("value")
                op = op_map.get(op, op)
                if m and val_val is not None:
                    try:
                        v_num = float(val_val)
                        if v_num.is_integer():
                            val_val = int(v_num)
                    except (ValueError, TypeError):
                        pass
                    if schema is not None:
                        metric_def = schema.get_metric(m)
                        if metric_def is not None:
                            m = metric_def.name
                    having_slots.append(HavingSlot(metric=m, operator=op, value=val_val))

    # 2. Structured answers from Jev questions (has_having_filter, having_metric, having_operator)
    has_having = slots_map.get("has_having_filter")
    having_metric_cand = str(slots_map.get("having_metric", "none")).strip()
    having_op_cand = str(slots_map.get("having_operator", "none")).strip()
    having_item = slots_map.get("_item_having_metric") or {}
    having_conf = (
        float(having_item.get("confidence", 0.0)) if isinstance(having_item, dict) else 0.0
    )
    has_having_val = (
        float(has_having)
        if isinstance(has_having, (int, float))
        else (0.5 if has_having is True else 0.0)
    )

    if (
        having_metric_cand != "none"
        and (has_having_val > 0.35 or having_conf > 0.6 or has_having is None)
    ):
        resolved_op = op_map.get(having_op_cand, having_op_cand)
        if resolved_op not in (">=", "<=", ">", "<", "=", "!="):
            resolved_op = ">="

        target_metric = having_metric_cand
        if schema is not None:
            metric_def = schema.get_metric(having_metric_cand)
            if metric_def is not None:
                target_metric = metric_def.name

        val_cand = slots_map.get("having_value")
        if val_cand is not None:
            try:
                v_num = float(val_cand)
                having_val: Any = int(v_num) if v_num.is_integer() else v_num
            except (ValueError, TypeError):
                having_val = val_cand
        else:
            nums = extract_numeric_literals(query)
            if ranking == "top_5" and 5.0 in nums:
                nums.remove(5.0)
            elif ranking == "top_10" and 10.0 in nums:
                nums.remove(10.0)
            if nums:
                n0 = nums[0]
                having_val = int(n0) if n0.is_integer() else n0
            else:
                having_val = 0

        having_slots.append(
            HavingSlot(
                metric=target_metric,
                operator=resolved_op,
                value=having_val,
            )
        )

    # 3. Fallback extraction from query when query mentions aggregate filters
    if not having_slots and (dimension != "none" or len(resolved_dimensions) > 0):
        is_agg_query = any(
            kw in query for kw in ["以上", "以下", "超", "未満", ">=", "<=", ">", "<"]
        ) and any(
            kw in query
            for kw in [
                "売上",
                "金額",
                "数量",
                "回数",
                "合計",
                "平均",
                "件数",
                "revenue",
                "quantity",
                "count",
                "price",
            ]
        )
        if is_agg_query:
            target_metric = metric
            if (
                target_metric not in ("count_records", "raw_list")
                or "件数" in query
                or "回数" in query
            ):
                nums = extract_numeric_literals(query)
                if ranking == "top_5" and 5.0 in nums:
                    nums.remove(5.0)
                elif ranking == "top_10" and 10.0 in nums:
                    nums.remove(10.0)

                if nums:
                    n0 = nums[0]
                    having_val = int(n0) if n0.is_integer() else n0

                    if any(kw in query for kw in [">=", "以上", "at least", "より多い"]):
                        op = ">="
                    elif any(kw in query for kw in ["<=", "以下", "at most", "未満でない"]):
                        op = "<="
                    elif any(kw in query for kw in [">", "超", "より大きい", "greater than"]):
                        op = ">"
                    elif any(kw in query for kw in ["<", "未満", "より小さい", "less than"]):
                        op = "<"
                    else:
                        op = "="

                    if schema is not None:
                        if "売上" in query or "金額" in query:
                            m_def = (
                                schema.get_metric("total_revenue")
                                or schema.get_metric("sum_revenue")
                            )
                            if m_def:
                                target_metric = m_def.name
                        elif "数量" in query:
                            m_def = (
                                schema.get_metric("total_quantity")
                                or schema.get_metric("sum_quantity")
                            )
                            if m_def:
                                target_metric = m_def.name

                    having_slots.append(
                        HavingSlot(
                            metric=target_metric,
                            operator=op,
                            value=having_val,
                        )
                    )

    # Deduplicate having slots while preserving order
    unique_having: list[HavingSlot] = []
    seen_having_keys: set[tuple[str, str, Any]] = set()
    for hs in having_slots:
        h_key = (hs.metric, hs.operator, str(hs.value))
        if h_key not in seen_having_keys:
            seen_having_keys.add(h_key)
            unique_having.append(hs)

    # 7.5 Numeric Literal Filter Extraction (fallback if no numeric/having filter provided)
    has_explicit_filter_slots = bool(slots_map.get("filter_slots"))
    has_numeric_filter = (
        has_explicit_filter_slots
        or any(
            isinstance(fs.value, (int, float)) and fs.operator in (">=", "<=", ">", "<", "=")
            for fs in filter_slots
        )
        or bool(unique_having)
    )
    if not has_numeric_filter:
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
            if schema is not None:
                tbl_meta = schema.get_table(primary_table)
                num_column = ""
                if tbl_meta:
                    # 1. First priority: column with role == 'metric'
                    metric_col = next(
                        (c for c in tbl_meta.columns if c.role == "metric"), None
                    )
                    if metric_col is not None:
                        num_column = metric_col.name
                    else:
                        # 2. Second priority: column with numeric type / hint
                        num_cand = next(
                            (c for c in tbl_meta.columns if _is_numeric_column(c)), None
                        )
                        if num_cand is not None:
                            num_column = num_cand.name
                        elif tbl_meta.columns:
                            num_column = tbl_meta.columns[0].name
                if not num_column:
                    num_column = "amount"
            else:
                # schema is None: legacy TPC-H hardcoded fallback
                num_column = "o_totalprice" if primary_table == "orders" else "s_acctbal"
                if primary_table == "customer":
                    num_column = "c_acctbal"
                elif primary_table == "lineitem":
                    num_column = "l_extendedprice"

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

    # 8. Window Operation Slot Extraction (F-JEV-04 / B-4)
    raw_window_op = slots_map.get("window_operation")
    if (
        raw_window_op is None
        and "answers" in response_data
        and isinstance(response_data["answers"], dict)
    ):
        ans_val = response_data["answers"].get("window_operation")
        if isinstance(ans_val, dict):
            raw_window_op = ans_val.get("choice", ans_val.get("value"))
        else:
            raw_window_op = ans_val
    if raw_window_op is None:
        raw_window_op = response_data.get("window_operation", "none")

    window_op = str(raw_window_op).strip().lower() if raw_window_op else "none"
    valid_window_ops = {"partitioned_rank", "time_shift", "moving_avg", "cumulative"}

    window_slot: WindowSlot | None = None
    if window_op in valid_window_ops:
        # 8.1 Partition By Resolution
        raw_partition = slots_map.get("window_partition_by")
        if (
            raw_partition is None
            and "answers" in response_data
            and isinstance(response_data["answers"], dict)
        ):
            ans_val = response_data["answers"].get("window_partition_by")
            if isinstance(ans_val, dict):
                raw_partition = ans_val.get("choice", ans_val.get("value"))
            else:
                raw_partition = ans_val
        if raw_partition is None:
            raw_partition = response_data.get("window_partition_by")

        partition_by: list[str] = []
        if raw_partition is not None:
            partition_candidates: list[str] = []
            if isinstance(raw_partition, list):
                for p in raw_partition:
                    if isinstance(p, str):
                        partition_candidates.append(p)
            elif isinstance(raw_partition, str) and raw_partition.strip():
                for p in re.split(r"[,、\s]+", raw_partition.strip()):
                    if p:
                        partition_candidates.append(p)

            for cand in partition_candidates:
                cand_clean = cand.strip()
                if not cand_clean or cand_clean.lower() == "none":
                    continue

                if schema is not None:
                    # 1. Check semantic dimension in schema
                    dim_meta = schema.get_dimension(cand_clean)
                    if dim_meta is not None:
                        if dim_meta.name not in partition_by:
                            partition_by.append(dim_meta.name)
                        continue

                    # 2. Resolve column and table
                    pt_name = cand_clean.split(".")[0] if "." in cand_clean else primary_table
                    cand_tbl, cand_col = _resolve_column_and_table(cand_clean, pt_name, schema)
                    tbl_meta = schema.get_table(cand_tbl)
                    if tbl_meta is not None and tbl_meta.get_column(cand_col) is not None:
                        resolved_key = f"{cand_tbl}.{cand_col}"
                        if resolved_key not in partition_by:
                            partition_by.append(resolved_key)
                    # Safe fallback: if column does not exist in schema, skip
                else:
                    pt_name = cand_clean.split(".")[0] if "." in cand_clean else primary_table
                    cand_tbl, cand_col = _resolve_column_and_table(cand_clean, pt_name, None)
                    resolved_key = f"{cand_tbl}.{cand_col}"
                    if resolved_key not in partition_by:
                        partition_by.append(resolved_key)

        # 8.2 Determine Target Metric
        target_metric = metric

        # 8.3 Window Slot Instantiation by Kind
        if window_op == "partitioned_rank":
            # Guard against mutual confusion (F-JEV-04): If partition_by solely consists
            # of the aggregation dimension itself (e.g. partition_by=['c_custkey'] when
            # dimension is 'c_custkey'), it is an overall ranking query, not a window rank.
            norm_dims = {d.split(".")[-1] for d in resolved_dimensions}
            norm_parts = {p.split(".")[-1] for p in partition_by}

            if resolved_dimensions and norm_parts and norm_parts.issubset(norm_dims):
                # Fall back to standard aggregation ranking, retaining ranking slot
                window_slot = None
            else:
                # Extract ranking limit for intra-group top-N
                limit_val: int | None = None
                if "window_limit" in slots_map:
                    with contextlib.suppress(ValueError, TypeError):
                        limit_val = int(slots_map["window_limit"])

                if limit_val is None:
                    # Regex match for natural language (e.g. トップ3, 上位5件, top 3, Top 10)
                    match_obj = re.search(r"(?:トップ|上位|top)\s*(\d+)", query, re.IGNORECASE)
                    if match_obj:
                        with contextlib.suppress(ValueError, TypeError):
                            limit_val = int(match_obj.group(1))

                if limit_val is None:
                    if ranking == "top_5":
                        limit_val = 5
                    elif ranking == "top_10":
                        limit_val = 10
                    elif ranking.startswith("top_"):
                        with contextlib.suppress(ValueError, TypeError, IndexError):
                            limit_val = int(ranking.split("_")[1])

                if limit_val is None:
                    # Fallback to Japanese counter patterns (例: 3顧客, 3件)
                    match_obj = re.search(r"(\d+)\s*(?:件|顧客|人|社|品|商品|行|位)", query)
                    if match_obj:
                        with contextlib.suppress(ValueError, TypeError):
                            limit_val = int(match_obj.group(1))

                if limit_val is None:
                    limit_val = 5  # Safe default fallback

                order_by_slots = [OrderBySlot(column=target_metric, direction="DESC")]
                window_slot = WindowSlot(
                    kind="partitioned_rank",
                    function="ROW_NUMBER",
                    partition_by=partition_by,
                    order_by=order_by_slots,
                    limit=limit_val,
                    target_metric=target_metric,
                )

                # Reset overall ranking limit to avoid dual LIMIT application
                ranking = "none"

        elif window_op == "time_shift":
            window_slot = WindowSlot(
                kind="time_shift",
                function="LAG",
                partition_by=partition_by,
                order_by=[],
                offset=1,
                limit=None,
                target_metric=target_metric,
            )
        elif window_op == "moving_avg":
            window_slot = WindowSlot(
                kind="moving_avg",
                function="AVG",
                partition_by=partition_by,
                order_by=[],
                offset=1,
                limit=None,
                target_metric=target_metric,
            )
        elif window_op == "cumulative":
            window_slot = WindowSlot(
                kind="cumulative",
                function="SUM",
                partition_by=partition_by,
                order_by=[],
                offset=1,
                limit=None,
                target_metric=target_metric,
            )

    return ParsedSlots(
        primary_table=primary_table,
        secondary_table=secondary_table,
        dimension=dimension,
        dimensions=resolved_dimensions,
        metric=metric,
        ranking=ranking,
        needs_clarification=needs_clarification,
        filter_slots=unique_filters,
        having_slots=unique_having,
        window_slot=window_slot,
        raw_response=response_data,
    )
