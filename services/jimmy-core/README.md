# Jimmy Core（ジミーコア）

記憶・人格・思考・他エージェントへの指示出し **だけ** を担当する中枢 API です。
動画生成・ECショップ・端末操作のコードは一切 import せず、それらは別 URL の「現場エージェント」として
`POST /dispatch` で呼び出します（分離マップは [`../README.md`](../README.md)）。

依存は FastAPI / httpx / psycopg のみで、ffmpeg・OpenCV・Google SDK などの重い依存はありません。

## エンドポイント

| メソッド | パス | 役割 |
| --- | --- | --- |
| GET | `/health` | 稼働確認（認証なし） |
| POST | `/memory` | 会話・決定事項・タスク・メモを保存（`kind`: conversation / decision / task / note / event） |
| GET | `/memory?kind=&limit=` | 記憶の取得 |
| GET | `/state` | 現在のタスク、直近の決定事項・会話・思考、実行中ジョブ、キルスイッチ状態 |
| POST | `/think` | 状況分析 → リスク監査 → 次のアクション決定（proceed / dispatch / confirm_with_user / halt） |
| POST | `/dispatch` | 現場エージェントへ非同期で指示（202 と `job_id` を返す） |
| GET | `/dispatch/{job_id}` | ジョブ状態 |
| POST | `/webhook/callback` | 現場エージェントからの進捗・完了通知 |
| POST | `/killswitch` | 全ディスパッチ停止（killswitch エージェントのみ許可） |
| GET | `/agents` | 登録済みエージェント（トークンは返さない） |

`/health` と `/` 以外は `Authorization: Bearer $JIMMY_CORE_API_TOKEN` が必須です。トークン未設定時は 503 を返します（fail-closed）。

### 例

```bash
curl -s -X POST "$CORE/think" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"situation":"本番のデータベースを削除して作り直す"}'
# => {"risk":{"level":"high",...},"decision":{"action":"confirm_with_user",...},"engine":"rules","elapsed_ms":0.3}
```

`/think` はルールベースで数ミリ秒で終わります。`GEMINI_API_KEY` を設定した場合だけ Gemini で要約と次の手順を補強します（タイムアウト `JIMMY_CORE_LLM_TIMEOUT` 秒、失敗時はルール結果を返す）。LLM はリスクを上げることはできますが、下げることはできません。

## 現場エージェントとの接続

環境変数でエージェントを登録します。`<NAME>` は任意（例: `DEVICE`, `VIDEO`, `SHOP`, `KILLSWITCH`）。

| 変数 | 内容 |
| --- | --- |
| `JIMMY_AGENT_<NAME>_URL` | エージェントのジョブ受付 URL（https のみ） |
| `JIMMY_AGENT_<NAME>_TOKEN` | エージェントに送る Bearer トークン（任意） |

コアはエージェントへ次の JSON を POST します。

```json
{"job_id": "job_000001", "action": "edit", "payload": {}, "callback_url": "https://<core>/webhook/callback", "callback_token": "<job ごとの HMAC>"}
```

エージェントは完了時に `callback_url` へ `X-Jimmy-Callback-Token: <callback_token>` ヘッダー付きで
`{"job_id": "...", "status": "progress|completed|failed|cancelled", "result": {...}}` を POST します。
受付レスポンスで `{"status": "completed", "result": ...}` を返せば、その場で完了扱いにもできます。

## その他の環境変数

| 変数 | 既定値 | 内容 |
| --- | --- | --- |
| `JIMMY_CORE_API_TOKEN` | なし（必須） | API トークン |
| `JIMMY_CORE_DATABASE_URL` | なし | PostgreSQL に保存（`jimmy_core_documents` テーブルを自動作成）。未設定ならファイル保存 |
| `JIMMY_CORE_DATA_DIR` | `data` | ファイル保存先 |
| `JIMMY_CORE_CALLBACK_SECRET` | API トークン | コールバック署名用の秘密鍵 |
| `JIMMY_CORE_PUBLIC_URL` | `RENDER_EXTERNAL_URL` | `callback_url` の基準 URL |
| `JIMMY_CORE_DISPATCH_TIMEOUT` | `15` | エージェント呼び出しのタイムアウト（秒） |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | なし / `gemini-3.5-flash` | `/think` の LLM 補強（任意） |

## ローカル実行とテスト

```bash
cd services/jimmy-core
pip install -r requirements-dev.txt
pytest -q
JIMMY_CORE_API_TOKEN=dev uvicorn jimmy_core.app:app --reload
```

## Render

Render の Web Service（Python）として、Root Directory を `services/jimmy-core` にしてデプロイします。

- Build: `pip install -r requirements.txt`
- Start: `uvicorn jimmy_core.app:app --host 0.0.0.0 --port $PORT`
- Health check: `/health`

Root Directory の外側（karry-agent 本体）が変わっても、ジミーコアは再デプロイされません。
