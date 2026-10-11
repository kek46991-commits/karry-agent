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
