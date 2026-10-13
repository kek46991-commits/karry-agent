# HTML成果物のインスタント公開

`deploy_instant_website(html_content, filename=None)` はHTMLを保存し、`/static/instant/<名前>_<ランダムID>.html` のURLを返します。成果物のGit pushやGit CLIは不要です。**現在のコードの既定はR2/AWS S3互換の永続保存**で、管理者が一度バケット・S3鍵を設定します。通常の利用者は従来のHTML送信と確認だけで公開できます。

方式比較、管理者設定、旧コンテナのHTMLを失わない切替手順は [durable-website-storage.md](durable-website-storage.md) を参照してください。永続化PRの本番反映・移行を完了するまでは、旧本番ページはまだ一時保存です。

## チャットで使う

HTML成果物を作って公開したいと依頼すると、モデルは同名ツールでHTML全文・予定公開URL・SHA256・persistenceを準備し、`needs_user_action` で停止します。まだ公開していません。

HTML全文と公開先を確認し、承認チェックと「確認した内容を即時公開」ボタンを押してください。既存の所有者APIトークンを使い、提示された内容だけを保存して公開リンクを返します。確認は10分有効で、期限切れ・再起動時は再準備が必要です。確認待ちはメモリですが、永続保存された公開済みHTMLは別です。モデルが `confirmed=true` を生成しても自己承認できません。

誰でも閲覧可能になるため、個人情報・秘密情報がないことを確認してください。確認画面のHTMLソースはテキストとして表示し、実行しません。

## 確定HTMLを所有者APIから直接送る

既存Bearer認証付き `POST /api/agent/tools/execute` で、所有者が確定したHTMLを明示的に送る場合は即時保存・公開します。モデルの自動自己承認とは別です。

```json
{
  "tool_name": "deploy_instant_website",
  "arguments": {
    "html_content": "<!doctype html><html lang=\"ja\"><head><title>Demo</title></head><body><h1>Sample page</h1></body></html>",
    "filename": "sample_website.html"
  }
}
```

`result.success`、`result.status`、`result.persistence` を確認してください。成功時URLは `RENDER_EXTERNAL_URL` の既存ドメインを使い、期限付き署名URLではありません。Render以外では管理者が実際のオリジンを設定してください。サンドボックスのローカル関数を呼んだだけでは、Renderアプリの公開先と設定を自動共有しません。

確認待ちをAPIから承認する場合は、プレビューのIDとSHA256を、所有者Bearer認証付き `POST /api/agent/websites/confirm` へ送ります。

```json
{
  "approval_id": "<プレビューのapproval_id>",
  "sha256": "<プレビューのsha256>",
  "confirmed": true
}
```

`confirmed=false` はキャンセルです。確認対象・保存先・公開先・期限を検査します。この確認APIはモデルツールとして登録していません。

## 保存・配信の制約

永続モードでは公開HTMLを非公開R2/S3へ保存し、FastAPIがサイズ・SHA256・UTF-8を検証して配信します。バケットを一般公開せず、鍵をブラウザへ送信しません。Render再起動ではオブジェクトは消えませんが、サービス停止・削除・保持設定・ドメイン変更は別です。設定不足・接続失敗時に一時ディスクへ自動フォールバックしません。

`KARRY_WEBSITE_STORAGE=local` は明示的な一時開発用です。以前と同じ `static/instant/` に原子的に保存し、再起動で消える警告を返します。公開関数はUUIDを付けて既存画面と既存成果物を上書きしません。内部パスは返しません。

HTMLはUTF-8、512 KiB以内。128件・合計32 MiBの容量制限を設け、既存成果物を自動削除しません。複数プロセスの容量検査は厳密な分散トランザクションではありません。Token、合言葉、非公開設定の実値を検査しますが、一般的な個人情報を完全に判定する機能ではありません。

配信は既存のCSP sandbox（allow-scriptsあり、allow-same-originなし）を維持します。生成HTMLからアプリの認証情報やストレージへアクセスさせず、フォーム送信・API接続・フレーム等を制限。ローカルJSのインタラクション・CSS・HTTPS画像やフォントは使えますが、動的バックエンド接続を備えたフルWebアプリ配信機能ではありません。URL別表記も同じ検査とヘッダーを使います。

## 検証と配置

永続化版は元348件を維持し、追加テストで保存後読取・障害・再起動相当のメモリ／ディスク初期化・非破壊移行を検証します。確定件数と本番設定状況は永続化ガイドを参照してください。R2有効化・秘密設定・旧HTML移行・本番マージが完了するまでは、コード実装成功を本番永続化成功と説明しません。
