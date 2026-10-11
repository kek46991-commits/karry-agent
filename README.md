# karry-agent / kelly-agent — Phase 1・2

Gemini API (`google-genai`) と Function Calling を使った自律型エージェントの基盤です。

## セットアップ

```bash
cd /home/ubuntu/karry-agent
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -e '.[test]'
cp .env.example .env
# .env の GEMINI_API_KEY を設定
```

## CLI 実行

単発実行:

```bash
python -m karry_agent.cli 'README.md を読み、要点を説明してください'
```

対話モード:

```bash
python -m karry_agent.cli
# User > に入力。exit または quit で終了
```

実行履歴は `progress.md` に Markdown 形式で追記されます。保存先は `KARRY_PROGRESS_FILE` で変更できます。

## 実装内容

- `AgentCore`: Gemini の応答 → Function Call → ツール結果 → 次の応答を繰り返す ReAct ループ
- `read_file`: `KARRY_WORKSPACE` 配下だけを読み取り（最大 100,000 文字）
- `write_file`: 新規ファイルのみ作成。既存ファイルの上書きは拒否
- `web_search`: DuckDuckGo Instant Answer API を利用した検索
- `logger.py`: `progress.md` に CLI 開始・終了・タスク・エラーを追記
- `cli.py`: 単発実行および対話型 CLI
- `max_steps`: 無限ループ防止（既定 8）

## 注意

`GEMINI_API_KEY` はコードへ埋め込まず、環境変数または `.env` から読み込みます。`.env` は Git にコミットしないでください。

外部検索は検索語を DuckDuckGo に送信します。ファイル操作・外部送信・削除などを増やす場合は、承認フローと監査ログを追加してください。画像生成（Imagen / Nano Banana）は別フェーズの範囲です。
