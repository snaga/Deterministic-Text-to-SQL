# 0003. ウィンドウ関数（Window Functions）および2段CTEクエリ合成によるグループ内Top-Nと時系列推移（YoY）のサポート

- **ステータス**: Accepted
- **決定日**: 2026-10-19
- **対象**: `src/deterministic_text_to_sql/compiler/`, `src/deterministic_text_to_sql/jev/`, `doc/`

---

## Context (背景と課題)

ADR-0002 において、多軸集計（`GROUP BY col1, col2`）、セマンティック時間ディメンション、および集計後フィルタ（`HAVING`）を導入し、「複雑度の階層（Complexity Hierarchy）」における Level 1（単一ファクト＋ディメンション集約）を完全制覇し、Level 2 の一部を達成した。

実務におけるアドホック分析では、これに加えて以下の2大パターンが極めて頻繁に要求される：
1. **グループ内 Top-N（Partitioned Ranking）**:
   - 例: 「各地域ごとの売上トップ3顧客」「商品カテゴリ別の注文件数上位5商品」
   - 従来の全体 `ORDER BY ... LIMIT N` ではクエリ全体の上位しか取得できず、グループごとの上位 N 件を抽出できない。
2. **時系列差分・比率（Time-Series Window / YoY・MoM）**:
   - 例: 「年別の売上および前年比（YoY）成長率」「月別注文件数の前月差」
   - 前期実績を参照するために `LAG() OVER (ORDER BY time_dim)` によるウィンドウ集計が必要。

ANSI SQL の仕様上、以下の2つの厳格な構文制約が存在する：
- ウィンドウ関数の結果（例: `ROW_NUMBER()`）を `WHERE` 句で直接フィルタリングすることはできず、サブクエリまたは共通テーブル式（CTE: `WITH` 句）で一度ラップしてから外側で `WHERE rank_num <= N` を適用しなければならない。
- `GROUP BY` による集計結果に対してウィンドウ関数（例: `LAG(SUM(...))`）を適用し、さらに比率計算を行う場合、集計済みベーステーブルを CTE として事前実体化してからウィンドウ演算を行うのが最も移植性が高く安全である。

事前に行ったスパイクプロトタイプ検証（`scratch/prototype_window_cte.py`）により、`sqlglot` を用いた CTE 合成、`exp.Window` ノードのアセンブリ、および SQLite / PostgreSQL 上での完全な透過実行と高精度な Jev スロット分離が実証された。

---

## Decision (決定事項)

本プロジェクトにおいて、以下のアーキテクチャ設計およびコンポーネント拡張を採用し、ウィンドウ関数と2段CTEクエリ合成を決定論的Text-to-SQLエンジンに導入する。

### 1. ウィンドウ操作スロットモデル（`WindowSlot`）の新設
`src/deterministic_text_to_sql/compiler/models.py` にイミュータブルな `WindowSlot` モデルを新設する：

```python
class WindowSlot(BaseModel):
    """Window function operation specification for partitioned ranking and time shifts."""

    kind: Literal["partitioned_rank", "time_shift", "moving_avg", "cumulative"]
    function: str = "ROW_NUMBER"  # ROW_NUMBER, DENSE_RANK, LAG, LEAD, AVG, SUM
    partition_by: list[str] = Field(default_factory=list)  # 例: ["region.r_name"]
    order_by: list[OrderBySlot] = Field(default_factory=list)  # 例: [OrderBySlot(column="total_revenue", direction="DESC")]
    offset: int = 1  # LAG / LEAD のシフト量
    limit: int | None = None  # グループ内 Top-N の上限 (例: 3)
    target_metric: str | None = None  # 対象メトリクス (例: "total_revenue")

    model_config = ConfigDict(frozen=True)
```

`QueryIntent` に `window: WindowSlot | None = None` フィールドを追加する。

### 2. `GenericSQLBuilder` における「Two-Pass（2段CTE）クエリ合成器」の導入
`intent.window` が指定された場合、従来の単一 `SELECT` 生成から「2段 CTE パイプライン」へと自動切り替えを行う：
1. **Pass 1 (Base CTE Query)**:
   - 起点テーブルからの最短経路 JOIN、`WHERE` フィルタ、およびディメンションによる `GROUP BY` を行い、ベースとなる集約クエリ（例: `ranked_base` または `yearly_base`）を組み立てる。
   - グループ内 Top-N の場合、ベースクエリの SELECT 句に `exp.Window(this=exp.Anonymous(this="ROW_NUMBER"), partition_by=..., order=...) AS rank_num` を配置。
2. **Pass 2 (Outer Query)**:
   - `outer_query = exp.select(...).from_(exp.to_table("ranked_base"))`
   - グループ内 Top-N の場合: `outer_query.where(exp.LTE(this=exp.column("rank_num"), expression=exp.Literal.number(limit)))`
   - 時系列 YoY の場合: 外側クエリで `LAG(total_revenue, 1) OVER (ORDER BY orders_year)` および成長率式をアセンブリ。
   - `outer_query.with_("base_cte_name", as_=base_query)` を付与して完成。

### 3. Jev プロンプト・スロット抽出の直交分離
全体ランキング（`ranking: top_N` ➔ 単純な `ORDER BY + LIMIT`）と、グループ内ランキング（`window: partitioned_rank` ➔ `PARTITION BY + CTE`）が混同しないよう、Jev Criteria の質問定義において「グループ内順位」を独立した直交スロットとして提示・抽出する。

---

## Consequences (結果と影響)

### Positive (メリット)
- **複雑度 Level 2 の完全制覇**: 実務アドホック分析で最も難易度が高い「グループ内 Top-N」や「前年比成長率（YoY）」が、ミリ秒・ハルシネーション 0% で安定出力可能になる。
- **高いポータビリティ**: 生成される CTE + ウィンドウ関数構文は標準 ANSI SQL に完全準拠しており、PostgreSQL、DuckDB、SQLite（3.25+）、Snowflake 等の全対応エンジンで変更なく動作する。
- **後方互換性の維持**: `intent.window` が存在しない通常のクエリは、従来の単一 `SELECT` パスを通るため、既存の全 432 テストケースに対するオーバーヘッドやデグレは一切生じない。

### Negative (デメリット/コスト)
- **AST ビルダー内部のロジック分岐**: `GenericSQLBuilder` が「1-Pass Direct SELECT」と「2-Pass CTE Wrapping」の2つの生成戦略をハンドリングする必要がある。

### Risks (リスク)
- **構文ダイアレクト差異**: SQLite における `EXTRACT` の `STRFTIME` 変換など、CTE 内の派生カラムと外側クエリでの参照名の整合性が崩れるリスクがある。
  - **対策**: スパイク検証済みのアプローチに従い、CTE 内部で明示的なカラムエイリアス（`orders_year` 等）を付与し、外側からはそのエイリアスを参照することでダイアレクト差を完全に吸収する。
