# テクノロジースタック (tech.md)

## 🛠️ プログラミング言語
- **Python 3.11+**: 型ヒントの充実、高速化、豊富なエコシステムを活用した型安全設計

## 🏗️ フレームワーク & ライブラリ
- **SQL AST コンパイラ**: `sqlglot` (v25+) - 構文木（AST）の構築、最適化、PostgreSQL/DuckDB/SQLite/BigQuery/Snowflake 等へのダイアレクト別トランスパイル
- **グラフ探索エンジン**: `networkx` (v3+) - 外部キー（FK）関係のグラフモデリング、最短経路（Dijkstra/BFS）および Steiner Tree 近似探索
- **スキーマ & データバリデーション**: `pydantic` (v2+) - セマンティックメタデータ（テーブル・カラム・ロール・値域・リレーション）の型安全な定義とバリデーション
- **HTTP クライアント**: `httpx` (v0.27+) - TypeSafe Jev API への高速な接続（Keep-Alive、接続プール、同期/非同期クライアント）
- **CLI & ターミナル UI**: `typer` / `rich` - 対話型CLIインターフェース、HITL（Human-in-the-Loop）曖昧性解消プロンプト、カラーシンタックスハイライト

## 🧪 テストツール
- **ユニット & 結合テスト**: `pytest` (v8+) - 決定論的SQL生成の回帰テスト、AST等価性テスト
- **カバレッジ測定**: `pytest-cov` - C1/C2分岐カバレッジの網羅的検証
- **モック & API テスト**: `respx` / `pytest-mock` - Jev API のレスポンスモック、異常系テスト

## 🔧 開発ツール
- **静的解析 & リンター / フォーマッター**: `ruff` - 高速なコードスタイル検証および自動修正
- **静的型チェッカー**: `mypy` - 型完全性（strict mode）の保証
- **パッケージ & 依存関係管理**: `uv` / `pip` - 高速な環境構築と再現性の高いロックファイル管理
- **コード探索 & AST 解析**: `ast-digger` - トークン効率的なコード構造把握・シンボル探索

## 📦 インフラ / プラットフォーム
- **実行環境**: クロスプラットフォーム（Linux, macOS, Windows）ローカルCLIおよびコンテナ実行（Docker）
- **データベース接続（検証用）**: `sqlite3` (Python標準) / `duckdb` (組込み分析エンジン) / `psycopg2-binary` (PostgreSQL接続)

## 🔗 外部API / サービス
- **TypeSafe Jev API**: `POST /v1/systemone`, `GET /v1/models` - 超高速（<30ms）System 1 判別エンジン。クエリからのスロット抽出・HITL曖昧性検知を実行。
