# アーキテクチャ決定レコード (Architecture Decision Records)

本ディレクトリには、`deterministic-text-to-sql` プロジェクトにおける重要なアーキテクチャ上の決定事項（ADR）を記録・永続化しています。

## 📋 ADR 一覧

| 番号 | タイトル | ステータス | 決定日 | 概要 |
| :---: | :--- | :---: | :---: | :--- |
| [0001](0001-adopt-apache-ossie-for-semantic-metadata.md) | セマンティックメタデータ標準仕様としての Apache Ossie の採用 | **Accepted** | 2026-10-17 | 任意のデータベースに対応可能な汎化エンジンを実現するため、外部設定ファイルの標準フォーマットとして Apache Ossie (incubating) を全面採用する。 |
| [0002](0002-multi-dimensional-grouping-semantic-time-dimensions-and-having-filter.md) | 多軸集計、メタデータ駆動時間ディメンション、および集計値フィルタ（HAVING）のサポート | **Accepted** | 2026-10-18 | 実務のアドホック分析で頻出する多軸クロス集計（GROUP BY col1, col2）、時系列粒度（年別・月別）、集計値絞り込み（HAVING）をメタデータ駆動で安全に実現する。 |
| [0003](0003-window-functions-and-two-pass-cte-synthesis.md) | ウィンドウ関数および2段CTEクエリ合成によるグループ内Top-Nと時系列推移（YoY）のサポート | **Accepted** | 2026-10-19 | 実務アドホック分析で頻出する「グループ内Top-N」や「前年比成長率（YoY）」を、2段CTEクエリ合成とWindowSlotモデルにより安全・決定論的に実現する。 |
