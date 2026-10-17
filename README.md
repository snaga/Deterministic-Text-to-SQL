# Deterministic Text-to-SQL Engine

TypeSafe AI の System 1 特化型モデル「Jev」と型安全 SQL AST ビルダー（`sqlglot`）を連携させ、自然言語から高信頼・決定論的な SQL をミリ秒オーダー（<100ms）で自動合成する Python 製 Text-to-SQL エンジンおよび CLI です。

## 主な特徴

- **決定論的アセンブリ**: LLM に直接 SQL 文字列を生成させず、抽出スロットとスキーマ定義から AST を決定論的に構築
- **グラフベース JOIN 解決**: NetworkX により最短 FK 結合パスを自動計算
- **多重定義の自動縮退 & HITL**: 階層オントロジー（地域/国）の重複自動解決、曖昧クエリ時の Human-in-the-Loop 対話解消
- **マルチダイアレクト対応**: PostgreSQL, DuckDB, SQLite, BigQuery, Snowflake 等に対応
