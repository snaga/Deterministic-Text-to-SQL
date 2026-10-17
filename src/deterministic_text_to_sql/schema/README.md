# Schema サブパッケージ (L1 Intent)

## 責務 (Responsibility)
データベースの物理・論理スキーマ定義を Pydantic v2 モデルとして型安全に表現し、Apache Ossie (v0.2.0.dev0) 形式の YAML/JSON ファイル、ネイティブ JSON ファイル、辞書データからロードおよび厳格なバリデーションを行います。

## 含まれる主要コンポーネント
- `ColumnMetadata`: カラム名、データ型、論理説明、役割（id, dimension, metric, fk, attribute, filter, date_filter）、集約候補、許容値ホワイトリスト、自然言語シノニム
- `ForeignKeyMetadata`: 単一・複合外部キーリレーション情報
- `TableMetadata`: テーブル名、論理説明、自然言語エイリアス／シノニム、主キー、外部キー、カラムリスト
- `MetricDefinition`: セマンティック集約指標定義（ANSI SQL計算式、データ型、自然言語シノニム）
- `HierarchyRule`: 階層オントロジー親子マッピング規則（包含値マッピング、イミュータブル）
- `SchemaMetadata`: 全体スキーマコンテナ、テーブルおよびメトリクス検索ヘルパー
- `OssieSchemaAdapter`: Apache Ossie (v0.2.0.dev0) 形式の YAML / JSON からの透過的変換アダプター
- `SchemaLoader` / `load_schema`: 拡張子自動判別によるスキーマ読み込みとバリデーション
- `SchemaError` / `SchemaValidationError`: スキーマ固有の例外

## 依存制約
- **許可される依存先**: 標準ライブラリ (`json`, `pathlib`, `typing`), `pydantic`, `pyyaml`
- **禁止される依存先**: 上位レイヤー (`graph`, `compiler`, `jev`, `hitl`, `engine`, `cli`) への逆依存
