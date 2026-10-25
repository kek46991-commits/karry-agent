# 動画脚本の自動分解（フェーズ1）

動画スイートに小説・ワンシーン等の原文から、読み上げ用ナレーションと映像シーン案を作る機能を追加します。AIが指定数のシーンを日本語で返し、生成結果は原文、全体の映像イメージ、ナレーション、各シーンの説明とともに履歴へ保存されます。画面の「動画設定へ反映」で全体の映像イメージとナレーションを既存の動画入力欄へ転記できます。

## DBと起動時マイグレーション

新しいSQLAlchemyモデル `GeneratedScript` は `generated_scripts` テーブルを使います。`id`（自動採番）、`title`、`raw_input`、`narration_script`、`visual_prompt`、`scenes`（JSON）、`created_at` を保存します。新しいテーブルのみの追加であり、既存テーブルの列やデータは変更しません。

動画サービスの起動時に `Database.initialize()` が `Base.metadata.create_all()` を実行するため、設定済みのSQLiteまたはPostgreSQLにテーブルがない場合は冪等に作成されます。`VIDEO_DATABASE_URL`（未指定なら`DATABASE_URL`、それもなければSQLite）が対象です。既存のテーブル定義を変更するマイグレーションにはAlembic等の導入が別途必要です。

## API

すべて既存の `KARRY_API_TOKEN` Bearer認証が必要です。

- `POST /api/video/scripts/generate` — `{ "raw_input": "...", "scene_count": 4 }` からAI生成・DB保存し、保存済みレコードを返します。原文は1〜12,000文字、シーン数は1〜8です。
- `GET /api/video/scripts?limit=20&offset=0` — 保存履歴を新しい順に返します。
- `GET /api/video/scripts/{id}` — 保存済みレコードを1件取得します。

脚本生成には `GEMINI_API_KEY` が必要です。モデルは `KARRY_SCRIPT_MODEL`、次に `GEMINI_MODEL`、未設定なら `gemini-3.5-flash` を使います。キー未設定時は503を返し、履歴の閲覧は引き続き利用できます。原文をサーバーログへ出力しません。

## 動作確認例

```bash
curl -sS -X POST http://127.0.0.1:8000/api/video/scripts/generate \
  -H "Authorization: Bearer $KARRY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"raw_input":"雨の夜、少女は古い駅のホームで青い傘を見つけた。遠くから汽笛が聞こえ、彼女は一歩踏み出した。","scene_count":3}'

curl -sS 'http://127.0.0.1:8000/api/video/scripts?limit=20&offset=0' \
  -H "Authorization: Bearer $KARRY_API_TOKEN"
```
