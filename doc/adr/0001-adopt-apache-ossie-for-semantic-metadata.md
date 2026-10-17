# 0001. セマンティックメタデータ標準仕様としての Apache Ossie の採用

- **Status**: Accepted
- **Date**: 2026-10-17
- **Deciders**: TypeSafe AI / Deterministic Text-to-SQL プロジェクトチーム

## Context (背景と課題)

当プロジェクト「deterministic-text-to-sql」は、TypeSafe AI の System 1 特化型モデル「Jev」と型安全 SQL AST ビルダー「sqlglot」を連携させ、自然言語から高信頼・決定論的な SQL をミリ秒オーダー（<100ms）で自動合成するエンジンである。

初期フェーズでは TPC-H ベンチマークスキーマを対象に実装を進め、最短経路 JOIN 解決（NetworkX）や型安全 AST 生成（sqlglot）の基本機構を確立した。しかし、現在のコードベースには以下の課題が存在する：

1. **特定スキーマ（TPC-H）への強い結合**:
   - Jev の質問定義（`prompts.py`）、スロット抽出（`slots.py`）、HITL 曖昧性検知（`feedback.py`）、オントロジー縮退（`deduplicator.py`）、メトリクス解決（`engine.py`）に、TPC-H 固有のカラム名やプレフィックス（`c_`, `o_`, `l_`）、業務指標（売上、返品フラグ等）がハードコードされている。
2. **汎用化のためのメタデータ仕様の不在**:
   - 新規データベース（EC、SaaS、金融、医療等）への適用にあたり、業務メトリクス、自然言語シノニム、外部キー結合、階層オントロジー、業務除外ルールを宣言的に記述・設定するための統一仕様が必要とされていた。

## Decision (決定事項)

セマンティックメタデータの外部設定ファイル標準仕様として、The Apache Software Foundation (ASF) のインキュベータープロジェクトである **Apache Ossie (incubating)**（旧 Open Semantic Interchange / OSI）のコア仕様（`core-spec/spec.md` v0.2.0）を全面的に採用する。

具体的には、以下のアーキテクチャ方針を決定する：

1. **アダプターパターンの採用**:
   - 既存の内部ドメインモデル `SchemaMetadata` を維持・拡張し、新設する `OssieSchemaAdapter` を通じて Ossie 形式（YAML / JSON）を透過的に読み込む。
2. **Ossie コンポーネントの直接マッピング**:
   - `relationships` ➔ `SchemaGraph`（NetworkX 最短経路 JOIN）
   - `metrics`（`expression.dialects`, `ai_context.synonyms`） ➔ Jev の `METRIC_CRITERIA` 動的生成 & `sqlglot` 集約 AST ビルド
   - `datasets` & `fields`（`dimension.is_time`, `ai_context.synonyms`） ➔ Jev の主テーブル・ディメンション Criteria & 自然言語スロットバインド
3. **拡張機構（`custom_extensions`）の活用**:
   - Ossie Core Spec で直接カバーされない「値レベルの階層オントロジー縮退マッピング（Mathematical Subsumption）」および「HITL 曖昧性検知カスタムルール」は、Ossie の公式拡張フィールド `custom_extensions`（`vendor_name: "DETERMINISTIC_TEXT_TO_SQL"`）に格納する。
4. **TPC-H スキーマの Ossie YAML 化**:
   - 現行の `fixtures/tpch_schema.json` を Ossie 準拠の YAML 形式（`tpch_ossie.yaml`）へ移行し、全テストスイートの基盤を Ossie 互換とする。

## Consequences (結果と影響)

### Positive (メリット)

- **完全なスキーマ非依存・汎用化の達成**:
  - Python コードを 1 行も修正することなく、Ossie YAML を 1 つ配置するだけで任意の RDBMS / DWH に対する決定論的 Text-to-SQL が稼働する。
- **オープン標準エコシステムとの相互運用性**:
  - dbt Semantic Layer、Snowflake Semantic Views、Tableau、GoodData 等の既存セマンティック資産を、Ossie のコンバーター群を介してシームレスに取り込める。
- **自然言語精度の飛躍的向上**:
  - Ossie の `ai_context.synonyms`（類義語リスト）を活用することで、Jev の判別精度およびスロットバインディングの堅牢性が大幅に向上する。
- **保守性と責務分離の明確化**:
  - 「ビジネスルール（設定）」と「決定論的合成エンジン（コード）」が完全に分離され、長期的なメンテナンス性が担保される。

### Negative (デメリット/コスト)

- **YAML パーサーおよび Ossie スキーマローダーの実装コスト**:
  - `PyYAML` の導入と、Ossie ドキュメント形状（`version: 0.2.0.dev0`, `datasets`, `relationships`, `metrics`）のバリデーション実装が必要となる。
- **スキーマ定義の記述量増加**:
  - 単純な DDL に比べ、メトリクスやリレーション、シノニムの定義を記述する必要がある（ただし、Ossie の既存コンバーターや自動生成スクリプトで緩和可能）。

### Risks (リスク)

- **Apache Ossie の仕様変更リスク**:
  - Ossie は現在 `0.2.0.dev0` のドラフト段階にあり、将来マイナーなフィールド名変更が発生する可能性がある。
  - *対策*: アダプター層（`OssieSchemaAdapter`）を設けて内部モデル `SchemaMetadata` と疎結合に保つことで、仕様変更時の影響をアダプター内に局所化する。

## Considered Alternatives (検討した選択肢)

1. **独自 JSON スキーマの拡張 (Status Quo Extended)**:
   - *Pros*: 現行 `tpch_schema.json` の延長で実装が最も手軽。
   - *Cons*: 独自形式（オレオレ仕様）となり、外部ツールやコミュニティとの相互運用性が皆無。将来的なエコシステム拡大のボトルネックとなる。
   - *Result*: 却下。
2. **dbt Semantic Layer / MetricFlow への直接依存**:
   - *Pros*: dbt コミュニティで広く普及している。
   - *Cons*: dbt 専用の構文・ツールチェインへの強いベンダーロックインが発生し、軽量なスタンドアロン CLI / 将来の MCP サーバー用途には過大。
   - *Result*: 却下。
3. **Apache Ossie (incubating) の採用 (Selected)**:
   - *Pros*: ベンダー中立のオープン標準であり、AI エージェントと BI ツールの連携を主目的としているため、本エンジンの設計思想と 100% 合致。
   - *Result*: 採用。
