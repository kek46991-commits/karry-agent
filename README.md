# karry-agent

Gemini APIを活用した自律型AIエージェントのコアエンジンです。
Function Calling (ツール連携) 機能に対応しており、外部ツールの実行や最新情報の取得が可能です。

## 🚀 機能 (Features)

- Gemini API (`gemini-2.5-flash`) 統合
- 堅牢なエラーハンドリング & 指数バックオフ再試行
- `progress.md` へのタスク進捗ログ記録
- 対話型 CLI インターフェース (`cli.py`)
- **Function Calling ツール連携 (`tools.py`)**

## 🚀 クイックスタート

### 1. 環境構築

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env 内の GEMINI_API_KEY に自身のAPIキーを入力
python3 cli.py
python3 -m unittest discover tests
```
