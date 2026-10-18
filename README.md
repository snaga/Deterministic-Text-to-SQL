# Deterministic Text-to-SQL Engine 🚀✨

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![SQL Engine](https://img.shields.io/badge/AST-sqlglot-brightgreen.svg)](https://github.com/tobymao/sqlglot)
[![Classifier](https://img.shields.io/badge/System1-TypeSafe%20Jev-orange.svg)](https://typesafe.ai)
[![Semantic Model: Apache Ossie](https://img.shields.io/badge/semantic%20model-Apache%20Ossie-red.svg)](https://github.com/apache/ossie)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Type Checked: Mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)

TypeSafe AI の System 1 特化型超高速判別モデル「**Jev**」と型安全 SQL AST ビルダー「**sqlglot**」を連携させ、自然言語の質問から**高信頼・決定論的な SQL をミリ秒オーダー（<0.5ms）で自動合成する** Python 製 Text-to-SQL エンジンおよび CLI です。

オープン標準メタデータ仕様 **[Apache Ossie (incubating) v0.2.0.dev0](https://github.com/apache/ossie)** のセマンティックモデル（YAML / JSON）をネイティブサポートしており、**コード改修を一切行うことなく、モデル定義ファイルを差し替えるだけで任意の業務データベースへ即時適応可能**です。

LLM に直接 SQL 文字列を書かせず、**セマンティック多変量スロット判別**、**外部キー最短経路グラフ探索（NetworkX）**、**数学的包摂による階層オントロジー縮退**、および **sqlglot 型安全 AST 構築** を完全に分離することで、**ハルシネーション（幻覚）と SQL 構文エラーを物理的に遮断**します。

---

## 🌟 主な特徴

1. **決定論的 AST アセンブリ (ハルシネーション率 0%)**:
   - 文字列連結ではなく、`sqlglot.expressions` の AST ノードとして SQL を純粋関数的に構築。
   - スキーマ定義に存在しないテーブルやカラムの混入を物理的に排除。
2. **Apache Ossie (incubating) ネイティブ対応 & ハードコード全廃**:
   - オープン標準のセマンティックモデル（YAML / JSON）を直接読み込み、テーブル、リレーション、指標（Semantic Metrics）、同義語（Synonyms）を完全スキーマ駆動で解釈。
3. **グラフベースの最短経路 JOIN 解決**:
   - NetworkX により、外部キー（単一・複合）の結合リレーションを無向/有向グラフ化。
   - クエリで参照されたテーブル群から最短パス（Dijkstra）を自動探索し、適切な `JOIN ... ON` 句を自動合成。
4. **階層オントロジー縮退（数学的包摂）**:
   - 「地方」と「都道府県」のように親子関係にあるフィルタが同時に指定された場合、子条件の確定を検知して親条件および不要なテーブル結合を自動的に剪定。
5. **Human-in-the-Loop (HITL) 動的曖昧性検知**:
   - 指標の競合（例: 売上定義が複数存在）や確率競合を自動検知し、スキーマメタデータから選択肢を動的生成して人間に確認を要求。
   - スラッシュ指示構文（`売上トップ5 / Aで`）や対話プロンプトにより即座に曖昧性を解決。
6. **AI エージェント & 人間オペレータ向けデュアル CLI**:
   - **AI エージェント向け**: 装飾ゼロの完全構造化 JSON 出力（`--json`）とステータスコード規約（`0`: 正常, `1`: エラー, `2`: HITL要求）。
   - **人間オペレータ向け**: Rich による美しいシンタックスハイライト、対話型 REPL セッション（`interactive`）。
7. **マルチダイアレクト対応**:
   - **PostgreSQL**, **DuckDB**, **SQLite**, **Snowflake**, **BigQuery** への型安全トランスパイル。

---

## 🔄 ハードコード全廃・汎用化アーキテクチャ (Before / After)

本エンジンは、従来の TPC-H 専用プロトタイプから **Apache Ossie 準拠の完全汎用エンジン** へと進化しました。内部コードから特定ドメイン依存のロジックや定数は完全に排除されています。

| 設計項目 | 以前の実装 (Before: TPC-H 密結合) | 現在の実装 (After: Apache Ossie 汎用化) |
| :--- | :--- | :--- |
| **セマンティック定義** | 独自仕様のカスタム JSON 形式に依存 | オープン標準 **Apache Ossie (YAML / JSON)** をネイティブサポート（旧 JSON も 100% 後方互換） |
| **Jev 判別プロンプト** | テーブル名や指標候補をコード内に固定定数化 | スキーマの `datasets`, `fields`, `metrics`, `synonyms` から **Jev Criteria を動的自動生成** |
| **カラム・テーブル逆引き** | `c_`, `o_`, `l_`, `p_`, `s_` 等の特定プレフィックス判定 | `ai_context.synonyms` およびスキーマグラフ探索による**プレフィックス非依存の完全汎用解決** |
| **カテゴリ値バインディング** | 国名や地域名（JAPAN, ASIA 等）を固定辞書化 | 各フィールドの `values`（値ホワイトリスト）から安全かつ動的にバインディング |
| **階層オントロジー縮退** | 国と地域（nation/region）の TPC-H 固有関係に固定 | `custom_extensions.hierarchies` に定義された**任意の親子関係（都道府県/地方、部署/事業部等）に汎用適用** |
| **複雑指標 (Metrics) 計算** | 単一カラムの単純集約（`SUM(col)` 等）に限定 | `metrics` の `expression`（例: `SUM(l_extendedprice * (1 - l_discount))`）により**多カラム複合計算式を自在に展開** |
| **HITL 曖昧性確認質問** | `lineitem` vs `orders` の売上質問を静的生成 | 確率競合（`p1 - p2 <= 0.35`）や同義語衝突を検知し、**スキーマ定義から質問・選択肢ラベルを動的合成** |

---

## 📄 Apache Ossie セマンティックモデル定義 (YAML)

本エンジンが推奨標準として採用している **Apache Ossie (incubating) v0.2.0.dev0** セマンティックモデル（YAML）の定義構造例です。

```yaml
version: 0.2.0.dev0
name: sales_semantic_model
description: "EC販売・注文分析セマンティックモデル"

# 1. データセット（テーブル・ビュー）とフィールド定義
datasets:
  - name: orders
    source: orders
    description: "注文伝票ヘッダー情報"
    primary_key:
      - o_orderkey
    ai_context:
      synonyms: ["注文", "オーダー", "orders"]
    fields:
      - name: o_orderkey
        datatype: BIGINT
        role: id
      - name: o_custkey
        datatype: BIGINT
        role: fk
      - name: o_totalprice
        datatype: DOUBLE
        role: metric
        ai_context:
          synonyms: ["伝票金額", "合計金額"]
      - name: o_orderdate
        datatype: DATE
        role: date_filter

# 2. 外部キーリレーション（Graph 最短経路 JOIN の解決に使用）
relationships:
  - name: rel_orders_customer
    source_dataset: orders
    target_dataset: customer
    relationship_type: many_to_one
    relationship_fields:
      - source_field: o_custkey
        target_field: c_custkey

# 3. セマンティック指標（ビジネス計算式を AST に直接展開）
metrics:
  - name: total_revenue
    description: "値引き後実質売上合計"
    datatype: DOUBLE
    expression:
      dialects:
        - dialect: ANSI_SQL
          expression: "SUM(lineitem.l_extendedprice * (1 - lineitem.l_discount))"
    ai_context:
      synonyms: ["売上", "合計売上", "純売上", "revenue"]

# 4. カスタム拡張: 階層オントロジー（数学的包摂による自動縮退ルール）
custom_extensions:
  - vendor_name: "DETERMINISTIC_TEXT_TO_SQL"
    data:
      hierarchies:
        - parent_table: "region"
          parent_column: "r_name"
          child_table: "nation"
          child_column: "n_name"
          child_to_parent:
            JAPAN: ASIA
            CHINA: ASIA
            GERMANY: EUROPE
            UNITED STATES: AMERICA
```

> [!NOTE]
> 従来の `SchemaMetadata` 形式（JSON）も完全な後方互換性を保持しており、`.yaml`, `.yml`, `.json` のいずれの形式も自動判別して透過的に読み込みます。

---

## 🚀 クイックスタート

### インストール

```bash
# リポジトリをクローンしてインストール
git clone https://github.com/your-org/deterministic-text-to-sql.git
cd deterministic-text-to-sql
pip install -e .
```

### 環境変数の設定

TypeSafe Jev API キーを設定します（開発・オフラインデモ環境ではモック動作可能）：

```bash
export TYPESAFE_API_KEY="your_typesafe_api_key"
```

---

## 🎓 TPC-H チュートリアル & サンプル (`examples/tpch/`)

本リポジトリには、標準 TPC-H ベンチマーク（8テーブル）のセマンティックモデルと、インメモリ SQLite を使って即座に動作を体験できるチュートリアルキットが同梱されています。

```bash
# チュートリアルスクリプトの実行（APIキー未設定でも自動モックで100%実動）
python examples/tpch/run_tutorial.py
```

### チュートリアルに含まれる内容:
- **`examples/tpch/tpch_semantic_model.yaml`**: 完全な TPC-H Apache Ossie セマンティックモデル定義。
- **インメモリ SQLite への自動投入**: 8テーブルの DDL 作成とサンプルデータの投入。
- **代表的 4 シナリオの合成 & DB 実行**: 多段 JOIN 顧客集計、Top-5 売上ランキング、不等式数値フィルタ、階層オントロジー縮退。
- **HITL 曖昧性検知 & スラッシュ指示解決**: 人間介入プロンプトの動作と解決の確認。

詳細は [examples/tpch/README.md](file:///C:/Users/satos/dev/Text-to-SQL/examples/tpch/README.md) をご覧ください。

---

## 💻 CLI の使い方

インストールすると、CLI コマンド `deterministic-text-to-sql` が利用可能になります。
本エンジンは完全汎用 Text-to-SQL エンジンであるため、**`--schema <path>` による Apache Ossie セマンティックモデル（YAML / JSON）の指定が基本**です（未指定時は親切なエラー案内を表示）。TPC-H ベンチマークで試す場合は `--schema examples/tpch/tpch_semantic_model.yaml` を指定してください。

### 1. 人間向け対話型 REPL モード (`interactive`)

`--schema` を指定して引数なしで実行するか、`interactive` サブコマンドを実行すると、対話型 REPL セッションが起動します。

```bash
# TPC-H サンプルモデルを指定して起動
deterministic-text-to-sql --schema examples/tpch/tpch_semantic_model.yaml
# または
deterministic-text-to-sql interactive --schema examples/tpch/tpch_semantic_model.yaml
```

```text
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Deterministic Text-to-SQL Engine - Interactive REPL
  Dialect: postgres | Type 'exit' or 'quit' to end session.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SQL> 売上トップ5

┏━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Key ┃ Choice                           ┃ Context / Hint                              ┃
┡━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ A   │ 商品明細売上 (lineitem.l_extendedprice) │ 明細単位の数量や値引きの厳密な集計に適しています │
│ B   │ 注文伝票売上 (orders.o_totalprice)   │ 注文伝票ごとの総額（伝票確定値）を集計します │
└─────┴──────────────────────────────────┴─────────────────────────────────────────────┘

選択肢を入力してください (A / B) [A]: A

┏━ Generated Deterministic SQL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ SELECT                                                          ┃
┃   SUM(lineitem.l_extendedprice) AS total_revenue                ┃
┃ FROM lineitem                                                   ┃
┃ ORDER BY                                                        ┃
┃   total_revenue DESC                                            ┃
┃ LIMIT 5                                                         ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
[Total Latency: 0.4ms]
```

### 2. ワンショット自然言語クエリ (`query`)

1回限りのクエリ合成をコマンドラインから即座に実行できます。`--schema` オプションで対象の `.yaml` / `.json` モデル定義ファイルを指定します。

```bash
# 基本的な質問（TPC-H スキーマ指定）
deterministic-text-to-sql query "日本の顧客一覧" --schema examples/tpch/tpch_semantic_model.yaml

# スラッシュ構文による HITL 補足指定
deterministic-text-to-sql query "アジアの顧客の売上トップ5 / Aで" --schema examples/tpch/tpch_semantic_model.yaml

# ダイアレクト指定（PostgreSQL, SQLite, DuckDB, Snowflake, BigQuery）
deterministic-text-to-sql query "売上上位5件の顧客と注文情報" --schema examples/tpch/tpch_semantic_model.yaml --dialect sqlite

# 曖昧性確認をスキップして強制生成 (--force)
deterministic-text-to-sql query "売上トップ5" --schema examples/tpch/tpch_semantic_model.yaml --force
```

### 3. AI エージェント向け構造化 JSON 出力 (`--json`)

AI エージェント（Claude, Gemini, GPT 等）がツールとして呼び出す場合、`--json` フラグを付与することで、ターミナル装飾のない純粋な機械可読 JSON を取得できます。

```bash
deterministic-text-to-sql query "日本の顧客一覧" --schema examples/tpch/tpch_semantic_model.yaml --json
```

**出力例 (ステータスコード `0` / SUCCESS):**
```json
{
  "status": "success",
  "query": "日本の顧客一覧",
  "clarification": null,
  "dialect": "postgres",
  "sql": "SELECT\n  customer.c_name,\n  COUNT(*) AS count\nFROM customer\nJOIN nation\n  ON customer.c_nationkey = nation.n_nationkey\nWHERE\n  nation.n_name = 'JAPAN'\nGROUP BY\n  customer.c_name",
  "hitl_issue": null,
  "hitl_summary": {
    "primary_table": "customer",
    "secondary_table": "nation",
    "dimension": "c_name",
    "metric": "count_records",
    "ranking": "none",
    "filters": ["JAPAN"],
    "needs_clarification": false
  },
  "intent": {
    "primary_table": "customer",
    "dimensions": ["c_name"],
    "metrics": [
      {
        "column": "*",
        "agg": "COUNT",
        "alias": "count"
      }
    ],
    "filters": [
      {
        "table": "nation",
        "column": "n_name",
        "operator": "=",
        "value": "JAPAN",
        "display_label": "nation.n_name = 'JAPAN'"
      }
    ],
    "limit": null,
    "order_by": []
  },
  "latency_ms": {
    "jev_inference": 0.0,
    "ast_build": 0.35,
    "total": 0.42
  },
  "error_message": null
}
```

#### 終了ステータスコード規約
| 終了コード | 状態 | 内容 |
| :---: | :--- | :--- |
| **`0`** | `success` | 決定論的 SQL の合成に成功 |
| **`1`** | `error` | スキーマ不整合、無効な引数、または内部エラー |
| **`2`** | `hitl_required` | 曖昧性が検知され、人間または親エージェントによる選択・補足が必要 |

### 4. スキーマ自己検査コマンド (`schema`)

対象データベースのテーブル、カラム、役割（role）、外部キー結合関係、**Semantic Metrics**、および **Ontology Hierarchies** を Rich テーブルや JSON で詳細に可視化・検査できます。

```bash
# 人間向けリッチテーブル表示（TPC-H スキーマ指定）
deterministic-text-to-sql schema --schema examples/tpch/tpch_semantic_model.yaml

# エージェント向け構造化 JSON
deterministic-text-to-sql schema --schema examples/tpch/tpch_semantic_model.yaml --json
```

---

## 🐍 Python ライブラリとしての利用 (MCP / サービス統合)

CLI に依存しない純粋なサービスクラス `DeterministicTextToSQLEngine` として設計されているため、Python アプリケーションや将来の MCP ツールハンドラーから極めて簡単に利用できます。

```python
from pathlib import Path
from deterministic_text_to_sql import DeterministicTextToSQLEngine, EngineStatus

# Apache Ossie YAML / JSON スキーマとエンジンを初期化
engine = DeterministicTextToSQLEngine(
    schema="examples/tpch/tpch_semantic_model.yaml",
    api_key="your_typesafe_api_key",
)

with engine:
    # 1行で Text-to-SQL クエリを実行
    result = engine.process_query(
        raw_query="アジアの顧客の売上トップ5",
        dialect="sqlite",
        clarify="A",  # HITL 曖昧性解決の事前指定（任意）
    )

    if result.status == EngineStatus.SUCCESS:
        print("Generated SQL:\n", result.sql)
        print(f"AST Build Time: {result.latency_ms.ast_build:.2f} ms")
    elif result.status == EngineStatus.HITL_REQUIRED:
        print("Clarification needed:", result.hitl_issue.question)
        for opt in result.hitl_issue.options:
            print(f"  [{opt.key}] {opt.label}: {opt.hint}")
```

---

## 🧪 テスト・品質保証

本プロジェクトは **100% 型安全** かつ **厳格な Detroit スタイル TDD** で開発されています。

```bash
# 全テストスイート実行 (Unit & Integration: 全 340 件)
pytest

# Ruff コードスタイル & リンター検証
ruff check .

# Mypy 厳格型チェック (Strict)
mypy src tests
```

- **テストカバレッジ**: 全 **340 テスト**（ユニットテスト 314 件 + E2E 統合テスト 26 件）がオールグリーンで通過。
- **パフォーマンス保証**: AST コンパイル処理時間は平均 **0.2〜0.5ms**（SLA 目標 <10ms を大幅達成）。
- **後方互換性**: 従来のカスタム JSON スキーマと Apache Ossie YAML の完全な振る舞い等価性を担保。

---

## 📜 ライセンス

Apache License 2.0 © 2026 Satoshi Nagayasu

詳細についてはプロジェクトルートの [LICENSE](LICENSE) ファイルを参照してください。

