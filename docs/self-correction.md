# 自律デバッグ・自己修正ループ（実験機能）

`src/karry_agent/self_correction.py` は、失敗したチェックを Gemini に直させて再実行するループです。

```
隔離コピーを作る → pytest を実行 → 失敗ログ（Traceback）と対象コードを Gemini へ
→ 返ってきた修正（old/new の厳密な置換）を検証してコピーへ書き込む → 再実行
→ 通るまで最大3回（KARRY_SELF_CORRECTION_MAX_ATTEMPTS、上限5）
```

## 安全のための決まり

- **動いているサーバーのファイルは書き換えません。** 毎回 `.git`・`.venv`・`.env` を除いてリポジトリを一時ディレクトリへコピーし、そこだけを書き換えます。Render ではファイルの書き換えは再デプロイで消えるため、本体へ反映する経路は PR だけです。
- 実行するコマンドは `python -m pytest -q -x <tests/ 配下のパス>` に固定です。モデルや API 利用者がコマンドを決めることはありません。
- 子プロセスには、名前が token / secret / api key などに当たる環境変数を渡しません（`GEMINI_API_KEY`・`KARRY_API_TOKEN` を含む）。
- Gemini に送るのは、マスク済みの失敗ログ（末尾8,000文字）と、allowlist（既定 `src/karry_agent/core.py`・`src/karry_agent/tools.py`・`app.py`）のうち秘密値を含まないファイルだけです。呼び出しは既存の `GeminiRepairGenerator`（`google-genai`、ツール実行なし、temperature 0）を使います。
- 修正は `validated_changes` で検査します。allowlist 外のパス、パストラバーサル、old が1か所に一致しない置換、構文エラー、秘密値を含むコードは拒否し、その回は失敗扱いにします。
- テストが通ったときだけ `corrected`（`verified: true`、`validation: tests_passed_in_sandbox`）になり、diff と SHA-256 を返します。`KARRY_SELF_HEALING_CREATE_PRS=1` なら、既存の `GitHubRepairPublisher` で `self-healing/<job_id>` のドラフトPRを作ります。マージ・デプロイは行いません。
- 1プロセスで実行できるジョブは既定3件です。ワーカーは1つで、順番に処理します。

## 使い方

```text
KARRY_EXPERIMENTAL_FEATURES=1         # API を公開（サンドボックスのみ）
KARRY_SELF_CORRECTION=1               # ループを有効化
GEMINI_API_KEY=<既存の秘密設定>
KARRY_SELF_CORRECTION_TESTS=tests     # 実行時エラーのときに回すチェック（カンマ区切り）
KARRY_SELF_HEALING_CREATE_PRS=1       # 任意: 修正をドラフトPRにする
KARRY_REPAIR_GITHUB_REPO=kek46991-commits/karry-agent
KARRY_REPAIR_GITHUB_TOKEN=<GitHub token>
KARRY_REPAIR_GITHUB_BASE=feature/sandbox-clone
```

- `POST /api/agent/self-correction` `{"tests": ["tests/test_calc.py"]}` → 202 でジョブを開始
- `GET /api/agent/self-correction/{job_id}` → `queued` / `running` / `corrected` / `not_corrected` / `passed_without_changes` / `error` と、各回の結果（`attempts`）
- `GET /api/agent/self-correction` → このプロセスのジョブ一覧

どれも所有者トークンが必要です。`KARRY_EXPERIMENTAL_FEATURES` が未設定（公開用の本番）だと 404 になり、`/docs` にも出ません。

有効にすると、実行中に起きた未知のコードエラー（`SelfHealingEngine` が `code_or_dependency` と分類したもの）も、このループに自動で渡されます。インシデントには `job_id` と結果が記録されます（`GET /api/agent/healing/incidents`）。
