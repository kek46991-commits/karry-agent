# PostgreSQLメディア管理

`schema.ts`にユーザー指定の`media_files`を追加しました。UUIDの`id`、`session_id`、`type`、`file_url`、`prompt`、`created_at`を持ち、typeは`image_input`/`image_generated`に制限しています。タスク別の時系列索引も追加しました。Python/FastAPIをTypeScriptへ置き換えず、Drizzleが作成する同じPostgreSQLテーブルへpsycopgで保存します。[3]

生成時の二重送信を防ぐため、追加の`media_generation_jobs`でrequest UUID、prediction ID、状態を記録します。既存のタスクJSON・R2画像・HTMLを移行/削除しません。

## マイグレーション

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run db:typecheck
# schema変更時のみ。生成SQLとsnapshotをレビューしてコミット
npm run db:generate
# PostgreSQLのDATABASE_URLを非公開の.env/Secretへ登録してから実行
npm run db:migrate
```

`db:migrate`はDrizzleの移行履歴を使用し、同じSQLを2回適用しません。プロセス間のadvisory lock、ロック/実行時間制限、秘密値を表示しないエラー処理を追加しました。既存の同名テーブルがDrizzle未管理で存在する場合、勝手に削除・置換せず接続先と既存スキーマを確認してください。アプリ起動時に暗黙のDDLや本番移行は実行しません。DDL権限を持つ管理用接続で適用し、運用接続は必要なテーブルの読取・追加・更新権限に限定できます。

本番の接続先は未指定です。現在接続できるRenderワークスペースにはPostgreSQLがなく、作業環境にも本番DATABASE_URLはありません。**今回の実適用は隔離されたSandboxの`karry_media_test`のみで、本番適用ではありません。** 認証付き本番接続が提供されれば同じMigrationを適用します。

## API

新しい`/api/media/*`は所有者Bearer認証を要求し、既存タスクを確認してから処理します。

`POST /api/media/upload`はmultipartの`session_id`と`file`を受け取ります。JPG/PNG/WEBP/GIFの実形式と容量を検証し、EXIF等を除去。GIFは先頭フレームの静止PNGです。正規化画像のdata URLをmedia_filesへINSERTして、status、id、session_id、type、file_url、filenameを返します。同じタスクの同じ正規化画像の再送は同じレコードです。

`GET /api/media?session_id=...`はタスク別のメタ情報一覧、`GET /api/media/{media_id}?session_id=...`は非公開の画像レコードを返します。一覧へ全Base64を載せず、取得APIは非公開/no-storeです。画像はRender静的公開領域へ保存しません。1画像は入力4 MiB、正規化2 MiB、タスク100件が上限です。

これはDBのメディアライブラリーAPIです。既存チャットUIの画像添付は従来の`/api/upload_image`→R2→Gemini経路を維持し、新しいライブラリー画像を自動的に次回のGemini添付へ流したり、古いUIへDB依存を追加したりしません。画像生成ボタンのUI追加はこの変更には含みません。

`POST /api/media/generate`は`session_id`、`prompt`、任意の`request_id` UUIDを受け取ります。クライアントは送信前にUUIDを発行し、通信再送でも同じ値を使ってください。旧形式（session_id/promptのみ）でも受付UUIDを返しますが、受付応答が失われた場合に新しいrequest_idで再生成しないでください。

公式モデル`black-forest-labs/flux-schnell`の`/v1/models/.../predictions`へ1件送信します。モデル名をversionハッシュとして渡さないよう修正しました。受付前にDBへ生成ジョブを確定し、未完了は202のprocessing/submitting、受付が不明ならsubmission_unknownです。同じrequest_idは再課金POSTを行いません。明確な400/401/403/404/422/429拒否はfailedとして枠を解除し、キー・入力を修正後に別request_idで利用できます。受付不明のジョブは運用者がReplicate側を確認するまで自動再送しません。タスクあたり進行/不明ジョブは1件で、アップロードより先に生成完了用の保存枠を予約します。[1]

`GET /api/media/jobs/{job_id}?session_id=...`で同じジョブを確認できます。processing時だけ1回状態を読取り、succeededになってから出力を検証・取得・DBコピーしてmedia_filesとジョブ完了を同一トランザクションで確定します。DB/ダウンロードが一時失敗しても同じprediction IDを使い、新規生成しません。Replicateの生ログ・エラー本文・署名URLはAPIへ露出しません。

ReplicateのAPI出力は既定で約1時間後に削除されるため、URL文字列だけを保存せず正規化画像のdata URLを非公開DBへコピーします。1時間を超えて取得できなくなった未完了ジョブは、新規課金を勝手に実行せず運用者が確認してください。[2]

## 秘密設定

`DATABASE_URL`：対象PostgreSQLの接続文字列。ローカルまたはRenderの秘密設定に登録し、Git/チャット/フロントエンドへ出さないでください。

`REPLICATE_API_TOKEN`：生成APIのサーバー秘密設定。アップロード・一覧・保存済み画像を読むだけなら不要です。

`KARRY_MEDIA_GENERATION_ENABLED=1`：管理者が生成利用を有効化するときだけ設定。既定は無効です。この作業中にReplicateへ課金生成を送信せず、本番の設定・DB作成・既存データ変更も行っていません。

## 検証

ローカルの隔離PostgreSQLでDrizzle Migrationを2回実行し、SQLのtype制約、画像INSERT/一覧/再読込、同じ画像の再送、生成受付→完了保存、未知受付、二重送信防止、失敗時の再確認を結合テストで検証しました。モデル応答はモックです。

Python **535 passed, 2 skipped**、既存DOM **25 passed**。DrizzleのTypeScript型検証も成功しています。CIにも隔離PostgreSQLサービスと2回のMigrationを追加しました。実DB接続がない通常のテストではDB結合11件のみ明示スキップし、SQLiteやインメモリをPostgreSQLと偽って代用しません。

## References

[1]: https://replicate.com/docs/reference/http "Replicate HTTP API official model prediction creation"
[2]: https://replicate.com/docs/topics/predictions/data-retention "Replicate API prediction data retention"
[3]: https://orm.drizzle.team/docs/get-started/postgresql-new "Drizzle PostgreSQL schema and migration setup"
