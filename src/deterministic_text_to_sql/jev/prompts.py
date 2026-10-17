"""Prompt builder and payload generation for TypeSafe Jev API."""

from __future__ import annotations

from typing import Any

from deterministic_text_to_sql.schema.models import ColumnMetadata, SchemaMetadata

# Standard TPC-H fallback constants for backward compatibility
TPCH_PRIMARY_CRITERIA: dict[str, str] = {
    "customer": "顧客・会員情報",
    "orders": "注文ヘッダー",
    "lineitem": "注文明細（売上、商品数量、値引、配送）",
    "part": "部品・製品カタログ",
    "supplier": "仕入先・サプライヤー",
    "partsupp": "部品在庫・仕入原価",
}

TPCH_SECONDARY_CRITERIA: dict[str, str] = {
    "nation": "国情報",
    "region": "地域情報",
    "customer": "顧客情報",
    "supplier": "仕入先情報",
    "part": "部品カタログ",
    "orders": "注文ヘッダー",
    "lineitem": "注文明細",
    "none": "追加テーブルなし",
}

TPCH_DIMENSION_CRITERIA: dict[str, str] = {
    "c_custkey": "顧客別・顧客ID (customer.c_custkey)",
    "c_name": "顧客名 (customer.c_name)",
    "c_mktsegment": "市場セグメント",
    "n_name": "国名",
    "r_name": "地域名",
    "p_name": "部品名",
    "s_name": "仕入先名",
    "o_orderdate": "注文日",
    "none": "全体集計または個別リスト（GROUP BYなし）",
}

METRIC_CRITERIA: dict[str, str] = {
    "count_records": "件数・人数のカウント (COUNT)",
    "sum_revenue": "売上合計 (SUM(extendedprice) または SUM(totalprice))",
    "sum_quantity": "数量合計 (SUM(quantity))",
    "sum_availqty": "在庫数量 (SUM(ps_availqty))",
    "avg_retailprice": "平均価格 (AVG(retailprice))",
    "raw_list": "集計ではなく属性の個別リスト表示",
}

RANKING_CRITERIA: dict[str, str] = {
    "top_5": "上位5件",
    "top_10": "上位10件",
    "none": "指定なし",
}

NUMERIC_COLUMN_CRITERIA: dict[str, str] = {
    "c_acctbal": "顧客の口座残高 (customer.c_acctbal)",
    "ps_availqty": "部品の在庫数量 (partsupp.ps_availqty)",
    "l_discount": "注文明細の値引率 (lineitem.l_discount)",
    "p_retailprice": "部品の小売定価 (part.p_retailprice)",
    "p_size": "部品のサイズ (part.p_size)",
    "none": "なし",
}

NUMERIC_OPERATOR_CRITERIA: dict[str, str] = {
    "GT": "より大きい、超、プラス (>)",
    "GTE": "以上 (>=)",
    "LT": "より小さい、未満、不足 (<)",
    "LTE": "以下 (<=)",
    "EQ": "等しい (=)",
    "none": "なし",
}

EXCLUSION_TARGET_CRITERIA: dict[str, str] = {
    "returns": "返品された注文明細を除外 (lineitem.l_returnflag != 'R')",
    "cancelled": "キャンセルされた注文を除外 (orders.o_orderstatus != 'P')",
    "none": "なし",
}

MASTER_NATION_CRITERIA: dict[str, str] = {
    "JAPAN": "日本 (JAPAN)",
    "UNITED STATES": "アメリカ合衆国 (UNITED STATES)",
    "GERMANY": "ドイツ (GERMANY)",
    "none": "なし",
}

MASTER_REGION_CRITERIA: dict[str, str] = {
    "ASIA": "アジア (ASIA)",
    "EUROPE": "ヨーロッパ (EUROPE)",
    "AMERICA": "アメリカ (AMERICA)",
    "none": "なし",
}

NATION_LABEL_MAP: dict[str, str] = {
    "JAPAN": "日本 (JAPAN)",
    "UNITED STATES": "アメリカ合衆国 (UNITED STATES)",
    "GERMANY": "ドイツ (GERMANY)",
    "FRANCE": "フランス (FRANCE)",
    "UNITED KINGDOM": "イギリス (UNITED KINGDOM)",
    "CHINA": "中国 (CHINA)",
    "INDIA": "インド (INDIA)",
    "BRAZIL": "ブラジル (BRAZIL)",
    "CANADA": "カナダ (CANADA)",
    "RUSSIA": "ロシア (RUSSIA)",
}

REGION_LABEL_MAP: dict[str, str] = {
    "ASIA": "アジア (ASIA)",
    "EUROPE": "ヨーロッパ (EUROPE)",
    "AMERICA": "アメリカ (AMERICA)",
    "AFRICA": "アフリカ (AFRICA)",
    "MIDDLE EAST": "中東 (MIDDLE EAST)",
}


def _is_numeric_column(col: ColumnMetadata) -> bool:
    """Determine whether a column represents a numeric quantity or metric."""
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
    # Heuristic for schemas without explicit datatype (e.g. flights)
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


def _build_dynamic_criteria(
    schema: SchemaMetadata,
) -> tuple[
    dict[str, str],  # primary_criteria
    dict[str, str],  # secondary_criteria
    dict[str, str],  # dimension_criteria
    dict[str, str],  # metric_criteria
    dict[str, str],  # numeric_column_criteria
    dict[str, str],  # exclusion_target_criteria
    dict[str, str],  # master_nation_criteria
    dict[str, str],  # master_region_criteria
]:
    """Dynamically construct criteria dictionaries from provided SchemaMetadata."""
    # 1. Primary & Secondary Tables
    parent_tables = {h.parent_table for h in schema.hierarchies}
    primary_criteria = {
        t.name: t.description or f"{t.name} table"
        for t in schema.tables
        if t.name not in parent_tables
    }
    if not primary_criteria:
        primary_criteria = {t.name: t.description or f"{t.name} table" for t in schema.tables}

    secondary_criteria = {
        t.name: t.description or f"{t.name} table" for t in schema.tables
    }
    secondary_criteria["none"] = "追加テーブルなし"

    # 2. Dimensions
    dimension_criteria: dict[str, str] = {}
    for t in schema.tables:
        for col in t.columns:
            if col.role in ("dimension", "id"):
                desc = (
                    f"{col.description} ({t.name}.{col.name})"
                    if col.description
                    else f"{t.name}.{col.name}"
                )
                dimension_criteria[col.name] = desc
    dimension_criteria["none"] = "全体集計または個別リスト（GROUP BYなし）"

    # 3. Metrics
    metric_criteria: dict[str, str] = {
        "count_records": "件数のカウント (COUNT)",
    }
    if schema.metrics:
        for m in schema.metrics:
            metric_criteria[m.name] = m.description if m.description else m.name
    else:
        for t in schema.tables:
            for col in t.columns:
                if col.role == "metric" or col.agg:
                    col_lower = col.name.lower()
                    if (
                        "sum_revenue" not in metric_criteria
                        and (
                            "price" in col_lower
                            or "revenue" in col_lower
                            or "売上" in col.description
                        )
                    ):
                        metric_criteria["sum_revenue"] = (
                            "売上合計 (SUM(extendedprice) または SUM(totalprice))"
                        )
                    if (
                        "sum_quantity" not in metric_criteria
                        and (
                            "quantity" in col_lower
                            or "qty" in col_lower
                            or "数量" in col.description
                        )
                    ):
                        metric_criteria["sum_quantity"] = f"数量合計 (SUM({col.name}))"
                    if "sum_availqty" not in metric_criteria and (
                        "availqty" in col_lower or "在庫" in col.description
                    ):
                        metric_criteria["sum_availqty"] = f"在庫数量 (SUM({col.name}))"

                    aggs = f" ({', '.join(col.agg)})" if col.agg else ""
                    metric_criteria[col.name] = f"{col.description or col.name}{aggs}"
    metric_criteria["raw_list"] = "集計なしの個別行一覧"

    # 4. Numeric Columns
    numeric_column_criteria: dict[str, str] = {}
    for t in schema.tables:
        for col in t.columns:
            if _is_numeric_column(col):
                desc = (
                    f"{col.description} ({t.name}.{col.name})"
                    if col.description
                    else f"{t.name}.{col.name}"
                )
                numeric_column_criteria[col.name] = desc
    numeric_column_criteria["none"] = "なし"

    # 5. Master Nation & Region
    master_nation_criteria: dict[str, str] = {}
    master_region_criteria: dict[str, str] = {}

    for h in schema.hierarchies:
        for child_val, parent_val in h.child_to_parent.items():
            master_nation_criteria[child_val] = NATION_LABEL_MAP.get(child_val, child_val)
            master_region_criteria[parent_val] = REGION_LABEL_MAP.get(parent_val, parent_val)

    for t in schema.tables:
        for col in t.columns:
            if col.values:
                col_lower = col.name.lower()
                tbl_lower = t.name.lower()
                if "nation" in col_lower or "country" in col_lower or "nation" in tbl_lower:
                    for v in col.values:
                        master_nation_criteria[v] = NATION_LABEL_MAP.get(v, v)
                elif "region" in col_lower or "continent" in col_lower or "region" in tbl_lower:
                    for v in col.values:
                        master_region_criteria[v] = REGION_LABEL_MAP.get(v, v)

    master_nation_criteria["none"] = "なし"
    master_region_criteria["none"] = "なし"

    # 6. Exclusion Target
    exclusion_target_criteria: dict[str, str] = {}
    for t in schema.tables:
        for col in t.columns:
            col_lower = col.name.lower()
            if "return" in col_lower or any("返品" in s for s in col.synonyms):
                exclusion_target_criteria["returns"] = (
                    f"返品された注文明細を除外 ({t.name}.{col.name} != 'R')"
                )
            elif (
                "cancel" in col_lower
                or "orderstatus" in col_lower
                or any("キャンセル" in s for s in col.synonyms)
            ):
                if "order" in t.name.lower() or "order" in col_lower:
                    exclusion_target_criteria["cancelled"] = (
                        f"キャンセルされた注文を除外 ({t.name}.{col.name} != 'P')"
                    )
                else:
                    exclusion_target_criteria["cancelled"] = (
                        f"キャンセルを除外 ({t.name}.{col.name})"
                    )
            elif col.role == "filter" or (col.values and col.role not in ("id", "fk")):
                key = f"exclude_{col.name}"
                exclusion_target_criteria[key] = (
                    f"{col.description or col.name} による除外 ({t.name}.{col.name})"
                )
    exclusion_target_criteria["none"] = "なし"

    return (
        primary_criteria,
        secondary_criteria,
        dimension_criteria,
        metric_criteria,
        numeric_column_criteria,
        exclusion_target_criteria,
        master_nation_criteria,
        master_region_criteria,
    )


def build_jev_payload(
    eval_state: str,
    schema: SchemaMetadata | None = None,
) -> dict[str, Any]:
    """Build the structured JSON payload for TypeSafe Jev System 1 classification API.

    Constructs parallel discriminator tasks adhering to the official Jev API specification
    (`state`, `model: "jev-latest"`, `questions: {...}`).
    Dynamically generates task choices and criteria based on the provided SchemaMetadata,
    or falls back to legacy TPC-H constants if schema is omitted or empty.

    Args:
        eval_state: Natural language user query or prompt string.
        schema: Optional semantic schema metadata to dynamically extract valid table,
            dimension, metric, numeric column, and master criteria. If omitted or empty,
            standard TPC-H fallback choices are used.

    Returns:
        Structured payload dictionary ready for POST /v1/systemone.
    """
    if schema is None or not schema.tables:
        primary_criteria = dict(TPCH_PRIMARY_CRITERIA)
        secondary_criteria = dict(TPCH_SECONDARY_CRITERIA)
        dimension_criteria = dict(TPCH_DIMENSION_CRITERIA)
        metric_criteria = dict(METRIC_CRITERIA)
        numeric_column_criteria = dict(NUMERIC_COLUMN_CRITERIA)
        exclusion_target_criteria = dict(EXCLUSION_TARGET_CRITERIA)
        master_nation_criteria = dict(MASTER_NATION_CRITERIA)
        master_region_criteria = dict(MASTER_REGION_CRITERIA)
    else:
        (
            primary_criteria,
            secondary_criteria,
            dimension_criteria,
            metric_criteria,
            numeric_column_criteria,
            exclusion_target_criteria,
            master_nation_criteria,
            master_region_criteria,
        ) = _build_dynamic_criteria(schema)

    questions: dict[str, Any] = {
        "primary_table": {
            "type": "choice",
            "instructions": "質問の主役となる起点テーブルはどれですか？",
            "criteria": primary_criteria,
        },
        "secondary_table": {
            "type": "choice",
            "instructions": "結合すべき関連テーブルはどれですか？",
            "criteria": secondary_criteria,
        },
        "dimension": {
            "type": "choice",
            "instructions": "集計やグループ化（GROUP BY）の基準ディメンションはどれですか？",
            "criteria": dimension_criteria,
        },
        "metric": {
            "type": "choice",
            "instructions": "計算・集計すべきメトリクスはどれですか？",
            "criteria": metric_criteria,
        },
        "ranking": {
            "type": "choice",
            "instructions": "ランキングや上位件数指定はありますか？",
            "criteria": RANKING_CRITERIA,
        },
        "needs_clarification": {
            "type": "noul",
            "instructions": (
                "質問内容に複数の集計方法やテーブル候補として解釈可能な曖昧さ"
                "（補足確認が必要な状態）がありますか？"
            ),
        },
        "has_numeric_filter": {
            "type": "noul",
            "instructions": (
                "質問文に「数値の比較・閾値条件（〇〇以上、〇〇未満、プラス等）」が"
                "含まれていますか？"
            ),
        },
        "numeric_column": {
            "type": "choice",
            "instructions": "数値条件の対象カラムはどれですか？",
            "criteria": numeric_column_criteria,
        },
        "numeric_operator": {
            "type": "choice",
            "instructions": "数値条件の比較方向（演算子）はどれですか？",
            "criteria": NUMERIC_OPERATOR_CRITERIA,
        },
        "has_exclusion_filter": {
            "type": "noul",
            "instructions": (
                "質問文に「特定の状態（返品、キャンセル等）の除外指示」が"
                "含まれていますか？"
            ),
        },
        "exclusion_target": {
            "type": "choice",
            "instructions": "除外指示の対象ステータスはどれですか？",
            "criteria": exclusion_target_criteria,
        },
        "has_master_filter": {
            "type": "noul",
            "instructions": "質問文に「国名や地域名などのマスターコード指定」が含まれていますか？",
        },
        "master_nation": {
            "type": "choice",
            "instructions": "指定されている国名はどれですか？",
            "criteria": master_nation_criteria,
        },
        "master_region": {
            "type": "choice",
            "instructions": "指定されている地域名はどれですか？",
            "criteria": master_region_criteria,
        },
    }

    return {
        "state": eval_state,
        "model": "jev-latest",
        "questions": questions,
    }
