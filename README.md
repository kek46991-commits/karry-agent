# karry-agent

Gemini APIを活用した自律型AIエージェントのコアエンジンです。
Function Calling (ツール連携) 機能に対応しており、外部ツールの実行や最新情報の取得が可能です。

## 🚀 機能 (Features)

- Gemini API (`gemini-2.5-flash`) 統合
- 堅牢なエラーハンドリング & 指数バックオフ再試行
- `progress.md` へのタスク進捗ログ記録
- 対話型 CLI インターフェース (`cli.py`)
- **Function Calling ツール連携 (`tools.py`)**
- Phase 4 第1ステップ: 現在日時取得・Web検索モックツール
- Phase 5 第1ステップ: Imagen 連携に向けた画像生成モックツール
- Phase 6 第1ステップ: 会話履歴を管理する `ConversationMemory`
- Phase 10: 実 Gemini API を使う ReAct / Function Calling と安全なコマンド実行
- Phase 11: Thought–Action–Observation 実行トレースとローカルスクリプト実行
- Phase 12: API 使用量・実行時間メトリクスと堅牢なエラー処理
- Phase 13: 複数サブエージェントの委譲・並列実行・結果集約
- Phase 14: MVP 統合検証と Devin 引き渡し向けドキュメント
- Phase 16: FastAPI チャット・実行追跡 API とワークスペース一覧ツール

## 🚀 クイックスタート

### 1. 環境構築

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 内の GEMINI_API_KEY に自身のAPIキーを入力
python3 cli.py
python3 -m pytest -q
```

### 3. Docker で実行

```bash
export GEMINI_API_KEY="your-gemini-api-key"
docker compose build
docker compose run --rm karry-agent
```

対話モードを終了するには `exit` または `quit` を入力します。API キーはイメージへ埋め込まず、Compose の環境変数から渡してください。

### 2. 外部ツールの確認

```python
from tools import AGENT_TOOLS, get_current_datetime, web_search_mock

print(get_current_datetime())
print(web_search_mock("Gemini API"))
from tools import generate_image_mock
print(generate_image_mock("青い空を飛ぶ白い鳥"))
print(AGENT_TOOLS)
```

### 4. 実 Gemini API の動作確認

`GEMINI_API_KEY` を設定すると、`AgentCore` は `google-genai` SDK の実 API に接続します。既定モデルは `gemini-3.5-flash` で、`GEMINI_MODEL` で変更できます。API 障害・タイムアウト時は指数バックオフで再試行します。

```bash
GEMINI_API_KEY="..." python3 -m karry_agent.cli "README.md の概要を説明してください"
```

エージェントはモデルの Function Call に応じて、ファイル読み書き、Web 検索、画像生成モック、現在日時取得、安全な読み取り系コマンドを実行します。コマンドは shell を経由せず、allowlist とワークスペース制限を適用します。実 API を使うテストは、課金・レート制限を避けるため既定ではスキップされます。

Phase 11 では各ツール呼び出しを `AgentResult.trace` に記録します。トレースには Action 名、引数、Observation、実行時間が含まれます。`run_local_script` はワークスペース内の既存 `.py` ファイルだけを `shell=False` で実行し、引数・タイムアウト・出力サイズを制限します。

Phase 12 では `AgentResult.metrics` に API 呼び出し回数、再試行回数、入力・出力・合計トークン数、ツール呼び出し数、エラー数、総実行時間（ms）を記録します。API エラーとタイムアウトは指数バックオフで自動再試行し、上限到達時は例外を外へ投げずエラー結果へフォールバックします。SDK の usage metadata が返らない場合も処理を継続します。

Phase 13 の `MultiAgentOrchestrator` は、専門分野付きの `DelegatedTask` を複数のサブエージェントへ並列委譲し、結果を入力順に集約します。サブエージェントの失敗は他タスクへ波及せず、タイムアウトやエラーも結果として確認できます。

Phase 14 のシステム構成、公開 API、セットアップ、テスト戦略、Devin 引き渡しチェックリストは [docs/architecture.md](docs/architecture.md) にまとめています。`tests/test_phase14.py` は Gemini 応答モックから Function Calling、ツール Observation、`AgentResult.metrics`、マルチエージェント集約までを一つの E2E フローとして検証します。

Phase 16 の FastAPI では `/chat` がセッション履歴を保持し、`/runs/{run_id}` で完了した実行のトレースとメトリクスを取得できます。`list_workspace` ツールは隠しファイルを除外し、ワークスペース外のパスを拒否します。

### Phase 17: ストリーミングとファイル操作

`POST /chat` に `{"stream": true}` を指定すると、SSE 形式で `started`、`step`（Thought/Action/Observation の実行トレース）、`completed` または `error` イベントを返します。現段階では実行本体を安全に完了させた後にトレースを送る基盤実装であり、真の逐次 Thought 生成は今後のストリーミング SDK 対応で拡張します。

追加ツール:

- `browse_path`: ワークスペース内のファイル内容またはディレクトリ一覧を閲覧
- `append_text_file`: UTF-8 テキストファイルへ追記

いずれもワークスペース外を拒否し、ファイルサイズ・一覧件数を制限します。

## Render への永続デプロイ

リポジトリには Render Blueprint 用の [render.yaml](render.yaml) を含めています。Render Dashboard で **New → Blueprint** を選択し、GitHub の `main` リポジトリを接続して適用してください。

Render の Environment 画面で `GEMINI_API_KEY` を Secret として設定します。`GEMINI_MODEL`、`KARRY_WORKSPACE`、`uvicorn app:app --host 0.0.0.0 --port $PORT`、`/health` のヘルスチェックは `render.yaml` から設定されます。詳細は [HANDOFF.md](HANDOFF.md) の Render 手順を参照してください。

## ジミーモード（プライベートモード）

入力またはセッション内で環境変数 `SECRET_PASSPHRASE` に設定した合言葉 が認証されると、そのセッションの AgentCore にだけジミー用のシステム指示が追加されます。ジミーは、かなまる（岸恵）の相棒・友達として親しみのある大阪弁で応答します。合言葉のないセッションにはプライベートなプロフィールや思い出の指示を渡しません。

この機能はセッション単位のインメモリ認証です。Render などで複数インスタンスを運用する場合は、セッション認証ストアを Redis などへ移行し、認証・監査・失効・レート制限を追加してください。
Render でジミーモードを有効にする場合は、`SECRET_PASSPHRASE` も Environment の Secret として設定してください。未設定ならプライベートモードは無効化され、通常モードで安全に動作します。

### `/agent/run` 互換エンドポイント

旧 Jimmy-Core 形式の `{ "task": "...", "session_id": "optional" }` を受け付ける互換 API を追加しています。内部では既存の `AgentCore`、Function Calling、メトリクス、プライベートモード認証を利用し、旧 `google.generativeai` SDK は使用しません。

### API キー未設定時のエラー

Gemini API キーが未設定の Web Service は、`Internal Server Error` ではなく HTTP 503 と `GEMINI_API_KEY is not set...` の JSON を返します。Render の Environment で `GEMINI_API_KEY` を Secret として設定し、再デプロイしてください。

### Google Calendar 連携

`add_calendar_event` を Function Calling ツールとして登録しています。予定登録は外部データを書き換えるため、最初の呼び出しではプレビューのみ返し、ユーザーが内容を明示確認して `confirmed=true` を指定した場合だけ Google Calendar API を実行します。

Calendar 連携では、メインの Function Calling ループから次の 3 ツールを利用できます。

- `list_calendar_events`: `time_min`、`time_max`、`query` で予定を検索する読み取り専用ツール
- `add_calendar_event`: 新規予定をプレビューし、`confirmed=true` のときだけ登録するツール
- `update_calendar_event`: `event_id` と変更項目を指定してプレビューし、`confirmed=true` のときだけ変更するツール

取得はそのまま実行できます。追加・変更は、エージェントが件名・日時・対象イベントを明示した確認メッセージを返し、ユーザーが承認した後の呼び出しだけが外部 API を変更します。

ローカル設定:

```bash
pip install -r requirements.txt
# Google Cloud Console で OAuth Desktop credentials を取得
export GOOGLE_CALENDAR_CREDENTIALS_FILE=/secure/path/credentials.json
export GOOGLE_CALENDAR_TOKEN_FILE=/secure/path/token.json
export GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH=1
```

初回 OAuth はローカル端末でのみ実行してください。`credentials.json` と `token.json` は `.gitignore` 対象で、Git、Docker image、ログへ含めないでください。Render のようなヘッドレス環境では既存の OAuth token を Secret / 安全な永続ストレージから提供し、対話型ログインを起動しないでください。

Render Blueprint では `GOOGLE_CALENDAR_TOKEN_JSON` と `GOOGLE_CALENDAR_CREDENTIALS_JSON` を Secret として定義しています。ローカルで OAuth を完了した後、Render Dashboard の Environment に JSON の内容を Secret として登録してください。アプリは起動時に `/tmp/karry-agent/` へ mode `600` の一時ファイルとして復元します。Render のファイルシステムは永続ではないため、再起動後も Secret から再復元されます。Render では `GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH` を設定せず、ヘッドレス OAuth を起動しないでください。

Render への設定は `scripts/push_calendar_env.py` で自動化できます（標準ライブラリのみ、Termux でも動作）。`credentials.json` / `client_secret*.json` / `token.json` をカレントディレクトリ・リポジトリ・ホーム・Download から自動検索し、JSON 形式と必須項目（`installed`/`web` の `client_id`・`client_secret`、token の `refresh_token`）を検証して 1 行 JSON として Render API に登録し、デプロイします。値は表示しません。

```bash
export RENDER_API_KEY=rnd_xxx
python scripts/push_calendar_env.py --render          # ローカルのファイルを登録してデプロイ
python scripts/push_calendar_env.py --login --render  # token.json をブラウザ認証で作成してから登録
python scripts/push_calendar_env.py --from-render     # Render の既存値を正規化して再登録
```

アプリ側も前後の空白・BOM・外側のクォート・`KEY=` 付き・二重エンコード・base64 の値を自動で正規化します。設定状態は `GET /health/calendar` で確認でき、レスポンスには有無と形式エラーだけが含まれ、値は含まれません。

本番の Render サービス構成（`karry-agent-1` が本番）、環境変数の一覧、PR #3 の変更内容、Termux での再設定手順、トラブルシューティングは [docs/calendar-setup.md](docs/calendar-setup.md) にまとめています。
