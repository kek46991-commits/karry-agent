# karry-agent 引き継ぎ資料

**対象:** 次担当の開発者・AI エージェント
**リポジトリ:** `kek46991-commits/karry-agent`  
**ブランチ:** `main`
**現行状態:** Gemini エージェント、FastAPI、日本語 Web UI、SSE、音声入出力、Render Blueprint、環境変数ベースのプライベートモードを実装済み

## 1. プロジェクト概要

`karry-agent` は `google-genai` SDK を利用する自律型 AI エージェントです。Gemini の Function Calling でツールを選択し、Thought–Action–Observation の ReAct ループでツール結果を次の判断へ渡します。API 回数、retry、token、tool call、エラー、実行時間のメトリクスを記録し、FastAPI と日本語 Web UI から利用できます。

現在の API と UI は MVP / プロトタイプです。認証、レート制限、永続的なジョブキュー、Redis/DB によるセッション永続化、厳格な監査を追加してから不特定多数へ公開してください。

## 2. 実装済み機能

| 領域 | 実装内容 |
|---|---|
| Gemini | `google-genai`、`GEMINI_API_KEY`、`GEMINI_MODEL`、指数バックオフ再試行 |
| AgentCore | 最大ステップ付き ReAct、Function Calling、エラー・タイムアウト処理 |
| ツール | 日時、Web 検索、画像生成モック、ファイル読み書き、閲覧・追記、安全なコマンド、Python スクリプト |
| メモリ | `ConversationMemory` による会話履歴上限制御 |
| マルチエージェント | 委譲、並列実行、結果集約、失敗隔離、タイムアウト |
| 監視 | API 回数、retry、token、tool call、error、elapsed の `AgentMetrics` |
| FastAPI | `/health`、`/run`、`/chat`、SSE、実行取得、セッション履歴、OpenAPI UI |
| Web UI | 日本語チャット、SSE トレース、実行メトリクス、モバイル対応 |
| 音声 | ブラウザ標準 `SpeechRecognition` / `webkitSpeechRecognition` による音声入力、`SpeechSynthesis` による読み上げ、自動読み上げ切り替え |
| 配布 | Docker、Compose、GitHub Actions、Kubernetes/Helm、Render Blueprint |
| プライベートモード | `SECRET_PASSPHRASE` によるセッション単位のジミーモード認証 |

### フェーズ履歴

- Phase 1–6: Gemini 接続、Function Calling、ログ、CLI、画像生成モック、会話メモリ
- Phase 7–9: Docker、GitHub Actions、Kubernetes/Helm のデプロイ構成
- Phase 10–14: 実 Gemini、ローカルツール、ReAct トレース、メトリクス、マルチエージェント、E2E
- Phase 15–17: FastAPI、Web UI、チャットセッション、実行追跡、SSE、ワークスペースツール
- 追加仕上げ: 日本語音声入出力、環境変数ベースのプライベートモード認証

## 3. ディレクトリ構成

```text
.
├── app.py                         # FastAPI アプリ、SSE、静的 UI、セッション/実行管理
├── static/index.html              # 日本語チャット UI、音声入出力、SSE クライアント
├── main.py                        # CLI / Docker 起動エントリーポイント
├── cli.py / core.py / tools.py    # ルート互換エントリーポイント
├── Dockerfile
├── docker-compose.yml
├── render.yaml                    # Render Blueprint
├── .renderignore
├── requirements.txt
├── pyproject.toml
├── .env.example                   # 値を含まない環境変数テンプレート
├── .gitignore                     # .env / Secret ファイルを除外
├── README.md
├── HANDOFF.md
├── docs/architecture.md
├── deploy/helm/karry-agent/       # Kubernetes / Helm
├── src/karry_agent/
│   ├── core.py                    # AgentCore、AgentResult、AgentMetrics
│   ├── privacy.py                 # 環境変数ベースのジミーモード認証
│   ├── tools.py                   # ToolRegistry と built-in tools
│   ├── memory.py                  # ConversationMemory
│   ├── orchestrator.py             # MultiAgentOrchestrator
│   ├── cli.py
│   ├── logger.py
│   └── __init__.py
└── tests/                         # Phase 別 unit / integration tests
```

`.env`、`.venv`、キャッシュ、`progress.md`、Secret / key ファイルはローカル専用で、Git 管理対象外です。

## 4. 環境構築

```bash
git clone https://github.com/kek46991-commits/karry-agent.git
cd karry-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
```

`.env` の例（秘密値は入力環境だけに保存し、Git にコミットしない）:

```env
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
SECRET_PASSPHRASE=
KARRY_WORKSPACE=.
```

主な環境変数:

| 変数 | 用途 |
|---|---|
| `GEMINI_API_KEY` | Gemini API 認証。実 API 利用時に必要 |
| `GEMINI_MODEL` | 利用モデル。既定は `gemini-3.5-flash` |
| `SECRET_PASSPHRASE` | ジミーモードの秘密合言葉。未設定なら認証を無効化 |
| `KARRY_WORKSPACE` | ファイル・スクリプト操作のルート |
| `RUN_LIVE_GEMINI_TESTS` | `1` のときだけ実 API テストを有効化 |

## 5. 起動方法

### CLI

```bash
python -m karry_agent.cli "プロジェクトの概要を説明してください"
# または
python main.py
```

### FastAPI / Web UI

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

ブラウザで以下を開きます。

- `http://localhost:8000/` — 日本語 Web UI
- `http://localhost:8000/docs` — OpenAPI UI
- `http://localhost:8000/health` — ヘルスチェック

### Docker

```bash
export GEMINI_API_KEY="..."
docker compose build
docker compose run --rm karry-agent
```

### Render

`render.yaml` は Python Web Service として次を設定します。

- `pip install --upgrade pip && pip install -r requirements.txt && pip install .`
- `uvicorn app:app --host 0.0.0.0 --port $PORT`
- `main` ブランチの自動デプロイ
- `/health` のヘルスチェック
- `GEMINI_API_KEY` と `SECRET_PASSPHRASE` を `sync: false` の Secret として定義

Render Dashboard で **New → Blueprint** を選択して GitHub リポジトリを接続し、Environment 画面で Secret 値を設定してください。未設定の `SECRET_PASSPHRASE` はプライベートモードを無効化します。Render の `/tmp/karry-agent` は一時領域であり、永続ファイル保存には外部ストレージが必要です。

## 6. FastAPI API

| Method | Path | 内容 |
|---|---|---|
| GET | `/health` | サービス状態 |
| GET | `/health/calendar` | Calendar Secret の有無と形式（値は返さない） |
| GET | `/` | `static/index.html` |
| POST | `/run` | 単発 Agent 実行。`run_id`、trace、metrics を返す |
| POST | `/chat` | セッション履歴付き実行。`stream: true` で SSE |
| GET | `/runs/{run_id}` | 実行結果・トレース・メトリクス |
| GET | `/sessions/{session_id}` | 会話履歴 |
| GET | `/docs` | OpenAPI UI |

`/chat` の例:

```json
{
  "prompt": "現在時刻を確認してください",
  "session_id": "optional-session-id",
  "stream": true
}
```

SSE は `started`、`step`、`completed`、`error` イベントを返します。現状は Agent 実行完了後に取得済みのトレースを送る基礎実装であり、モデル生成中の逐次中継には Gemini streaming API と非同期ジョブキューが必要です。

## 7. ジミーモードとセキュリティ

`SECRET_PASSPHRASE` が設定されている場合、入力内にその値が含まれるとセッションが認証済みになります。認証済みセッションだけに `AgentCore.run(..., private_mode=True)` の追加システム指示が渡され、ジミー人格が有効になります。通常モードにはプライベートなプロフィールや思い出の指示を渡しません。

環境変数が未設定または空の場合は、合言葉認証を無効化した通常モードで動作します。合言葉そのものをコード・README・`.env.example`・ログへ書かないでください。Render では必ず Secret として登録してください。

現在の認証状態はインメモリです。サーバー再起動で失効し、複数インスタンス間でも共有されません。本番では Redis/DB、認証失効、監査ログ、レート制限、HTTPS、tenant isolation を追加してください。API は現状認証なしのため、公開前に保護が必要です。

## 8. テストと CI

```bash
python -m pytest -q
python -m compileall -q src app.py
```

現行のローカル結果は `100 passed, 2 skipped` です。実 API テストは明示的に有効化した場合だけ実行します。

```bash
RUN_LIVE_GEMINI_TESTS=1 python -m pytest -q
```

GitHub Actions は `main` への push / pull request で依存関係を導入してテストします。主なテスト対象は AgentCore、Function Calling、ツール安全性、メトリクス、マルチエージェント、FastAPI、SSE、Web UI、音声 API の静的存在、Render 設定、プライベートモード認証です。

## 9. 次の推奨作業

1. FastAPI に API key/OAuth2、CORS 制限、レート制限、request quota を追加する。
2. SessionStore / RunStore を Redis または DB へ移行する。
3. SSE を本当の非同期ジョブ API と Gemini streaming API に接続する。
4. 音声機能にブラウザ権限説明、音声選択、停止ボタン、Safari/Firefox 差異対応を加える。
5. Render の無料プラン制約を確認し、本番は有料プラン・外部ストレージ・監視を設計する。
6. 実 Web 検索、Imagen/Nano Banana などの外部 adapter は認証・コスト・出典管理を含めて導入する。

## 10. 引き継ぎ時の確認事項

- `.env`、API キー、`SECRET_PASSPHRASE` の実値をコミットしない。
- `git status --short --branch` が clean で、`main` と `origin/main` が一致していることを確認する。
- `python -m pytest -q` と `git diff --check` を実行する。
- 本番公開前に認証、監査、レート制限、永続化、秘密情報のマスキングを再確認する。

## 11. `/agent/run` 互換 API

旧サンプルの `task` 形式を受け付ける `POST /agent/run` を `app.py` に追加しました。内部では既存の `AgentCore` と `google-genai` SDK、ツール実行、メトリクス、`SECRET_PASSPHRASE` によるセッション認証を利用します。旧 `google.generativeai` の直接呼び出しや別の API キー管理は導入していません。

リクエスト例:

```json
{"task":"現在時刻を確認してください","session_id":"optional-session"}
```

レスポンスには `status`、`task`、`thought_process`（AgentCore の最終出力）、`next_action`、`run_id`、`trace`、`metrics`、`private_mode` が含まれます。`thought_process` は内部の非公開 Chain-of-Thought を返すものではなく、ユーザー向けの実行結果です。

## 12. API キー未設定時のエラー対応

`get_agent()` の依存性注入中に `GEMINI_API_KEY` が未設定でも、無情報の 500 を返さず HTTP 503 と設定原因を示す JSON を返すようにしました。Render では Environment の `GEMINI_API_KEY` に実値を Secret として登録してから再デプロイします。

## 13. Google Calendar 連携

`src/karry_agent/tools.py` に `add_calendar_event(summary, start_time, duration_minutes=60, confirmed=False)` を追加しました。Function Calling から利用できますが、`confirmed` のデフォルトは `False` で、外部カレンダーを変更せずイベントプレビューを返します。ユーザーが日時・件名・所要時間を確認した後だけ `confirmed=true` を許可してください。

依存関係は `google-api-python-client`、`google-auth-httplib2`、`google-auth-oauthlib` です。認証ファイルは `GOOGLE_CALENDAR_CREDENTIALS_FILE`、既存 token は `GOOGLE_CALENDAR_TOKEN_FILE` で指定できます。初回の `InstalledAppFlow` は `GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH=1` のローカル環境だけで許可し、Render 等のヘッドレス Web Service では既存 token を Secret / 永続ストレージから注入します。`credentials.json`、`token.json`、`client_secret*.json` は `.gitignore` 済みです。

### Render 本番への設定（PR #3）

[PR #3](https://github.com/kek46991-commits/karry-agent/pull/3) で、Secret 値の正規化・検証（`calendar_secrets.py`）、起動時の `/tmp` への再復元、`GET /health/calendar`、Termux 用の `scripts/push_calendar_env.py`（ファイル自動検索、`--login` によるブラウザ OAuth、Render への登録とデプロイ）を追加しました。

Render には同じリポジトリから `karry-agent-1`（本番、`GEMINI_API_KEY` あり）と `karry-agent`（`render.yaml` と同名、`GEMINI_API_KEY` なし）の 2 サービスがあります。2026-10-12 時点で両方に `GOOGLE_CALENDAR_CREDENTIALS_JSON` / `GOOGLE_CALENDAR_TOKEN_JSON` を登録済みで、`/health/calendar` は `ready: true` です。登録した token で予定の取得ができることも確認しました。スクリプトで本番へ登録するときは `--service-name karry-agent-1` を指定してください。詳細は [docs/calendar-setup.md](docs/calendar-setup.md) を参照してください。

### 2026-10-13 Calendar追加不具合の修正

- 原因1: ヘッドレス起動時に `GOOGLE_CALENDAR_CREDENTIALS_JSON` を先に検証していたため、credentials Secret が不正形式でも、client 情報を含む有効な `GOOGLE_CALENDAR_TOKEN_JSON` が使われる前に処理が停止していた。
- 原因2: プレビュー後のユーザー確認を会話モデルの再判断だけに任せていたため、`confirmed=true` の insert が呼ばれず、確認後もプレビューで終わる場合があった。
- 修正: `src/karry_agent/tools.py` は token の復元・読み込み・refresh を先に行い、必要な場合だけ credentials/OAuthへフォールバックする。`app.py` はセッションごとに保留中の予定を保持し、「はい」「登録してください」などの明示確認を受けたら `add_calendar_event(..., confirmed=True)` を直接実行する。キャンセル語では保留を破棄する。
- 検証: Calendar関連テスト 31件が通過し、ローカルOAuth tokenで `list_calendar_events(max_results=1)` が成功した。秘密値・token内容はログへ出していない。
- 本番確認: `GET /health/calendar` で token が `valid: true` であること、OAuth token の scope が `https://www.googleapis.com/auth/calendar.events` であることを確認する。公開統合版にも同じ `tools.py` と確認処理を反映する。
