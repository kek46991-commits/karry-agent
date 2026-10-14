# タスクへの画像添付

提供コードの `/api/upload_image` を既存FastAPIに追加し、画像選択・プレビュー・添付取消・画像付きチャットを既存HTML UIへ一括統合しました。Reactへの全面移行や新しいフロントエンドビルドは不要です。PR #10はmainへマージ済みのため、その最新mainを基に [PR #11](https://github.com/kek46991-commits/karry-agent/pull/11) へ一括保存しました。main未マージ・本番未反映です。

## 使い方

所有者APIトークンでチャットに接続し、タスクを作成または選択して「画像を添付」からファイルを選びます。アップロード後にプレビューが表示され、次の送信で選択画像とテキストをGeminiへ渡します。テキストが空なら「添付画像を確認してください。」として送信します。アップロードしただけでは画像の認識や生成は実行しません。

×は画像を単に隠すのではなく、サーバーの `pending_images` からも外します。過去に保存した画像バイト・履歴参照の永久削除ではありません。タスクを切り替えると別タスクの画像を送らないようプレビューと選択を復元します。履歴の「画像を確認」から、認証付きで過去の画像を確認できます。認証情報・画像base64はブラウザのlocalStorageへ保存しません。

以前の画像を毎回自動で再送することはありません。送信済み・取消済み画像IDを古い画面から新しい送信へ含めると409で拒否します。同じrequest_idの完了済みリクエストは、画像の消費後も保存済み回答を再生し、モデルを重複実行しません。生成が失敗した場合は画像の添付を残し、手動で再試行できます。

## 形式と制限

JPG/JPEG・PNG・WEBP・GIFを、ファイル名や申告MIMEだけではなく実際のバイナリで検証します。入力は4 MiB、正規化後は2 MiB、画素数は16 MP、次回送信は最大4枚、保存済み画像と未完了予約は合計100件/タスクです。未完了予約も次回4枚の容量枠に数えます。明示memoryモードの全一時画像には32 MiBの上限があります。

Pillowで検証・向き補正・再エンコードし、EXIF等のメタデータを除きます。JPEGはquality=95で再エンコードするため、完全に同一のファイルバイトではありません。比率・画素数を勝手に縮小したり切り抜いたりはしません。容量超過時は画像を圧縮・小さくして再送するよう案内します。画像中に写っている文字や機密情報まで消す機能ではないため、送信前に内容を確認してください。

GIFはGeminiの対応MIMEに含まれないので、先頭フレームを静止PNGへ変換し、返信とUIで明示します。Geminiへの入力は `types.Part.from_bytes` を使用し、data URIをテキストとして渡しません。最大4枚×2 MiBと短い文脈は、公式のinlineリクエスト20 MB制限を考慮した上限です。[1] 画像付きの単独URL解析も取得1回＋提案生成1回の短縮経路を維持します。

デコードは同時1件に制限し、向き補正の不要なコピーを避けます。HTTP本文はmultipartパーサーより前にも上限を設け、認証なしのアップロード本文は読みません。UploadFileは例外時もcloseします。[2]

## 非公開保存と障害復旧

画像にはHTML用の公開 `/static/instant/` 配信を使いません。タスクと同じ非公開R2/S3バケットの `karry-tasks/images/<session_hash>/<image_id>.bin` に保存します。実際の先頭prefixは `KARRY_TASK_S3_PREFIX` に従います。タスクJSONには画像ID・形式・寸法・サイズ・SHA256と参照だけを残し、大きなbase64は保存しません。

バイナリPUTより先にCASで予約参照を保存し、容量枠を確保します。保存の結果が不明な場合も、画像をJSONから到達不能な孤立ファイルとして増やさず、予約を残して上限へ数えます。保存後に参照を確定して初めて次回添付へ入れます。タスクを再読込すると、予約先の実バイト・ContentType・サイズ・ハッシュを照合し、確認できた画像だけを復旧します。

バイナリがまだない・確認できない場合は成功扱いにせず予約を残します。接続を修復して同じ画像を再選択すると、内容ハッシュで同じ予約IDを再利用して保存を再試行できます。恒久的に使えないストレージのまま予約が4件に達した場合、新しい画像の追加は拒否し、接続修復または同じ画像の再試行が必要です。確認不能なファイルの自動削除や無検証のGCは行いません。

完了済み会話の保存と使用した画像の消費は同じCAS更新です。生成失敗・履歴保存失敗・選択の競合では添付を失いません。未完了アップロードの件数をAPIと画面へ返し、成功した添付と区別します。別タスクでのプレビュー・送信・取消は拒否します。明示memoryモードで参照の追加が失敗した場合は、その新規画像の未参照バイトだけを回収し、既存の参照済み画像は消しません。

## API

すべて所有者の `Authorization: Bearer <token>` が必要です。トークンは `KARRY_API_TOKEN` の設定値を使い、URLやソースへ含めません。

- `POST /api/upload_image`：multipartの `session_id` と `file`。`status`, `filename`, `session_id`, `image_url`（正規化画像のdata URI）, `image_id`, `image`（メタ情報）, `warning` を返します。未知タスクは404、認証不正は401、形式不正は400、容量超過は413、実行/選択競合は409です。
- `GET /api/images/{image_id}?session_id=...`：同じタスクに属する確定済み画像のdata URIとメタ情報を返します。認証付き・no-storeで、共有用の公開URLではありません。
- `DELETE /api/pending_images/{image_id}?session_id=...`：次回添付だけを外します。過去画像と会話を消す操作ではありません。
- `GET /api/sessions/{session_id}`：`image_assets`, `pending_images`, `image_uploads_pending` と、会話の `images` 参照を返します。予約済みバイナリの復旧もこの読込で確認します。
- `POST /api/chat`：既存の `session_id`, `prompt`, `stream`, `request_id` に `image_ids` を追加。指定時は最大4つの次回添付IDだけを送り、省略時はサーバーの次回添付を使用します。空配列はテキストのみの送信です。

multipart送信ではContent-Typeを手書きせず、ブラウザ/curlが境界を生成します。

```bash
curl --fail --silent --show-error \
  https://karry-agent-1.onrender.com/api/upload_image \
  -H "Authorization: Bearer $KARRY_API_TOKEN" \
  -F "session_id=$SESSION_ID" \
  -F "file=@./screenshot.png"
```

この例は実装版のマージ・デプロイとストレージ設定後に利用するものです。今回、この本番アップロードは実行していません。画像は送信時にGeminiへ渡るため、画像の内容と送信先を了承して使用してください。

## 設定と本番適用

追加の外部サービスや別のAPIキーは不要です。既存の `KARRY_TASK_STORAGE=s3`、非公開バケットとS3/R2署名鍵、所有者の `KARRY_API_TOKEN` を再利用します。認識するチャットには従来どおり `GEMINI_API_KEY` が必要です。R2/S3設定不足時にローカルへ黙って戻しません。`KARRY_TASK_STORAGE=memory` は明示的な一時開発モードで、再起動で画像は消えます。

本番にはPillowとpython-multipartを追加しています。requirements.txtとpyproject.tomlの両方へ反映済みです。機能のマージ・Renderデプロイ・設定確認後に動作確認してください。今回、本番マージ・本番画像送信・実R2/S3保存・実Gemini画像認識は実施していません。

## 検証

Pythonは **500 passed, 2 skipped**（元456件＋追加44件）、DOMは **20 passed**（元12件＋画像8件）。構文・依存整合性・差分・wheelビルドを確認しました。合成画像、HTTP TestClient、実SDKの画像Part、S3モックだけで、4形式・MIME偽装・サイズ・EXIF・取消・別タスク・生成失敗・保存失敗・再送・再起動相当の復元・URL短縮経路・保存予約の復旧・一時メモリの失敗回収を検証しています。

読み取り専用レビューで確認した、古い選択IDの再利用と孤立バイナリの可能性は、pending所属の検査とバイナリPUT前のCAS予約へ修正して回帰検証しました。ライブモデルを呼び出して認識品質を評価した結果ではありません。

## References

[1]: https://ai.google.dev/gemini-api/docs/image-understanding "Gemini image understanding: supported MIME types and inline input limits"
[2]: https://fastapi.tiangolo.com/tutorial/request-files/ "FastAPI: multipart uploads and UploadFile lifecycle"
