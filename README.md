# karry-agent / kelly-agent — Phase 1〜3

Gemini API (`google-genai`) と Function Calling を使った自律型 AI エージェントのコアエンジンです。
ReAct 型のツール実行、対話型 CLI、Markdown 進行ログ、API 障害時の再試行を備えています。

## クイックスタート

### 1. 環境構築

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e '.[test]'
cp .env.example .env
# .env の GEMINI_API_KEY に自身の API キーを設定
```

設定できる環境変数:

| 変数 | 内容 | 既定値 |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API キー（必須） | なし |
| `GEMINI_MODEL` | 使用モデル | `gemini-2.5-flash` |
| `KARRY_WORKSPACE` | ファイルツールのルート | CLI 実行ディレクトリ |
| `KARRY_PROGRESS_FILE` | 進行ログの保存先 | `progress.md` |

`.env` は Git にコミットしないでください。

### 2. CLI の使い方

単発実行:

```bash
python3 cli.py 'README.md を読み、要点を説明してください'
# または
python3 -m karry_agent.cli 'Gemini API の概要を検索してください'
```

対話モード:

```bash
python3 cli.py
# User > に入力。exit または quit で終了
```

実行履歴は `progress.md` に CLI 開始・終了・処理結果・エラーとして追記されます。

### 3. テスト

```bash
python3 -m pytest -q
# unittest 形式のテストも実行可能
python3 -m unittest discover tests
```

## Phase 3 の耐久性

Gemini の `APIError` と `TimeoutError` / socket timeout は、既定 3 回まで指数バックオフ（1 秒、2 秒）で再試行します。最終的に失敗した場合は `[Error]` で始まる結果を返し、CLI プロセスを異常終了させません。認証・設定などの予期しない例外は再試行せず、原因を返します。

`AgentCore` では `max_retries` と `sleep_fn` を指定できるため、運用時の調整とテスト時の待機時間短縮が可能です。

## Function Calling ツール

- `read_file`: `KARRY_WORKSPACE` 配下だけを読み取り（最大 100,000 文字）
- `write_file`: 新規ファイルのみ作成。既存ファイルの上書きは拒否
- `web_search`: DuckDuckGo Instant Answer API を利用した検索

外部検索は検索語を DuckDuckGo に送信します。画像生成（Imagen / Nano Banana）は別フェーズの範囲です。
