# Python 3.11 の軽量イメージをベースにする
FROM python:3.11-slim

# 作業ディレクトリの設定
WORKDIR /app

# 依存関係ファイルをコピーしてインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# プロジェクトのソースコードをすべてコピー
COPY . .

# src レイアウトのパッケージを解決できるようにする
ENV PYTHONPATH=/app/src

# コンテナ起動時のデフォルトコマンド
CMD ["python3", "main.py"]
