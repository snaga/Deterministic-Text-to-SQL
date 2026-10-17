# `compiler` サブパッケージ仕様 (L1 Intent)

本ドキュメントは、`deterministic_text_to_sql.compiler` パッケージの設計意図、モジュール境界、階層オントロジー縮退の数学的背景、および純粋関数設計方針を永続化するための仕様書（L1 Intent）です。

---

## 1. 概要と責務

`compiler` パッケージは、意味論的クエリ意図（`QueryIntent`）および抽出スロット（`FilterSlot` 等）から、決定論的かつ安全に SQL を生成・最適化するためのコンパイル層です。

### 責務 (What it does)
- **階層オントロジー縮退 (`deduplicator.py`)**: 親子階層関係（地域と国、大カテゴリと小カテゴリ等）にある複数フィルタから、冗長な親条件を数学的包摂原理に基づいて安全に縮退・排除。
- **決定論的 AST ビルダー (`builder.py`, TASK-05予定)**: `sqlglot` を活用し、文字列連結を排した型安全な AST 構築および複数ダイアレクト（PostgreSQL, DuckDB, SQLite 等）へのトランスパイル。
- **データモデル定義 (`models.py`)**: フィルタ条件（`FilterSlot`）および正規化クエリ意図（`QueryIntent`）の不変モデル化。

### 扱わないこと (What it does NOT do)
- 自然言語からのスロット抽出や曖昧性判定（`jev` / `hitl` の責務）。
- テーブル間の最短結合パス探索（`graph` の責務）。
- 外部 I/O やデータベース接続（副作用の完全排除）。

---

## 2. 階層オントロジー縮退の数学的背景

自然言語クエリ「アジアの日本の顧客の売上」を解釈する際、抽出エンジンは以下のような複数のフィルタ条件を同時に抽出することがあります:
- 親条件 $P$: `region.r_name = 'ASIA'`
- 子条件 $C$: `nation.n_name = 'JAPAN'`

### 数学的包摂 (Subsumption) とブール代数
ドメイン知識（TPC-H等のスキーマオントロジー）において、日本（JAPAN）はアジア（ASIA）の部分集合として定義されています:
$$\text{JAPAN} \subseteq \text{ASIA} \implies C \implies P$$

ブール論理における吸収律（Absorption Law / Subsumption）により:
$$C \land P \iff C$$

すなわち、子条件 $C$ が成立するならば親条件 $P$ は真であることが論理的に演繹されるため、親条件 $P$ は**完全に冗長（Redundant）**です。

### 冗長親条件を排除すべき理由
1. **SQL 最適化・インデックス活用の阻害防止**: 冗長な結合や余計なフィルタ句が WHERE 句に残留すると、オプティマイザのカーディナリティ推定が狂い、フルスキャンや不要な中間テーブル結合を誘発するリスクがある。
2. **決定論的 SQL の純度**: ユーザーが意図した本質的制約のみを厳密に SQL に投影する。

### 縮退アルゴリズムの動作原則
- **包摂成立時**: 親と子が同時に存在し、子の値が親の値を含意する場合、親フィルタを除外して子フィルタのみを残す。
- **独立条件**: 親に属さない複数の子（例: `JAPAN` と `GERMANY`）は独立した制約として両方保持する。
- **非包含時**: 子が存在しても親の値と整合しない場合（例: `nation = 'JAPAN'` と `region = 'EUROPE'`）、含意関係が成立しないため勝手な削除を行わず両方保持する。
- **単独指定**: 親のみ、または子のみが指定されている場合は当然そのまま保持する。
- **無関係フィルタ**: 別テーブルの条件（例: `orders.o_orderstatus = 'F'`）には一切干渉しない。

---

## 3. 純粋関数とイミュータビリティ (Immutability)

`compiler` 配下のすべての変換処理は **純粋関数（Pure Functions）** として設計されています:
1. **参照透過性**: 同一の入力に対して常に同一の出力を返します。
2. **副作用ゼロ**: 外部状態の読み書きやグローバル変数の変更は一切行いません。
3. **入力不変性 (Immutability)**:
   - `FilterSlot` および `QueryIntent` は `frozen=True` で定義され、生成後の改変を防止します。
   - `OntologyDeduplicator.deduplicate()` は入力の `list[FilterSlot]` を破壊的に変更せず、必ず新規リストを生成して返却します。
   - フィルタの出現順序（相対的安定性: Stable Ordering）を完全に保持します。

---

## 4. モジュール構成

```text
src/deterministic_text_to_sql/compiler/
├── __init__.py       # エクスポート定義
├── models.py         # FilterSlot, QueryIntent 不変モデル
├── deduplicator.py   # 階層オントロジー縮退器 (OntologyDeduplicator, HierarchyRule)
├── builder.py        # GenericSQLBuilder (TASK-05にて実装)
└── README.md         # 本仕様ドキュメント (L1 Intent)
```
