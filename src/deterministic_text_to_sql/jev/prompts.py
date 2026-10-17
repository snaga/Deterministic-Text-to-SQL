"""Prompt builder and payload generation for TypeSafe Jev API."""

from __future__ import annotations

from typing import Any

from deterministic_text_to_sql.schema.models import SchemaMetadata

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


def build_jev_payload(
    eval_state: str,
    schema: SchemaMetadata | None = None,
) -> dict[str, Any]:
    """Build the structured JSON payload for TypeSafe Jev System 1 classification API.

    Constructs parallel discriminator tasks adhering to the official Jev API specification
    (`state`, `model: "jev-latest"`, `questions: {...}`).

    Args:
        eval_state: Natural language user query or prompt string.
        schema: Optional semantic schema metadata to dynamically extract valid table
            and dimension choices. If omitted, standard TPC-H choices are used.

    Returns:
        Structured payload dictionary ready for POST /v1/systemone.
    """
    if schema is not None and schema.tables:
        primary_criteria: dict[str, str] = {
            t.name: t.description or f"{t.name} table"
            for t in schema.tables
            if t.name not in ("nation", "region")
        }
        if not primary_criteria:
            primary_criteria = {t.name: t.description or t.name for t in schema.tables}

        secondary_criteria: dict[str, str] = {
            t.name: t.description or f"{t.name} table" for t in schema.tables
        }
        secondary_criteria["none"] = "追加テーブルなし"

        dimension_criteria: dict[str, str] = {}
        for t in schema.tables:
            for col in t.columns:
                if col.role in ("dimension", "id"):
                    desc = col.description or f"{t.name}.{col.name}"
                    dimension_criteria[col.name] = desc
        dimension_criteria["none"] = "全体集計または個別リスト（GROUP BYなし）"
    else:
        primary_criteria = dict(TPCH_PRIMARY_CRITERIA)
        secondary_criteria = dict(TPCH_SECONDARY_CRITERIA)
        dimension_criteria = dict(TPCH_DIMENSION_CRITERIA)

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
            "criteria": METRIC_CRITERIA,
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
            "criteria": NUMERIC_COLUMN_CRITERIA,
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
            "criteria": EXCLUSION_TARGET_CRITERIA,
        },
        "has_master_filter": {
            "type": "noul",
            "instructions": "質問文に「国名や地域名などのマスターコード指定」が含まれていますか？",
        },
        "master_nation": {
            "type": "choice",
            "instructions": "指定されている国名はどれですか？",
            "criteria": MASTER_NATION_CRITERIA,
        },
        "master_region": {
            "type": "choice",
            "instructions": "指定されている地域名はどれですか？",
            "criteria": MASTER_REGION_CRITERIA,
        },
    }

    return {
        "state": eval_state,
        "model": "jev-latest",
        "questions": questions,
    }
