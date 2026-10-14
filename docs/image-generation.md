# チャットでの画像生成

## 概要

チャットで「〇〇の画像を作って」と依頼すると、エージェントが `generate_image` ツールを呼び出し、Gemini の画像生成モデルで画像を 1 枚作ります。生成画像はタスクの非公開画像ストア（`KARRY_TASK_STORAGE=s3` のときは R2/S3、`memory` のときはプロセス内メモリ）に保存され、回答メッセージの `images` に画像 ID として記録されます。Web UI は `/api/images/{image_id}?session_id=...` から data URL を取得し、回答の下にそのまま表示します。

## 404「成果物が見つかりません」が出ていた原因

- 以前の `generate_image_mock` は実際の画像を作らず、存在しない `mock_image_path.png` という文字列を返していました。
- モデルがこの名前や推測した `/static/instant/...` の URL を案内すると、開いた時に成果物 404 になります。`/static/instant/` は公開済み HTML 成果物専用で、画像は配信しません。

## 修正内容

- 既定のツール一覧から `generate_image_mock` を外し、実際に画像を作る `generate_image` を登録しました（`src/karry_agent/image_generation.py`）。
- ツールの結果には URL やファイルパスを含めません。システム指示でも、画像の URL やファイル名を書かないように指定しています。
- 生成画像は JPEG に変換し、添付画像と同じ検証（`image_attachments.normalize`）を通してから保存します。次回の Gemini 入力（pending）には加えません。
- 1 回の依頼で 4 枚まで生成できます。タスク全体の画像数の上限は、添付画像と同じく 100 枚です。
- 利用上限（429）・モデル未対応・安全フィルターで画像が返らない場合は、エラーの理由をチャットに返します。存在しない画像を表示することはありません。
- 旧 `generate_image_mock` は互換のために残していますが、ダミーのファイル名は返しません。

## 設定

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GEMINI_API_KEY` | なし | チャットと同じキーを使います |
| `GEMINI_IMAGE_MODEL` | `gemini-2.5-flash-image` | 画像生成に使うモデル |

Gemini の画像生成モデルは、無料枠では利用できない場合があります。`利用上限` や `モデルを利用できません` と返る場合は、Google AI Studio で API キーの利用枠と課金設定を確認してください。

## テスト

- `tests/test_image_generation.py`: チャット実行で画像が保存され、回答の `images` と `/api/images` から取得できること（memory / S3）、429 エラー、画像なしの応答、生成枚数の上限
- `tests/task_chat_ui.test.cjs`: 回答の生成画像が自動で表示されること、取得できない場合に案内を表示すること
