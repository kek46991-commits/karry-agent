# karry-agent Handoff

**対象:** Devin / 次担当開発者  
**現行フェーズ:** Phase 16 — Web UI enhancement、tool expansion、handoff preparation
**リポジトリ:** `kek46991-commits/karry-agent`  
**ブランチ:** `main`

## 1. エグゼクティブサマリー

karry-agent は `google-genai` SDK と Gemini を利用する Python エージェントです。Gemini の Function Calling でツールを選択し、Thought–Action–Observation の ReAct ループでツール結果を次の判断へ渡します。Phase 12 で実行メトリクスと再試行、Phase 13 で複数サブエージェントの委譲・集約、Phase 14 でモックによる E2E 統合検証を追加しました。Phase 15 では FastAPI の薄い REST シェルと、この引き渡し文書を追加し、Phase 16 ではチャットセッション・実行追跡 API とワークスペース一覧ツールを追加しました。

現在の API シェルは **MVP 用の同期 REST エンドポイント**です。認証、レート制限、ジョブキュー、ストリーミング、永続的な会話メモリは未実装です。本番公開前にロードマップの項目を実施してください。

## 2. Phase 1–16 の成果

| Phase | 成果 |
|---|---|
| 1 | Gemini API 接続、Function Calling の基礎、ツールレジストリ、ReAct の土台 |
| 2 | 進行ログ、CLI、環境変数テンプレート、互換エントリーポイント |
| 3 | API / timeout の指数バックオフ再試行、明示的なエラー応答 |
| 4 | 日時、Web 検索、ファイル操作の Function Calling 統合 |
| 5 | 画像生成モックを Function Calling に統合 |
| 6 | `ConversationMemory` と履歴上限制御 |
| 7 | Dockerfile、Compose、Docker 起動エントリーポイント |
| 8 | GitHub Actions CI（Python 3.11、pytest） |
| 9 | GKE / EKS 向け Helm Chart、Secret、NetworkPolicy、PDB、任意 HPA |
| 10 | 実 Gemini API、実ツール実行、安全な read-only コマンド |
| 11 | Thought–Action–Observation トレース、Python スクリプト実行 |
| 12 | API 回数、retry、token、error、elapsed メトリクスと堅牢なフォールバック |
| 13 | 並列サブエージェント委譲、集約、失敗隔離、タイムアウト |
| 14 | Gemini モックからツール・メトリクス・オーケストレーターまでの E2E |
| 15 | FastAPI `/health` / `/run`、API テスト、詳細ハンドオフ文書 |
| 16 | `/chat` セッション、`/runs/{run_id}` トレース取得、`/sessions/{id}` 履歴取得、`list_workspace` ツール |

## 3. 現在のディレクトリと主要ファイル

```text
.
├── app.py                         # FastAPI REST shell
├── main.py                        # CLI / Docker entry point
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
├── .env.example
├── README.md
├── HANDOFF.md
├── docs/architecture.md           # 詳細アーキテクチャ
├── deploy/                        # Kubernetes / Helm
│   ├── README.md
│   └── helm/karry-agent/
├── src/karry_agent/
│   ├── core.py                    # AgentCore, AgentResult, AgentMetrics
│   ├── tools.py                   # ToolRegistry と built-in tools
│   ├── memory.py                  # ConversationMemory
│   ├── orchestrator.py            # MultiAgentOrchestrator
│   ├── cli.py
│   ├── logger.py
│   └── __init__.py
└── tests/                         # Phase 別 unit / integration tests
```

### 主要 API

- `AgentCore(client=None, model_id=None, registry=None, max_steps=8, max_retries=3)`
- `AgentCore.run(prompt) -> AgentResult`
- `AgentResult.text`, `.trace`, `.metrics`
- `AgentMetrics.api_calls`, `.retries`, `.input_tokens`, `.output_tokens`, `.total_tokens`, `.tool_calls`, `.errors`, `.elapsed_ms`
- `ToolRegistry.register(...)` / `.execute(...)`
- `MultiAgentOrchestrator(agent_factory, max_workers=4, timeout_seconds=60)`
- `MultiAgentOrchestrator.run(tasks) -> OrchestrationResult`
- `ConversationMemory(max_history=10)`
- FastAPI: `POST /chat`, `GET /runs/{run_id}`, `GET /sessions/{session_id}`

## 4. セットアップ

```bash
git clone https://github.com/kek46991-commits/karry-agent.git
cd karry-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
```

`.env` はローカル専用です。API キーを Git、Docker image、values.yaml、ログへ含めないでください。

```env
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
KARRY_WORKSPACE=.
```

主な環境変数:

| 変数 | 必須 | 用途 |
|---|---:|---|
| `GEMINI_API_KEY` | 実 API 時のみ | Gemini API 認証 |
| `GEMINI_MODEL` | 任意 | 既定 `gemini-3.5-flash` |
| `KARRY_WORKSPACE` | 任意 | ファイル・スクリプト操作のルート |
| `RUN_LIVE_GEMINI_TESTS` | 任意 | `1` の場合だけ実 API テストを有効化 |

## 5. 起動方法

### CLI

```bash
python -m karry_agent.cli "プロジェクトの概要を説明してください"
# または
python main.py
```

### FastAPI REST shell

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
curl http://localhost:8000/health
curl -X POST http://localhost:8000/run \
  -H 'content-type: application/json' \
  -d '{"prompt":"現在時刻を確認してください"}'
```

エンドポイント:

- `GET /health` — プロセスの簡易ヘルスチェック。API キー検証は行わない
- `POST /run` — `{ "prompt": "..." }` を受け取り、`text`、`steps`、`tool_calls`、`trace`、`metrics`、`run_id` を返す
- `POST /chat` — `session_id` を使った会話履歴付き実行
- `GET /runs/{run_id}` — 実行状態、トレース、メトリクスを取得
- `GET /sessions/{session_id}` — 会話履歴を取得
- `/docs` — FastAPI が生成する OpenAPI UI

`/run` は同期処理で Gemini 呼び出しを待つため、公開前に認証・入力上限・レート制限・タイムアウト・ジョブキューを追加してください。

### Docker / Kubernetes

```bash
export GEMINI_API_KEY="..."
docker compose build
docker compose run --rm karry-agent
```

Kubernetes は `deploy/README.md` と `deploy/helm/karry-agent` を参照してください。Secret Manager / External Secrets を推奨します。

## 6. テストと CI

API キー不要の通常テスト:

```bash
python -m pytest -q
```

実 API スモークテストはクレジットとレート制限を考慮し、明示的に有効化した場合のみ実行します:

```bash
RUN_LIVE_GEMINI_TESTS=1 python -m pytest -q
```

GitHub Actions は `main` への push / pull request で Python 3.11 と pytest を実行します。Phase 14 の E2E は Gemini モックで、Function Calling、ツール、メトリクス、マルチエージェント集約を接続検証します。Phase 15 の `tests/test_phase15.py` は FastAPI の `/health`、`/run`、入力バリデーションを検証します。

## 7. Phase 15+ ロードマップ / backlog

### P0 — Web UI / API 本番化

- [ ] OAuth2 / API key 認証、tenant isolation、監査ログ
- [ ] prompt 長・request body・同時実行数・実行時間の制限
- [ ] 非同期ジョブ API（submit / status / cancel）と Redis / Pub/Sub キュー
- [ ] SSE / WebSocket による Thought–Action–Observation のストリーミング
- [ ] 会話 ID と `ConversationMemory` の DB / Redis 永続化
- [ ] OpenTelemetry trace、メトリクス exporter、PII / secret redaction

### P1 — エージェント機能

- [ ] 実プロバイダ Web 検索の選択、キャッシュ、引用・出典管理
- [ ] 画像生成（Imagen / Nano Banana 等）の実 API adapter と asset storage
- [ ] tool permission policy、承認フロー、dry-run、破壊的操作の二段階確認
- [ ] tool schema versioning、idempotency key、circuit breaker
- [ ] サブエージェントの specialist registry、予算・token quota、優先度制御
- [ ] RunStore / SessionStore を Redis または DB へ移行し、複数 API レプリカ間で共有

### P2 — 開発・運用

- [ ] OpenAPI client / TypeScript SDK と Web UI（React 等）
- [ ] staging / production Helm values、image digest 固定、署名・SBOM
- [ ] canary rollout、HPA の実測 tuning、SLO / alerting / runbook
- [ ] 負荷試験、長時間 soak test、障害注入、API quota 監視
- [ ] Devin 用の issue template、変更時の architecture decision record

## 8. 引き渡し時の注意

1. `.env`、API キー、Secret の値をコミットしない。
2. 外部 API / コマンド / ファイル書き込みツールは最小権限で運用する。
3. `AgentResult.metrics` を監視へ送る際、プロンプト全文やツール引数の秘密情報をそのまま送らない。
4. `/run` は現時点で認証なしのプロトタイプであり、インターネットへ直接公開しない。
5. 本番モデル・利用可能リージョン・料金・レート制限はデプロイ先で再確認する。
6. 変更後は `python -m pytest -q` と `git diff --check` を実行し、CI の green を確認する。

## 9. Phase 17 完了内容

- `POST /chat` に `stream=true` を追加し、SSE の `started` / `step` / `completed` / `error` イベントを提供
- `step` イベントに Thought–Action–Observation の実行トレースを JSON で格納
- `browse_path` でワークスペース内のファイル内容・ディレクトリ一覧を閲覧
- `append_text_file` で UTF-8 テキストを安全に追記
- ワークスペース外パス、隠しファイル、巨大ファイル、過大な一覧を制限
- `tests/test_phase17.py` で SSE、セッション、ツール登録、ファイル操作、パス逸脱を統合検証

SSE は現段階では実行完了後のトレースを送る基礎実装です。Gemini の streaming API と非同期ジョブキューを導入する際に、個々のモデル生成・ツール実行をリアルタイムに中継してください。
