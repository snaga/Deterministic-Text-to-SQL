# `jev` サブパッケージ仕様 (L1 Intent)

本ドキュメントは、`deterministic_text_to_sql.jev` パッケージの設計意図、モジュール境界、TypeSafe Jev System 1 判別エンジンの特性、および HTTP 通信の設計方針を永続化するための仕様書（L1 Intent）です。

---

## 1. 概要と責務

`jev` パッケージは、自然言語クエリからデータベースの意図スロット（主テーブル、副テーブル、集約指標、射影ディメンション、ランキング、曖昧性、フィルタ条件）を高速かつ決定論的に抽出し、スキーマメタデータに定義されたホワイトリストへ安全にバインドするセマンティック解釈層です。

### 責務 (What it does)
- **超並列スロット抽出 (`client.py`, `prompts.py`)**: TypeSafe Jev API (`POST /v1/systemone`) に構造化判別タスクを送信し、<30ms でスロット判定を取得。
- **ホワイトリスト Choice バインド (`slots.py`, F-JEV-02)**: メタデータの `values` やマスターデータ（国名・地域名等）に基づき、有効な値のみを採用。SQL インジェクション構文を物理的に遮断。
- **数値リテラル抽出 (`slots.py`)**: 自然言語クエリ内の数値表現（負数・小数・カンマ区切り）と比較演算子（以上、未満など）を正規表現と意味論に基づいて安全に抽出。
- **曖昧性検知 (HITL トリガー)**: クエリに含まれる多重解釈（売上定義の競合等）を検知し、Human-in-the-Loop 介入要求フラグ（`needs_clarification`）を設定。

### 扱わないこと (What it does NOT do)
- 外部キー探索やテーブル結合経路の解決（`graph` パッケージの責務）。
- sqlglot による AST 構築や SQL 文字列生成（`compiler` パッケージの責務）。
- CLI 出力やターミナル対話（`cli` パッケージの責務）。

---

## 2. TypeSafe Jev System 1 判別の特性

TypeSafe Jev は、汎用 LLM（System 2）のような非決定論的・長文のテキスト生成を行わず、事前に定義された選択肢（Choices）空間に対する極めて高速（<30ms）な多変量同時判別を行う System 1 特化型モデルです。

1. **決定論的 Choices バインド**:
   - 自由形式の SQL 生成ではなく、スキーマ定義から動的に生成された Choice リストに対する分類を行います。
   - 存在しないテーブルやカラムが幻覚（Hallucination）として出力されることを根本から防ぎます。
2. **高速 Keep-Alive 通信**:
   - 単一のクエリに対して 30ms 未満の応答を達成するため、HTTP Keep-Alive コネクションプールを常時維持します。

---

## 3. 技術選定理由 (Why httpx)

- **HTTP/1.1 & HTTP/2 Keep-Alive**: コネクションのハンドシェイクオーバーヘッドを排除し、ミリ秒オーダーの通信を維持します。
- **堅牢なタイムアウト制御**: ネットワーク遅延や異常時にアプリケーション全体がブロックされるのを防ぎます（デフォルト 3.0s）。
- **テスト容易性**: `respx` や `pytest-mock` による完全なモックテスト（ロンドン派）が可能であり、外部 API のダウン時や 5xx エラーのテストを再現性高く実施できます。

---

## 4. 依存制約ルール

- **許可される依存先**:
  - 標準ライブラリ (`os`, `re`, `time`, `typing`)
  - `httpx` (HTTP 通信)
  - `pydantic` (スロットモデル)
  - `deterministic_text_to_sql.schema.models` (スキーマメタデータ型定義)
  - `deterministic_text_to_sql.compiler.models` (`FilterSlot` 型定義)
- **禁止される依存先**:
  - 上位レイヤー (`engine`, `cli`) への逆依存。
  - `compiler.builder` や `graph.resolver` への依存（AST 生成やグラフ探索への干渉を排除）。

---

## 5. モジュール構成

```text
src/deterministic_text_to_sql/jev/
├── __init__.py        # エクスポート定義
├── exceptions.py      # JevError, JevAPIConnectionError, JevResponseParseError
├── models.py          # RawSlots, ParsedSlots
├── prompts.py         # Jev 判別ペイロード生成 (build_jev_payload)
├── slots.py           # スロットパース & ホワイトリストChoiceバインド (parse_jev_response)
├── client.py          # HTTP クライアント (JevClient)
└── README.md          # 本仕様書 (L1 Intent)
```
