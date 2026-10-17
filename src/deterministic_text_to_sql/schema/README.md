# Schema サブパッケージ (L1 Intent)

## 責務 (Responsibility)
データベースの物理・論理スキーマ定義を Pydantic v2 モデルとして型安全に表現し、JSON ファイルや辞書データからロードおよび厳格なバリデーションを行います。

## 含まれる主要コンポーネント
- `ColumnMetadata`: カラム名、データ型、論理説明、役割（id, dimension, metric, fk, attribute, filter, date_filter）、集約候補、許容値ホワイトリスト
- `ForeignKeyMetadata`: 単一・複合外部キーリレーション情報
- `TableMetadata`: テーブル名、論理説明、自然言語エイリアス、主キー、外部キー、カラムリスト
- `SchemaMetadata`: 全体スキーマコンテナ、テーブル検索ヘルパー
- `SchemaLoader`: JSON / Dict からの読み込みとバリデーション
- `SchemaError` / `SchemaValidationError`: スキーマ固有の例外

## 依存制約
- **許可される依存先**: 標準ライブラリ (`json`, `pathlib`, `typing`), `pydantic`
- **禁止される依存先**: 上位レイヤー (`graph`, `compiler`, `jev`, `hitl`, `engine`, `cli`) への逆依存
