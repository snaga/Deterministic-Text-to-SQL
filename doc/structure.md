# プロジェクト構造 (structure.md)

## 📁 フォルダ構成
本プロジェクトは、明確な責務分離と型安全性を重視したモジュール構造を採用します。

```text
.
├── src/
│   └── deterministic_text_to_sql/
│       ├── __init__.py
│       ├── cli.py               # Typer CLI エントリポイント
│       ├── engine.py            # コア統合エンジン（CLI/MCP非依存の純粋サービスクラス）
│       ├── schema/              # セマンティックメタデータ定義 & Pydanticモデル
│       │   ├── __init__.py
│       │   ├── models.py        # テーブル/カラム/FK/メトリクス/オントロジーモデル
│       │   ├── loader.py        # 汎用スキーマローダー
│       │   └── ossie.py         # Apache Ossie (YAML/JSON) アダプター & パーサー
│       ├── graph/               # スキーマグラフ & JOIN探索
│       │   ├── __init__.py
│       │   └── resolver.py      # NetworkXによる最短パス・JOIN解決
│       ├── jev/                 # TypeSafe Jev API クライアント & スロットパーサー
│       │   ├── __init__.py
│       │   ├── client.py        # Jev API HTTP通信
│       │   ├── slots.py         # スロット抽出・多重選択検出 (HITL判定)
│       │   └── prompts.py       # Jev用判別プロンプト定義
│       ├── compiler/            # 型安全 AST ビルダー
│       │   ├── __init__.py
│       │   ├── builder.py       # sqlglotを用いた決定論的ASTアセンブリ
│       │   └── deduplicator.py  # 階層オントロジー縮退ロジック
│       └── hitl/                # Human-in-the-Loop 対話管理
│           ├── __init__.py
│           └── feedback.py      # スラッシュ構文解析 & UIフィードバック
├── tests/                       # ユニット・統合テスト
│   ├── unit/                    # 各モジュールの単体テスト
│   ├── integration/             # E2Eクエリ生成・回帰テスト
│   └── fixtures/                # TPC-H スキーマ (JSON / Ossie YAML) やテストデータ
├── doc/                         # 🌍 公開用ドキュメント・仕様書マスター (Git管理)
│   ├── adr/                     # アーキテクチャ決定レコード
│   ├── product.md               # 製品概要・憲法
│   ├── tech.md                  # 技術スタック仕様
│   ├── structure.md             # プロジェクト構造（このファイル）
│   ├── requirements.md          # 要件定義 (EARS記法)
│   └── design.md                # 詳細設計書 (Mermaid/IPO)
├── pyproject.toml               # プロジェクト定義 & 依存関係設定
├── README.md                    # プロジェクト概要
└── .gitignore                   # Git除外設定
```

> [!IMPORTANT]
> **仕様書と作業領域の分離ルール**:
> - **公開仕様マスター (`doc/`)**: 要件・設計・憲法・ADRなど、リポジトリ公開に必要な正式仕様書のみを配置します。
> - **非公開開発作業領域**: 実装タスク計画や開発用作業メモは、`.gitignore` で完全除外した非公開ディレクトリ配下で運用し、この公開用 `doc/structure.md` には掲載しません。

## 🏷️ 命名規則
- **モジュール・ファイル名**: `snake_case.py` （例: `generic_builder.py`, `schema_loader.py`）
- **クラス名**: `PascalCase` （例: `SchemaGraph`, `GenericSQLBuilder`, `JevClient`）
- **関数名・メソッド名**: `snake_case` （例: `find_shortest_path`, `build_ast`）
- **定数名**: `UPPER_SNAKE_CASE` （例: `DEFAULT_TIMEOUT_MS`, `SUPPORTED_DIALECTS`）
- **プライベートメンバー**: `_snake_case` （例: `_validate_slots`, `_assemble_where`）

## 🏗️ アーキテクチャの方針
- **依存性の単方向流下 (パイプライン設計)**:
  `Input (自然言語)` → `Jev (スロット抽出)` → `Graph (JOINパス解決)` → `Compiler (ASTアセンブリ)` → `Output (型安全SQL)` の一方向フローを維持し、循環参照を物理的に遮断します。
- **純粋関数と副作用の分離**:
  ASTの構築（`compiler`）やグラフ最短経路探索（`graph`）は、外部通信を行わない純粋な決定論的ロジックとして設計し、100%再現可能なテスト容易性を確保します。
- **型完全性 (Strict Typing)**:
  すべての公開関数・メソッドに厳格な型ヒントを付与し、`mypy --strict` をパスさせます。メタデータやIntent表現には `Pydantic` モデルを採用し、データ構造の契約を明確化します。

## 🛠️ インポートパターン
- **絶対インポートの強制**: `from deterministic_text_to_sql.schema.models import TableMetadata` のように、パッケージルートからの絶対インポートを標準とします。
- **グループ分けの順序**:
  1. 標準ライブラリ (`os`, `sys`, `time`, `typing` など)
  2. サードパーティライブラリ (`sqlglot`, `networkx`, `pydantic`, `httpx` など)
  3. 自作パッケージモジュール (`deterministic_text_to_sql.*`)

## 🔗 その他設計の決定事項
- **ダイアレクト中立性**: 内部ASTは `sqlglot` の汎用式として構築し、最終出力時にユーザー指定のダイアレクト（`postgres`, `duckdb`, `bigquery`, `snowflake`）へトランスパイルします。
- **HITLファースト**: 判別結果に曖昧性（複数解釈の競合）が存在する場合、無理に単一のSQLを推定せず、即座に介入要求（HITL Issue）オブジェクトを返却してユーザーの確認を仰ぎます。

## 🛡️ Git除外方針 (.gitignore)
プロジェクトの健全性とセキュリティのため、以下のカテゴリを `.gitignore` で確実に除外します。
- **AIエージェント設定・プライベート設定**: `AGENTS.md`, `GEMINI.md`, `/.agents/`, `/.agent/`, `/.gemini/`, `/.claude/`
- **非公開開発作業領域**: 実装タスク計画や内部実験スクリプトを格納する非公開ディレクトリ
- **秘密情報・環境変数**: `.env`, `credentials.json`, `token.json`
- **ビルド・パッケージ成果物**: `dist/`, `build/`, `*.egg-info/`, `__pycache__/`
- **テスト・カバレッジ出力**: `.coverage`, `coverage.xml`, `htmlcov/`, `.pytest_cache/`
