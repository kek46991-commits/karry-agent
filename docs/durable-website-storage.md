# HTML公開の永続化：R2/S3互換ストレージ＋既存URL配信

**Cloudflare R2の非公開バケットへ保存し、既存FastAPIから読み取り配信する方式**を採用しました。Renderのローカルディスクは永続モードでは一切使用しません。公開URLは `/static/instant/<名前>_<ID>.html` を維持し、ストレージ鍵や期限付き署名URLをブラウザへ渡しません。

R2の保存内容はRenderの再起動・再デプロイ・インスタンス切替で削除されません。AWS S3にも同じアダプターで対応します。ただし「永久に必ずアクセス可能」という保証ではありません。Render／ストレージ停止中は配信できず、管理者のオブジェクト削除、保持ルール、アカウント停止、ドメイン変更には依存します。[1] [2]

## 最小構成を選んだ理由

Vercel連携では、成果物ごとのデプロイ作成に加え、READY／ERRORの状態管理とプロジェクト設定が必要になります。フルWebアプリのビルドには適していますが、現在の単一HTML即時公開の永続化には変更箇所が増えます。[3]

R2の直接公開＋別配信Worker／カスタムドメインはRenderから配信を独立できますが、現在の確認フロー・CSP隔離を維持する配信設定を別に用意する必要があります。今回は**ストレージだけを切り替え、既存のAPI・URL・隔離ヘッダーを再利用**することで、開発速度と安定性を優先しました。バケットを公開する必要も、成果物ごとのGit pushやデプロイ待ちもありません。

構成は「HTML送信 → 内容確認 → R2/S3へ条件付きPUT → 保存後HEAD検証 → 同じ公開URLを返却」です。URLへアクセスすると、FastAPIが非公開オブジェクトを読み、長さ・SHA256・UTF-8を検証して既存CSPとともに返します。512 KiB以内のHTMLだけを扱うため、複雑なビルド／multipart upload／キューは追加していません。[1] [2]

## 実装

`website_storage.py` にboto3ベースのS3互換アダプターを追加しました。UUIDのファイル名と固定prefixを検証し、`IfNoneMatch="*"` のPUTで上書きを防止。Content-MD5とSHA256メタデータを付け、HEADの容量・型・SHA256を検証した後だけ `success=true`、`persistence="durable"` を返します。読取は最大512 KiB＋1バイトに制限し、サイズ・型・ハッシュ・UTF-8を検証、応答ストリームを必ず閉じます。署名v4、接続3秒・読取10秒、SDKの最大2試行を明示設定しました。[2]

`instant_website.py` は同じ関数名と引数を維持し、保存先を切り替えます。**既定は `KARRY_WEBSITE_STORAGE=s3`**。設定不足・ストレージ障害・検証失敗時はエラーを返し、一時ディスクへ黙って戻りません。ローカル開発専用には明示的な `local` モードを残しました。公開確認には保存先識別子も結び付け、確認後のバケット／prefix／endpoint変更を拒否します。鍵のローテーションは保存先変更ではありません。

`app.py` は正規URL・複数スラッシュ・静的別名経路も同じ読取処理へ統一しました。未管理ファイル・パス改変を拒否し、ストレージがない場合に古いローカルHTMLを配信しません。既存Bearer認証、HTML全文の確認、CSP sandbox（allow-same-originなし）、no-store、nosniff等を維持します。API・配信の実装は同期関数を使い、静的mountの非同期経路ではスレッドへ逃がしてイベントループをブロックしません。

`website_migration.py` は既存の一時HTMLを同じファイル名・URLで永続化します。**既定dry-run、元ファイル削除なし、既存キー上書きなし**。同内容がすでに保存されていれば再アップロードせず、別内容ならエラー。移行後も読取内容を照合します。

公開済みオブジェクトとは別に、**公開確認待ちは従来どおり10分のプロセスメモリ**です。再起動で確認待ちが消えても公開済みHTMLは残ります。複数の公開APIプロセスへスケールする場合、確認待ちの共有は別途必要です。容量上限は現状の128件・合計32 MiBを維持し、SDKの一覧から確認します。複数プロセスの同時PUTでは合計容量の厳密なトランザクション制御ではありませんが、同じキーの上書きは条件付きPUTで拒否します。

## 管理者が一度だけ設定する項目

2026-10-14の確認ではCloudflareアカウントに接続できましたが、R2一覧APIが「R2をDashboardから有効化してください」（10042）を返しました。**R2の有効化、保存先バケット、S3アクセスキーは未設定です。** このPRは本番ストレージを作成したり秘密値を登録したりしません。

1. Cloudflare DashboardのR2を有効化し、成果物専用の**非公開**バケットを用意します。公開アクセス、r2.dev、バケット公開ドメインは有効にする必要がありません。
2. そのバケットに限定したObject Read & WriteのS3アクセスキーを作成します。保存・検証・読取・容量確認のため、対象バケットのPutObject／GetObject・HeadObject／ListBucket相当の操作が必要です。削除操作は実装にありません。Cloudflare管理APIへの接続と、Renderアプリ用のS3鍵は別です。[1] [4]
3. Renderのkarry-agent-1に以下の環境変数を登録します。秘密値はRenderの秘密設定またはGit除外済みの非公開.envへ入れ、PR・チャット・HTMLへ貼りません。バケットや鍵が未設定のままこのPRを本番へ入れると、既定s3の公開／読取は設定エラーになります。

```dotenv
KARRY_WEBSITE_STORAGE=s3
KARRY_WEBSITE_S3_BUCKET=<専用の非公開バケット名>
KARRY_WEBSITE_S3_PREFIX=karry-websites/
KARRY_WEBSITE_S3_ENDPOINT=https://<ACCOUNT_ID>.r2.cloudflarestorage.com
KARRY_WEBSITE_S3_REGION=auto
KARRY_WEBSITE_S3_ACCESS_KEY_ID=<S3 Access Key ID>
KARRY_WEBSITE_S3_SECRET_ACCESS_KEY=<S3 Secret Access Key>
```

`RENDER_EXTERNAL_URL` は既存のRender公開オリジンを使います。通常利用者は設定をせず、従来のHTML送信・確認だけで公開できます。AWS S3を使う場合はendpointを空欄、regionを実際のAWSリージョンにし、同じ設定名またはAWS_ACCESS_KEY_ID／AWS_SECRET_ACCESS_KEYを使います。一時資格情報にはSESSION_TOKENも設定できます。

4. 所有者Bearer認証で `GET /api/agent/websites/storage?verify=true` を呼び、`configuration_valid=true` と `connection_status="verified"` を確認します。これは一覧読取だけで、オブジェクトを作成しません。verifyなしは設定形式の確認だけであり、接続成功とは区別します。

本番切替・R2の有効化時に外部サービスの契約／支払い入力が必要なら、アカウント所有者がDashboardで実施してください。コードはR2の利用契約・保持設定を自動変更しません。

## 既存ページを失わず切り替える順序

**新しいコードをRenderへデプロイする前に、旧コンテナにあるHTMLを必ず退避してください。** デプロイ後に旧ディスクを読もうとしても、その時点で消えている可能性があります。

現在のサンプルを含め、公開URLから元のファイル名を保って保存するか、旧インスタンスの `static/instant/` を管理者の手元へコピーします。公開URLにアクセス可能なのは誰でも閲覧できる公開済みHTMLだけです。任意の私的ファイルは移行対象にしません。

このPRのコードを管理者環境へ取得し、退避先ディレクトリを指定してdry-runを確認します。dry-runではS3鍵がなくてもファイル・ハッシュ・予定URLを確認でき、外部書込みを行いません。

```bash
python -m karry_agent.website_migration --source-dir /path/to/exported-html
```

確認したHTMLだけを、管理者が設定済みのS3鍵を使って明示的にアップロードします。**これは公開対象の移行操作なので、内容確認後だけ実行してください。**

```bash
python -m karry_agent.website_migration --source-dir /path/to/exported-html --apply
```

全対象が `migrated` または `already_stored` になり、元ファイルを手元にも残した状態で、新しい本番コードへ切り替えます。元のファイル名を維持するため、同じRenderドメインではURLが変わりません。別内容の既存キー・容量・秘密値・接続エラーがあれば停止理由を確認し、元データを消しません。公開済みオブジェクトには短期TTLや削除処理を設定していません。管理者がストレージ側へ削除ルールを追加すると保持期間はそのルールに従います。

最後に元URLのHTTP 200とHTML内容を確認し、運用上許可された再起動／再デプロイを行った後に同じURLを確認します。**今回その本番再起動テストは実施していません。**

## 検証結果と未完了事項

実装は [PR #9](https://github.com/kek46991-commits/karry-agent/pull/9) からmainへマージ済みです（2260667）。タスクUIと履歴の永続化は、この基盤へ統合して追加します。[タスク永続化ガイド](task-session-chat.md)を参照してください。

**389テスト合格・2件スキップ**（元348件＋追加41件）。モックでローカルディスク・確認メモリを初期化した後もオブジェクトを読むこと、条件付きPUT・HEAD・SHA256・SDKパラメーター、保存／読取失敗、サイズ上限、認証、隔離ヘッダー、旧ローカルへの非フォールバック、非破壊移行を確認しました。Python／JavaScript構文、依存整合性、差分チェック、wheelビルド成功。

2026-10-14の最新確認では、本番状態APIはs3モードですが `configuration_valid=false`、`error_code=storage_configuration` でした。バケット・S3鍵設定を完了するまで、コードのマージを本番永続保存成功とは説明しません。今回、ライブ保存・本番データ移行・実再起動後の読取は未実施です。旧来の一時保存サンプルは移行済みとは確認していません。

## References

[1]: https://developers.cloudflare.com/r2/examples/aws/boto3/ "Cloudflare R2: boto3 connection examples"
[2]: https://developers.cloudflare.com/r2/api/s3/api/ "Cloudflare R2: S3 API compatibility and conditional operations"
[3]: https://vercel.com/docs/rest-api/reference/endpoints/deployments/create-a-new-deployment "Vercel REST API: create deployment and lifecycle states"
[4]: https://developers.cloudflare.com/r2/api/tokens/ "Cloudflare R2: scoped S3 access keys"
