# Security-first Design

公開リポジトリ・公開デモとして運用するための、秘密情報の扱いと検証方法です。

## 秘密情報の置き場所

| 置き場所 | 内容 |
| --- | --- |
| Render の Environment（`render.yaml` は `sync: false` のキー名だけ） | 本番の API キー・DB 接続文字列・トークン |
| ローカルの `.env`（`.gitignore` 対象） | 開発用の値 |
| `.env.example`（唯一コミットする設定ファイル） | キー名と `YOUR_...` のダミー値・空欄だけ |

`.gitignore` は `.env` / `.env.*`（`.env.example` を除く）/ `credentials.json` / `token.json` / `client_secret*.json` / `service-account*.json` / `*.pem` / `*.key` / `*.p12` / `.envrc` / `.secrets/` を除外します。

## 表示・保存前のマスキング（`src/karry_agent/secret_masking.py`）

| 対象 | 方式 |
| --- | --- |
| 環境変数の秘密値（キー名が token/secret/password/api key/DATABASE_URL 等） | `abc...wxyz` 形式。16文字未満は `[REDACTED]` |
| 既知のトークン形式 | `sk-...xxxx`、`AIza...xxxx`、`ghp_...xxxx`、`github_pat_...`、`rnd_...`、`r8_...`、`AKIA...`、`xox?-...`、`ya29....` |
| DB / Redis 接続文字列 | `postgresql://[REDACTED]@host/db` |
| `Bearer <token>` / 秘密鍵 PEM | マスク |
| 秘密を表すフィールド名（`api_key`、`password`、`token` 等） | 値ごと `[REDACTED]`（トークン数メトリクス・ページングトークンは除外） |

適用箇所:

- チャット応答の本文 `text`、実行トレース `trace`（`/chat`・`/run`・`/agent/run`・`/runs/{id}`・タスク履歴・SSE の `started`/`step`/`completed`）
- 直接ツール API `POST /api/agent/tools/execute` の結果
- Knowledge Sync API の保存内容と一覧の preview
- Web UI「実行トレース」描画時にも同じパターンと入力済みの所有者トークンを伏せ字化（多層防御）

エラー詳細・診断ログは従来どおり `self_healing.redact` を通します。`PARTNER_NAME` など個人コンテキストは資格情報ではないため、エージェントの応答では伏せません（診断ログでは伏せます）。

## 認証

- 所有者 API は `KARRY_API_TOKEN` 必須。未設定なら 503 で拒否（fail-closed）、比較は `secrets.compare_digest`。
- `/health/calendar` などの公開ヘルスチェックは値を返さず、有無と形式だけを返します。

## 自動検証

`tests/test_secret_hygiene.py` を CI で毎回実行します。

- `.env` や鍵ファイルが Git 管理に入っていない
- 追跡ファイルに実トークン形式の文字列が無い
- `.env.example` の秘密キーが空欄か `YOUR_...` だけ
- `.gitignore` が必要なパターンを含む

`tests/test_secret_masking.py` は、ツールが `.env` を読んだ場合でも応答・トレース・SSE・実行履歴に鍵が出ないことを確認します。
