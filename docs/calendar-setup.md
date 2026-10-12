# Google Calendar 連携 セットアップ記録

Google Calendar 連携を Render 本番で有効化した作業の記録です（2026-10-12 時点）。秘密の値（OAuth クライアント、token、API キー）はこの文書にも Git にも含めません。

## 現在の状態

| 項目 | 状態 |
|---|---|
| 関連 PR | [#3 fix(calendar): normalize Google OAuth secrets and automate Render setup](https://github.com/kek46991-commits/karry-agent/pull/3)（`main` にマージ済み、マージコミット `99c9dec`） |
| 本番サービス | Render `karry-agent-1`（`https://karry-agent-1.onrender.com`） |
| Calendar Secret | `GOOGLE_CALENDAR_CREDENTIALS_JSON` / `GOOGLE_CALENDAR_TOKEN_JSON` 登録済み・形式 OK |
| ヘルスチェック | `GET /health/calendar` → `{"ready": true, ...}` |
| 実 API 確認 | 登録済み token で Google Calendar API の予定取得が成功（`list_calendar_events`） |

## Render のサービス構成

同じリポジトリ・`main` ブランチから 2 つの Web サービスが自動デプロイされています。

| サービス名 | URL | 用途・設定 |
|---|---|---|
| `karry-agent-1` | `https://karry-agent-1.onrender.com` | 普段使う本番。`GEMINI_API_KEY` と Calendar Secret を設定済み |
| `karry-agent` | `https://karry-agent.onrender.com` | `render.yaml` と同名のサービス。Calendar Secret はあるが `GEMINI_API_KEY` がなく、チャットは使えない |

`scripts/push_calendar_env.py` の既定のサービス名は `karry-agent` です。本番へ登録するときは `--service-name karry-agent-1` を付けてください。使わない方のサービスは、取り違えを防ぐため削除を推奨します。

## 環境変数

| キー | 値 | 備考 |
|---|---|---|
| `GOOGLE_CALENDAR_CREDENTIALS_JSON` | Google Cloud の OAuth クライアント（デスクトップアプリ）の `credentials.json` | Secret。`installed` または `web` に `client_id` / `client_secret` が必要 |
| `GOOGLE_CALENDAR_TOKEN_JSON` | OAuth 認証で作った `token.json`（`authorized_user` 形式） | Secret。`refresh_token` が必要。`client_id` / `client_secret` がなければ credentials から補完 |
| `GOOGLE_CALENDAR_CREDENTIALS_FILE` | `/tmp/karry-agent/credentials.json` | 起動時に Secret を復元する先 |
| `GOOGLE_CALENDAR_TOKEN_FILE` | `/tmp/karry-agent/token.json` | 起動時に Secret を復元する先 |

Render 上では `GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH` を設定しないでください（ヘッドレス環境でブラウザ認証を起動しないため）。

## PR #3 で変わったこと

- `src/karry_agent/calendar_secrets.py`（新規）: Secret 値の正規化と検証。前後の空白、BOM、外側の `'` `"` `` ` ``、`KEY=` / `export KEY=` 付き、二重エンコード、base64 を自動で取り除いて読みます。エラーメッセージには値を含めず、JSON の行・列だけを示します。
- `src/karry_agent/tools.py`: 起動後の最初の 1 回だけ Secret を `/tmp` に mode `600` で書き出し、古いファイルが残っていても上書きします。アクセストークンが無効で `refresh_token` がある場合は必ず refresh します。
- `app.py`: `GET /health/calendar` を追加しました。2 つの Secret の有無（`configured`）と形式（`valid`）だけを返し、値は返しません。
- `scripts/push_calendar_env.py`（新規）: 標準ライブラリだけで動く Termux 用スクリプトです。
  - `credentials.json` / `client_secret*.json` / `token.json` を、カレントディレクトリ・リポジトリ・ホーム・Download から自動で探します。
  - `--login`: PKCE とループバック（`127.0.0.1` のランダムポート）による OAuth で、ブラウザ認証して `token.json` を作ります（`access_type=offline`、`prompt=consent`）。
  - `--render`: Render API で 2 つの Secret を 1 行 JSON として登録し、デプロイを開始します。
  - `--from-render`: Render に入っている既存の値を正規化して登録し直します。
- `tests/test_calendar_secrets.py`: 正規化・検証・復元・`/health/calendar`・スクリプト・OAuth ログインのテストを追加しました（全体で `100 passed, 2 skipped`）。

## 再設定の手順（Termux）

token の失効や、Google アカウントの変更をしたとき用の手順です。

```bash
pkg install -y python git
git clone https://github.com/kek46991-commits/karry-agent.git ~/karry-agent  # 初回のみ
cd ~/karry-agent && git pull
# credentials.json を ~/karry-agent/ に置く
export RENDER_API_KEY=rnd_xxx
python scripts/push_calendar_env.py --login --render --service-name karry-agent-1 --credentials credentials.json
```

1. ブラウザが開くので、Google アカウントでログインして「許可」を押します。
2. Termux に `token.json を作成しました`、`JSON の形式チェック: OK`、`デプロイを開始しました` と出れば完了です。
3. デプロイ後、`https://karry-agent-1.onrender.com/health/calendar` が `"ready": true` になっていることを確認します。

token.json がすでにある場合は、`--login` を外して実行します。

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| チャットで「Google Calendar の連携設定が未設定」と返る | 開いているサービスに Secret がありません。URL が `karry-agent-1` か確認し、`/health/calendar` を開いてください |
| `/health/calendar` で `valid: false` | `error` に出る行・列を確認し、ファイルを作り直して再登録してください |
| `access_denied` | OAuth 同意画面の「テストユーザー」に、使う Gmail を追加してください |
| `redirect_uri_mismatch` | OAuth クライアントが「ウェブアプリ」です。「デスクトップアプリ」で作り直してください |
| `refresh_token が返されませんでした` | Google アカウントのアプリのアクセス権を削除してから、`--login` を再実行してください |
| `Render service ... was not found` | `--service-name` に Render の画面にあるサービス名を指定してください |

## セキュリティ

- `credentials.json`、`token.json`、`client_secret*.json` は `.gitignore` 済みです。Git、Docker image、ログに含めないでください。
- Render API キーや GitHub トークンをチャットや Issue に貼った場合は、すぐに削除して作り直してください。
