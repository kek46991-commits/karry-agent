# タスク接続エラーの対応

## 確認した状態

2026-10-14、本番 `https://karry-agent-1.onrender.com` のBearer認証は正常です。ヘルスとツール一覧は200、認証なしのタスク一覧は401。認証付きタスク一覧は503 (`storage_unavailable`) です。HTMLストレージ状態は `configuration_valid=true` ですが `connection_status=not_checked` で、設定形式が正しいことと実接続成功は別です。

Cloudflareアカウントでは非公開保存用R2バケット `karryi` が存在します。R2の管理APIへ接続できることは、Renderへ設定されたS3署名キーがそのバケットを読めることを保証しません。Renderの環境変数の実値は今回まだ閲覧していません。

## 修正

スマホでチャット・モニターの下に埋もれていたAPIトークン欄と接続ボタンを画面上部へ移しました。未入力ならAPIへリクエストせず入力を案内します。`Bearer `を含めて貼り付けた場合の二重付与も補正します。トークンはHTMLへ埋め込まず、端末へ保存しません。未接続は灰色、接続失敗は赤、オンラインだけ緑です。

401はトークン確認、ストレージ503はサーバー側の保存先・鍵・権限を確認する案内として、画面上部へ表示します。接続失敗時はタスク作成・チャット・モデル実行を開始しません。`memory`への無断フォールバック、API認証無効化、保存済みデータの削除は行いません。

所有者認証付き `GET /api/agent/tasks/storage` を追加しました。タスク一覧を実際に読む読み取り専用診断で、成功なら `connection_status=connected`、失敗なら503と安全な `diagnostic_code` を返します。バケット名、endpointホスト、region、task prefixは所有者だけに返し、アクセスキー・シークレット・署名URL・生のプロバイダー例外は返しません。

| 診断コード | 管理者が確認する項目 |
| --- | --- |
| storage_bucket_missing | バケット名と対象アカウント |
| storage_permission | そのバケットへのObject Read & Write権限 |
| storage_credentials | R2/S3用のアクセスキーとシークレット、有効期限 |
| storage_signature | キーの組・endpoint・region |
| storage_endpoint | S3 endpointとregion |
| storage_network | endpoint、ネットワーク、保存先の稼働 |
| storage_unknown | 保存先・鍵・権限の追加確認 |

タスクJSONの既存 `storage_unavailable` コードを変更せず、安全な診断コードと固定のヒントを付加します。保存の競合・完了リクエストの再送照合・履歴復元は従来どおりです。

## 検証

Python **468 passed, 2 skipped**、DOM **16 passed**。以前のmainの456件・UI12件を維持し、認証・実接続・エラー分類・秘密値非開示・スマホ導線・未入力・Bearer形式・503の表示を追加検証しました。モデルのライブ実行や成果物公開は行っていません。

UI修正と診断を本番へ反映した後、実接続の診断結果に基づき保存先の設定を補正し、タスク一覧のHTTP 200を確認する必要があります。新しい秘密鍵・権限設定や管理画面ログインが必要な場合、ユーザーに安全な方法で入力・認可を依頼します。
