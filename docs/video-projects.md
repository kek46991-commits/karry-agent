# 動画プロジェクト管理と0円プレビュー

Karry-agent本体へSQLベースの動画プロジェクト管理を組み込みました。既存のCreative Video Suiteはそのまま保持し、新しいプロジェクトAPIとローカルプレビューを併設します。動画UIにある「0円プレビュー」は、アップロード画像を静止画としてMP4にし、任意のローカル英語TTSを付ける機能です。AIアニメーションや口パクではありません。外部有料生成APIは呼びません。

## 本体の認証とライフサイクル

全ての新しい動画APIは既存の`KARRY_API_TOKEN`で認証します。`VIDEO_API_TOKEN`を設定しても本体認証を迂回できません。画像アップロードはmultipart解析前にも所有者トークンと容量を検証します。トークン・非公開ファイルの絶対パス・外部CDNの署名URLをAPIへ返しません。

`app.py`のlifespanは既存のシークレットチャットのcleanupを保持したまま動画サービスを起動・終了します。動画DBやストレージの設定が失敗した場合は新動画APIを503にし、チャットとhealthを停止させません。`KARRY_VIDEO_PROJECTS_ENABLED=0`で新しいSQL動画機能だけを無効化できます。既定は有効です。

## ローカル起動

```bash
sudo apt-get update && sudo apt-get install -y ffmpeg espeak-ng
python -m pip install -e '.[test]'
export KARRY_API_TOKEN='your-long-random-token'
export VIDEO_DATABASE_URL='sqlite:///./data/video_projects.db'
export VIDEO_STORAGE_ROOT='./data/video'
uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

既存DockerfileはFFmpeg／ffprobe／eSpeak NGを導入します。**現行render.yamlはネイティブPythonランタイムです。** このPRだけでは稼働中のRenderサービスがDockerへ自動変換されるわけではありません。ネイティブ環境にFFmpeg／ffprobeがない場合はプレビューを利用できません。eSpeakがない場合でもTTSなしの無音プレビューは利用できます。本番で無料プレビューを使うにはDockerランタイム等でこれらのOSツールを配置してください。この実装ではRenderのサービス・ランタイム・料金プランを変更していません。

## DBとファイル保存

`VIDEO_DATABASE_URL`を優先し、未指定なら既存の`DATABASE_URL`、どちらもなければSQLiteを使用します。PostgreSQLは`postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE`で利用できます。起動時に`video_projects`／`video_jobs`を初回作成します。既存テーブルを削除・変更する処理はありません。既存DBにこれらと同名のテーブルがある場合は事前にスキーマを確認してください。将来のカラム変更はAlembic等のmigrationが必要です。

プロジェクトにはID、タイトル、非公開画像キー、16:9／9:16、1〜120秒、TTS設定、生成状態、出力キー、更新versionを保存します。`VIDEO_STORAGE_ROOT`には画像・MP4・外部ジョブ受領記録を保存します。DBだけでなくこのディレクトリも永続ディスク等で保護してください。Renderの通常のローカルファイルシステムでは再デプロイ時に消失します。新しいSQL動画保存はR2／S3に自動転送しません。既存Creative Video Suiteの保存方法は変更していません。

## 追加API

要求された4つのルートは本体に登録済みです。

```text
POST /api/video/projects                  画像付きプロジェクト作成
GET  /api/video/projects                  一覧・ページネーション
POST /api/video/generate/preview          ローカルMP4生成（202）
POST /api/video/generate/render           明示的なAI動画生成（202）
```

プロジェクトのGET／PATCH／DELETE、画像取得、プロジェクト最新MP4の取得も`/api/video/projects/{project_id}/*`から利用できます。PATCHとDELETEは最新のversionが必要です。生成中の変更・削除は409で拒否します。

既存UIが使用する`/api/video/jobs`と衝突しないよう、新しいSQL生成ジョブは`/api/video/project-jobs/{job_id}`を使用します。生成開始のJSONにある`status_url`をそのままポーリングし、`succeeded`になったら`download_url`を認証付きfetchで取得してください。`GET /api/video/project-config`は無料プレビューの利用可否を返します。

```bash
curl -sS http://127.0.0.1:8000/api/video/projects \
  -H "Authorization: Bearer $KARRY_API_TOKEN" \
  -F 'title=Preview' -F 'aspect_ratio=9:16' -F 'duration_seconds=10' \
  -F 'tts={"text":"This is a local preview.","voice":"en"}' \
  -F 'image=@./image.png'

curl -sS -X POST http://127.0.0.1:8000/api/video/generate/preview \
  -H "Authorization: Bearer $KARRY_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"project_id":"PROJECT_UUID","request_id":"REQUEST_UUID"}'
```

`request_id`にはUUIDを使い、同じ処理の再送では同じUUIDを維持してください。省略した場合はサーバーが発行します。同一プロジェクトの多重生成をDBロックで防ぎます。正常終了時は同期FFmpeg処理の終了と未確定ファイルの削除を管理します。プロセスの強制停止・再起動では期限切れleaseと受領済みIDを使って明示的にresumeしてください。これは専用の分散タスクキューではないため、初期運用はworkers 1を推奨します。

## TTSとAI生成の制限

無料MP4は軽量な360×640／640×360、24fps、H.264／AACです。画像は全体を保持したcontain＋黒帯で変換します。JPEG／PNG／WebPの実デコード、容量・画素数・symlink等の安全チェックを行います。日本語ローカルTTSは標準構成に含まれません。UIのローカル英語TTSチェックを外すと音声なしのプレビューです。ONの場合だけvoice=enの英語音声を付けます。指定時間より音声が長ければ`speech_too_long`で失敗し、音声を勝手に切り捨てません。

新しいAIレンダリングはReplicate経由KlingとRunwayに対応します。`REPLICATE_API_TOKEN`または`RUNWAY_API_KEY`がある場合だけ有効です。`POST /api/video/generate/render`を明示的に呼んだ場合にだけ有料startを送ります。この新しいアダプターは単発・最大10秒です。既存Creative Video Suiteの分割生成機能は別経路として保持します。直接の`KLING_API_KEY`接続は実装していません。

送信結果不明時は自動再送せず、プロジェクトを確認待ちにします。受領済み外部IDがあれば`POST /api/video/project-jobs/{job_id}/resume`で同じ生成の状態確認・ダウンロードだけを再開できます。`abandon`は外部キャンセルではありません。外部ジョブを確認したうえで`{"acknowledge_unknown":true}`を送った場合だけロックを解放し、その後の新規renderは重複課金になり得ます。

この版は単一オーナーの共有Bearer方式です。新しいSQLプロジェクトはタスクIDごとの権限分離を実装していません。UI内では結果を生成したタスクの表示に紐づけますが、複数利用者への公開にはowner_idと所有者フィルターの追加が必要です。
