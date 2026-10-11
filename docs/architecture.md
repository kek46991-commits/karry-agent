# karry-agent アーキテクチャ / Devin Handoff

## システム構成

```text
User / CLI
   |
   v
AgentCore (Gemini API client)
   |  Thought -> Action -> Observation
   +--> ToolRegistry
   |      +--> read_file / write_file
   |      +--> web_search
   |      +--> run_command / run_local_script
   |      +--> datetime / image mock
   |
   +--> AgentResult
          +--> trace (tool action / observation / duration)
          +--> metrics (API calls / retries / tokens / errors / elapsed)

MultiAgentOrchestrator
   +--> DelegatedTask[]
   +--> specialist sub-agents (parallel)
   +--> OrchestrationResult (ordered aggregation + failure isolation)
```

## モジュール API

### `karry_agent.core`

- `get_client()` — `GEMINI_API_KEY` から `google-genai` client を生成
- `AgentCore.run(prompt)` — Gemini Function Calling の ReAct ループを実行
- `AgentResult.text` — 最終回答
- `AgentResult.trace` — `ExecutionEvent` の一覧
- `AgentResult.metrics` — `AgentMetrics`（API 呼び出し数、再試行数、トークン数、エラー数、経過時間）

### `karry_agent.tools`

- `ToolRegistry.register(name, description, parameters, handler)` — ツール登録
- `ToolRegistry.execute(name, args)` — 例外を構造化エラーへ変換して実行
- ファイル操作は `KARRY_WORKSPACE` 配下に限定
- コマンド／スクリプトは `shell=False`、allowlist、タイムアウト、出力上限を適用

### `karry_agent.orchestrator`

- `DelegatedTask(task_id, prompt, specialist)` — 委譲タスク
- `MultiAgentOrchestrator(agent_factory, max_workers, timeout_seconds)` — 並列実行の管理
- `OrchestrationResult.results` — 入力順の `SubAgentResult` 一覧
- サブエージェントの失敗は他タスクから隔離され、集約結果に明示される

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env の GEMINI_API_KEY を設定
python -m pytest -q
```

実 API を使う場合:

```bash
GEMINI_API_KEY="..." GEMINI_MODEL=gemini-3.5-flash \
  python -m karry_agent.cli "依頼内容"
```

API キーは `.env`、Kubernetes Secret、または Secret Manager に保存し、Git にコミットしないでください。

## テスト戦略

- 通常の CI: `python -m pytest -q`。API キー不要のモック／統合テストを実行
- 実 API スモーク: `RUN_LIVE_GEMINI_TESTS=1` と `GEMINI_API_KEY` を明示した場合のみ実行
- Phase 14 E2E: Gemini 応答モックからツール実行、メトリクス、オーケストレーター集約までを検証

## Devin への引き渡しチェックリスト

- [ ] `main` の最新コミットを取得
- [ ] `.env` は秘匿情報から作成し、リポジトリへ追加しない
- [ ] `python -m pytest -q` が成功することを確認
- [ ] 実 API のモデル名・利用可能リージョン・レート制限を対象環境で確認
- [ ] `AgentCore.max_steps` / `max_retries` / `MultiAgentOrchestrator.timeout_seconds` を運用要件に合わせる
- [ ] `AgentResult.metrics` をログ基盤へ送る（API キーやプロンプト全文は送らない）
- [ ] Kubernetes では `deploy/README.md` の Secret、NetworkPolicy、リソース設定を確認
- [ ] 本番で書き込み・外部送信・破壊的コマンドを有効化する前に承認フローを追加
