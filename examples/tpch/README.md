# 🎓 TPC-H Benchmark Tutorial & Examples

オープン標準メタデータ仕様 **[Apache Ossie (incubating) v0.2.0.dev0](https://github.com/apache/ossie)** に準拠した TPC-H ベンチマーク（8テーブル）の完全なセマンティックモデルと、インメモリ SQLite を使って即座に動作確認できるチュートリアルキットです。

---

## 📁 ディレクトリ構成

- **`tpch_semantic_model.yaml`**:
  - Apache Ossie (incubating) v0.2.0.dev0 準拠の TPC-H セマンティックモデル定義（8テーブル、主キー、外部キーリレーション、6つの Semantic Metrics、親子オントロジー階層定義）。
- **`run_tutorial.py`**:
  - インメモリ SQLite データベースを起動し、DDL・シードデータを投入した上で、4 つの代表的クエリパターンおよび HITL 曖昧性検知・解決を自動実行・結果表示するチュートリアルスクリプト。
- **`README.md`**: 本ガイド。

---

## 🚀 クイック実行 (Python チュートリアル)

外部 DB のセットアップや API キーの登録なしで、スクリプト 1 つで決定論的 SQL の合成と実 DB での実行結果を確認できます。

```bash
# プロジェクトルートから実行
python examples/tpch/run_tutorial.py
```

### 実行されるシナリオ
1. **多段 JOIN 顧客集計 (Customer Aggregation)**:
   - 自然言語: `アジア地域の顧客残高集計`
   - Dijkstra 最短経路により `customer -> nation -> region` を自動結合し、正しい `SUM(c_acctbal)` を集計。
2. **Top-K 売上ランキング (Revenue Ranking)**:
   - 自然言語: `売上上位5件の顧客と注文情報`
   - `customer` と `orders` を自動結合し、`ORDER BY total_revenue DESC LIMIT 5` を合成。
3. **不等式数値フィルタ (Inventory Shortage)**:
   - 自然言語: `在庫数10以下の部品一覧`
   - `part` と `partsupp` を結合し、`WHERE ps_availqty <= 10` 条件を適用。
4. **階層オントロジー縮退 (Mathematical Subsumption)**:
   - 自然言語: `アジアの日本の顧客残高`
   - 「日本（nation）」が「アジア（region）」に包含されるため、親テーブル `region` の不要な JOIN と WHERE 条件を自動剪定。
5. **Human-in-the-Loop (HITL) 曖昧性検知 & 解決**:
   - 自然言語: `売上トップ5`（解釈が多義的）
   - エンジンが停止して選択肢（A: 商品明細売上 / B: 注文伝票売上）を提示。
   - スラッシュ指示構文（`売上トップ5 / Aで`）で即座に解決して SQL を合成。

---

## 💻 CLI コマンドでの実行例

CLI コマンド `deterministic-text-to-sql` から本セマンティックモデルを直接読み込んで実行できます。

### 1. セマンティックモデルの可視化 (`schema`)

Rich テーブルでテーブル、カラム、Semantic Metrics、オントロジー階層を綺麗に可視化します：

```bash
deterministic-text-to-sql schema --schema examples/tpch/tpch_semantic_model.yaml
```

JSON 形式でエージェント向けに出力する場合：

```bash
deterministic-text-to-sql schema --schema examples/tpch/tpch_semantic_model.yaml --json
```

### 2. ワンショット自然言語クエリ (`query`)

```bash
# 基本クエリ
deterministic-text-to-sql query "アジア地域の顧客残高集計" --schema examples/tpch/tpch_semantic_model.yaml

# スラッシュ指示構文による HITL 曖昧性解決
deterministic-text-to-sql query "売上トップ5 / Aで" --schema examples/tpch/tpch_semantic_model.yaml

# SQLite ダイアレクト指定
deterministic-text-to-sql query "売上上位5件の顧客と注文情報" --dialect sqlite --schema examples/tpch/tpch_semantic_model.yaml
```

### 3. AI エージェント向け構造化 JSON 出力 (`--json`)

```bash
deterministic-text-to-sql query "日本の顧客一覧" --schema examples/tpch/tpch_semantic_model.yaml --json
```

---

## 🐍 Python コードからの組み込み利用

```python
from deterministic_text_to_sql import DeterministicTextToSQLEngine, EngineStatus

# Apache Ossie YAML セマンティックモデルを指定して初期化
engine = DeterministicTextToSQLEngine(
    schema="examples/tpch/tpch_semantic_model.yaml"
)

result = engine.process_query("アジア地域の顧客残高集計", dialect="postgres")
if result.status == EngineStatus.SUCCESS:
    print("Compiled SQL:\n", result.sql)
    print(f"Latency: {result.latency_ms.ast_build:.2f} ms")
```
