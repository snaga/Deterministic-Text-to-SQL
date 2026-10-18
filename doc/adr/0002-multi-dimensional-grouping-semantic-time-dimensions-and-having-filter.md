# 0002. 多軸集計（Multi-dimensional Grouping）、メタデータ駆動時間ディメンション、および集計値フィルタ（HAVING Filter）のサポート

- **Status**: Accepted
- **Date**: 2026-10-18
- **Deciders**: TypeSafe AI / Deterministic Text-to-SQL プロジェクトチーム

## Context (背景と課題)

当プロジェクト「deterministic-text-to-sql」は、Apache Ossie (incubating) v0.2.0.dev0 のセマンティックモデルを採用し、メトリクス計算式 AST 展開、最短経路 JOIN 解決、オントロジー縮退、および自然言語曖昧性検知（HITL）を汎用化・実用化した。

しかし、実務のアドホック分析（BI・ダッシュボード・経営/アナリストの問い合わせ）における頻出パターンを検証した結果、以下の3つの機能的制約により、現場で求められる基本的な多軸分析や集計後絞り込みが成立しない課題が明らかになった：

1. **単一ディメンション（単一軸集計）の制約**:
   - Jev の Criteria 定義（`prompts.py`）が `"dimension": {"type": "choice", ...}` と単一選択になっており、スロットモデル `ParsedSlots` も `dimension: str` の単一カラム保持設計であったため、「年別かつ国別の注文金額集計」のように複数軸が指定された場合、1軸しか採用されずもう一方の軸（年別）が欠落する。
2. **時間軸・時系列粒度（Date Truncation / Extract）の変換機構の欠落**:
   - RDBMS / DWH の物理テーブルには「年（Year）」や「月（Month）」といった物理カラムは存在せず、日付カラム（`orders.o_orderdate` 等）が存在する。
   - 自然言語で「年別」「月別」と要求された際、`EXTRACT(year FROM ...)` や `DATE_TRUNC('month', ...)`、`STRFTIME(...)` などの時間関数を AST レベルで展開・バインドする機構が未実装であった。
3. **集計値フィルタ（HAVING 句）の未サポート**:
   - 「売上が1000万円以上の顧客一覧」「注文回数が5回以上のユーザー」のように、集計指標に対する絞り込み（`HAVING SUM(amount) >= ...`）が指定された場合、現在の `FilterSlot` はすべて `WHERE` 句として展開されてしまい、SQL 構文エラー（WHERE 句での集約関数参照違反）または合成不能となる。

Window 関数（OVER 句）や CTE（サブクエリ）を必要とする高度なアナリティクス（YoY 成長率、グループ内 Top-N 等）に着手する前に、フラットな単一 SELECT 文（FROM + JOIN + WHERE + GROUP BY + HAVING + ORDER BY + LIMIT）の枠組みで解決可能な上記 3 パターンを先行して正式サポートすることが求められている。

## Decision (決定事項)

決定論的かつ高信頼な SQL 合成を維持しつつ、アドホック分析の柔軟性を大幅に拡張するため、以下のアーキテクチャ方針を決定する：

### 1. 多軸ディメンション（Multi-dimensional Grouping）のサポート
- **Jev 判別拡張**:
  - `prompts.py` の `dimension` を複数選択可能（`dimensions: list[str]`）な Criteria 構造へ拡張する。
- **スロットモデルのリスト化**:
  - `ParsedSlots` に `dimensions: list[str] = Field(default_factory=list)` を導入する（後方互換性のため既存の単一 `dimension: str` プロパティもプロパティアクセサまたはデフォルト値として維持）。
- **インテントおよび AST 合成**:
  - `engine.py` の `_build_intent_from_slots` において全ディメンションを canonical 解決し、`QueryIntent.dimensions` に格納する。
  - `GenericSQLBuilder` が全ディメンションを `SELECT` 句および `GROUP BY` 句に過不足なく配置する。

### 2. メタデータ駆動セマンティック時間ディメンション（Semantic Time Dimensions）の導入
- **メタデータモデルの拡張 (`DimensionMetadata`)**:
  - メトリクス（`MetricMetadata`）と同様に、計算式を持つセマンティック・ディメンション定義モデルを新設する：
    ```python
    class DimensionMetadata(BaseModel):
        name: str
        expression: str | None = None
        description: str = ""
        synonyms: list[str] = Field(default_factory=list)
        is_time: bool = False
        time_granularity: str | None = None  # 'year', 'month', 'day' など
    ```
- **Apache Ossie セマンティックモデルの拡張**:
  - Ossie YAML / JSON の `datasets.fields` における `is_time: true` および `time_granularities: [year, month, day]`、ならびに `custom_extensions` のセマンティック・ディメンション定義を自動ロードし、`SchemaMetadata.dimensions` として管理する。
  - 例: `orders.o_orderdate` に `time_granularities: [year, month]` が指定された場合、`orders_year` (`EXTRACT(year FROM orders.o_orderdate)`) や `orders_month` を透過的に仮想ディメンションとして登録する。
- **AST ビルダーでの安全なパースと展開**:
  - `GenericSQLBuilder` は、指定されたディメンションが計算式（`expression`）を持つ場合、`sqlglot.parse_one` で安全に AST 化し、`SELECT` 句および `GROUP BY` 句に配置する。
  - 式内で参照されているテーブル（例: `orders`）を自動抽出し、JOIN 最短経路解決に登録する。

### 3. 集計値フィルタ（HAVING Filter）のサポート
- **集計フィルタスロットの導入**:
  - スロットモデルおよびインテントモデルに `having_slots: list[HavingSlot]`（または `FilterSlot(is_having=True)`）を導入する：
    ```python
    class HavingSlot(BaseModel):
        metric: str
        operator: str  # '>=', '<=', '=', '>', '<', '!='
        value: Any
    ```
- **Jev による判別とスロットバインディング**:
  - 自然言語内の条件判定において、対象が通常の物理カラム値（WHERE）か、集計メトリクスに対する閾値（HAVING）かを判別し、適切にスロットへ振り分ける。
- **HAVING 句の AST 合成**:
  - `GenericSQLBuilder` において、`having_slots` を `exp.Having` 句として構築し、メトリクス式またはメトリクス alias との比較式を合成する。

## Consequences (結果と影響)

### Positive (メリット)

- **アドホック分析カバー率の劇的向上**:
  - 「年別かつ国別の注文金額集計」「売上が1000万円以上の顧客」など、実務で最頻出する多軸クロス集計・時系列推移・集計後絞り込みが完全自動化される。
- **SQL 方言および日付ロジックのハードコード完全排除**:
  - 日付の切り出し方や計算式はすべてセマンティックメタデータ側（YAML / JSON）で宣言的に管理され、Python エンジン本体はピュアで汎用的な AST 合成機として保たれる。
- **JOIN パスの完全自動追従**:
  - 「年別」がディメンションとして指定されるだけで、`orders.o_orderdate` を含む式が認識され、起点テーブルが `lineitem` であっても自動的に `lineitem ↔ orders` の JOIN が追加される。
- **既存資産との完全な後方互換性**:
  - 従来の単一ディメンションクエリや既存テストスイート（全340テスト）への影響はなく、上位互換としてシームレスに機能する。

### Negative (デメリット/コスト)

- **Jev プロンプトの入力トークン微増**:
  - 時間ディメンション（年別・月別）や複数ディメンションの選択肢が Criteria に追加されるため、Jev へのリクエストサイズがわずかに増加する（数トークン程度）。
- **メタデータ検証ロジックの追加**:
  - ディメンション式の構文妥当性や、HAVING 句における集約関数存在チェックなどのバリデーションが必要となる。

### Risks (リスク)

- **SQL ダイアレクト間の日付関数の差異**:
  - `EXTRACT(year FROM ...)` や `STRFTIME`、`DATE_TRUNC` は DBMS（PostgreSQL, DuckDB, SQLite, Snowflake）によって構文が異なる場合がある。
  - *対策*: `sqlglot` の強力なトランスパイル機能を活用しつつ、Ossie の `dialects` 拡張をサポートしてダイアレクト固有の式をメタデータで上書き可能とする。
