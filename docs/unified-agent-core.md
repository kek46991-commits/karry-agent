# 統合エージェント・コア

提供された `agent_core.py` の7関数を、既存の `ToolRegistry → AgentCore → /chat・/run・/agent/run` に接続しました。新しいGeminiクライアントや別サーバーに置き換えず、会話履歴、SSE、実行トレース、Calendarの確認フローを維持しています。

`from karry_agent.agent_core import AGENT_TOOLS, dispatch_tool` またはリポジトリ直下の `agent_core.py` を使用できます。旧 `tools.py` の初期3ツールのリストは互換性のためそのままです。本体が使用する `build_default_registry()` には既存ツールと新しい7関数のインターフェースが登録されます。

## ツールと互換性

`read_file` と `write_file` は提供コードの `filepath` と既存コードの `path` の両方に対応します。ワークスペース外へのアクセスと既存ファイルの上書きを拒否する既存方針は維持しています。`list_dir` はディレクトリ直下を最大500件返し、隠しファイルとGoogle秘密ファイル名を除外します。

`execute_command` は既定で既存のコマンドallowlistを利用します。任意の `shell=True` 実行が必要な場合のみ、`KARRY_ALLOW_SHELL=1` と `KARRY_API_TOKEN` を設定してください。任意シェルには30秒のタイムアウトとプロセスグループ停止を設定し、秘密情報の環境変数を子プロセスへ渡しません。出力は各20,000文字に制限します。ただし、**cwdや環境変数削除はOSサンドボックスではありません**。ファイル、ネットワーク、バックグラウンドプロセスへのアクセスを完全に隔離するものではなく、既存allowlistもOSサンドボックスではありません。単一所有者用の隔離コンテナー、最小権限、適切なネットワーク制限が前提です。

`add_calendar_event` は `end_time` と `description` を追加しました。既存の `duration_minutes` と `confirmed` は維持します。プレビューした終了時刻と説明も、チャットの次の明示確認へ保持します。既定はプレビューだけで、実際の予定登録には既存どおり確認が必要です。

モデルが自分で `confirmed=true` を出力しても、AgentCoreでは `false` に固定し、追加はプレビューのみです。Web APIが保存済みのプレビューに対するユーザーの次ターンの確認を処理します。`/run` と `/agent/run` でもセッションを未指定なら新規発行し、応答の `session_id` を次のリクエストに指定して確認できます。直接ツールAPIとPython関数の `confirmed=true` は、認証された所有者が指定内容の登録を明示的に要求するインターフェースです。

`KARRY_ALLOW_SHELL=0` は新しい `execute_command` の任意シェル分岐だけを無効にします。既存の `run_local_script` によるPython実行まで無効になる設定ではありません。これら既存機能も隔離環境で使ってください。

## Google OAuth

CalendarとPhotosは `google_workspace.get_google_credentials()` を共通利用します。優先順はPhotos専用 `GOOGLE_PHOTOS_TOKEN_JSON`（Photosのみ）、共通 `GOOGLE_WORKSPACE_TOKEN_JSON`、既存 `GOOGLE_CALENDAR_TOKEN_JSON`、OAuth環境変数3点、既存tokenファイルです。

OAuth環境変数3点は `GOOGLE_CLIENT_ID`、`GOOGLE_CLIENT_SECRET`、`GOOGLE_REFRESH_TOKEN` です。JSON Secretの正規化と検証には既存の `calendar_secrets.py` を使います。必要なclient情報を含むtokenがある場合、無関係なcredentials Secretの形式不正で停止しません。tokenの有効性を確認して明示的にrefreshし、refresh失敗は `OAuth_Refresh_Error` として処理します。通常のヘッドレス認証ではSecretをファイルへ復元せずメモリ内で使用します。ローカルOAuthの明示的な `GOOGLE_CALENDAR_ALLOW_LOCAL_OAUTH=1` は引き続き対応しています。

Calendarのスコープは既存の `https://www.googleapis.com/auth/calendar.events` です。Photos Pickerには `https://www.googleapis.com/auth/photospicker.mediaitems.readonly` が必要です。**Calendarだけに同意したtokenをPhotosにも利用できるわけではありません。** 共通tokenなら必要な両スコープをユーザーが承認したものを設定してください。別tokenなら `GOOGLE_PHOTOS_TOKEN_JSON` を使います。新規OAuth同意画面やPhotos用初回ログインUIはこの変更には含みません。

Google APIエラーのレスポンスには秘密値・生の例外・認証付きURLを含めず、エラー分類と取得可能なHTTPステータスを返します。

## PhotosとMapsの実API

提供コードのPhotosスコープ `photoslibrary.readonly` は廃止されています。Library APIはアプリ作成コンテンツに限定されるため、この統合ではPhotos Pickerを使用します。[1]

`search_photos()` を `session_id` なしで実行すると `status: needs_user_action`、`pickerUri`、`session_id` が返ります。ユーザーがリンク先で写真を選択後、同じsession IDを指定して再実行します。[2] 選択前はモデルが再呼び出しを繰り返さず、APIは `needs_user_action` で返ります。`query_filter` は**選択済み項目の現在ページのファイル名部分一致**です。ライブラリ全体の無人検索や画像内容検索ではありません。`nextPageToken` があれば `page_token` で続きのページを取得します。`pollingConfig` は返しますが、無期限の監視は起動しません。

`get_map_directions` はGoogle Maps Routes APIの実エンドポイントを呼びます。[3] `GOOGLE_MAPS_API_KEY` とRoutes APIの有効化が必要です。MapsはCalendar／PhotosのOAuthとは別認証です。提供コードの `maps.readonly` スコープや仮の成功メッセージは使いません。結果は自動車経路の距離、所要時間、ポリラインです。

## GeminiとAPI

Gemini SDKの自動関数実行を無効にし、既存レジストリで引数を検証して実行します。元のモデルContentを保持し、関数IDをFunctionResponseへ返すため、thought signatureを落としません。[4] ツールエラーをエラーメトリクスへ計上します。写真選択待ち後の同じ応答に含まれる追加関数は実行しません。

既存の `POST /chat`、`POST /run`、`POST /agent/run` が統合ツールを使用します。既存のステップ上限を維持し、応答 `status` で `completed`、`failed`、`limit_reached`、`needs_user_action` を区別します。`completed` はモデルの回答生成完了であり、全ツールの成功保証ではありません。`trace` と `metrics.errors` も確認してください。

認証付きの直接ツールAPIを追加しました。

```text
GET  /api/agent/tools
POST /api/agent/tools/execute
```

直接APIは `KARRY_API_TOKEN` が未設定なら503、認証不一致なら401です。`KARRY_API_TOKEN` を設定すると既存のchat/run/互換APIとセッション・実行履歴APIにもBearer認証を要求します。既存のチャット画面に追加したAPIトークン欄から送信できます。トークンをブラウザストレージや会話には保存しません。ヘルスチェックと静的画面は公開のままです。

```bash
curl "$BASE_URL/api/agent/tools/execute" \
  -H "Authorization: Bearer $KARRY_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tool_name":"list_dir","arguments":{"dirpath":"."}}'
```

直接APIの処理結果は `result` 内にあります。例えば `result.status: error` がHTTP 200で返る場合があります。共有Bearer tokenは単一所有者向けで、複数ユーザーのOAuth・権限分離・監査主体の分離ではありません。任意シェルを有効にせずtokenも未設定なら、旧APIの挙動を互換維持しますが、一般公開の運用ではtoken設定を推奨します。

## 検証とRender配置

```bash
pip install -r requirements.txt
pip install -e '.[test]'
python -m pytest -q
python -m compileall -q src app.py agent_core.py
pip check
```

新規統合テストは `tests/test_unified_agent_core.py` です。外部APIはモックし、本番Calendarに予定を作成していません。実APIの秘密値がこの作業環境にないため、Google／Geminiのライブ検証は未実施です。

既存のRender構成ではmainへの変更が自動配置されます。PRで差分を確認した後にmainへマージすると、接続されたRenderサービスに反映される構成です。本番は既存資料に記載された `https://karry-agent-1.onrender.com` ですが、この変更の配置成功を確認するまで新バージョンの公開完了とは扱いません。

`render.yaml` と `.env.example` に新しい設定項目を追加しています。既存RenderサービスのEnvironmentで必要な秘密値を設定してください。新規Secretはコードのマージだけで発行・追加されません。`KARRY_ALLOW_SHELL` は0のままが既定です。任意シェルの有効化は別の運用判断として扱い、この作業では本番権限を広げません。配置後は `/health` と、認証付き `/api/agent/tools` の新しいツール宣言を確認してください。

Calendar等の副作用を伴う処理には全体のロールバック・重複排除を追加していません。実行後の通信エラーで同じ依頼を無条件に再送しないでください。

## References

[1]: https://developers.google.com/photos/support/updates "Google Photos API updates"
[2]: https://developers.google.com/photos/picker/guides/get-started-picker "Google Photos Picker flow"
[3]: https://developers.google.com/maps/documentation/routes/compute_route_directions "Google Maps Routes API: Get a route"
[4]: https://googleapis.github.io/python-genai/ "Google Gen AI SDK function calling"
