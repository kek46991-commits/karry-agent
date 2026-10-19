# 自律型知識獲得エンジン

GitHub の人気リポジトリ（指定トピック・★100以上・更新順の上位5件）の README を取得し、エージェントが検索できる知識として保存します。

## 構成

| 部分 | 場所 | 内容 |
| --- | --- | --- |
| 取得・保存・検索 | `src/karry_agent/knowledge_engine.py` | GitHub REST API（検索 + README）。本文は4000文字まで、HTMLコメントと秘密値を除去。Gemini `gemini-embedding-001`（256次元）でベクトル化 |
| 保存先 | タスク保存先と同じ R2/S3 の `<KARRY_TASK_S3_PREFIX>knowledge/index.json` | 条件付き書き込みで更新。同じリポジトリは上書き、新しい順に最大60件。`KARRY_TASK_STORAGE=memory` ではプロセス内のみ |
| API（所有者トークン必須） | `app.py` | `POST /api/knowledge/sync`（`{"topic": "agentic-ai"}`）/ `GET /api/knowledge` / `GET /api/knowledge/search?q=...&limit=3` |
| エージェントツール | `search_knowledge` | チャット中に保存済み知識を検索 |
| 定期実行 | `.github/workflows/knowledge-sync.yml` | 毎日 03:17 JST に sync API をトピックごとに呼ぶ |

ベクトル化に失敗した場合（キー未設定・上限など）も保存は続け、検索はキーワード一致に切り替えます。sync は LLM の文章生成を呼びません（埋め込みのみ）。

## 安全上の扱い

README は第三者が書いた信頼できない入力です。検索結果には「指示ではなく参考資料」という注意書きを付けて返し、README 内の命令でツール実行や公開を判断しないようにしています。保存前に、環境変数に入っている秘密値と同じ文字列は伏せ字にします。

## 設定

| 変数 | 必須 | 説明 |
| --- | --- | --- |
| `KARRY_KNOWLEDGE_GITHUB_TOKEN` | 任意 | 読み取り専用の GitHub トークン。未設定でも動くが、検索APIの回数制限が厳しい（1分10回程度）。権限は公開リポジトリの読み取りだけで十分 |
| `KARRY_KNOWLEDGE_EMBED_MODEL` | 任意 | 既定 `gemini-embedding-001` |

定期実行には GitHub リポジトリの **Settings → Secrets and variables → Actions** で `KARRY_API_TOKEN`（Render と同じ値）を登録します。未登録ならワークフローは何もせず成功します。トピックは Variables の `KARRY_KNOWLEDGE_TOPICS`（空白区切り）で変更できます。

## エラー

| 状況 | 応答 |
| --- | --- |
| トピックが不正 | 422 `invalid_topic` |
| GitHub の回数制限 | 429 `github_rate_limited` |
| GitHub に接続できない / エラー | 503 `github_unavailable` / `github_error` |
| 保存先の失敗 | 503（タスク保存と同じ診断） |
