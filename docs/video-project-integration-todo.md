# 動画統合の進捗

- [x] プロジェクトID・タイトル・画像・16:9/9:16・秒数・TTS・状態をSQLite/PostgreSQLで保存・取得・更新・削除する。
- [x] アップロード画像とローカルTTSを合成し、外部有料APIなしでMP4プレビューを生成する。
- [x] 環境変数がある場合だけ、明示的なrender呼び出しから非同期Kling-via-Replicate／Runwayを実行する。
- [x] 要求された`POST /api/video/projects`、`GET /api/video/projects`、`POST /api/video/generate/preview`、`POST /api/video/generate/render`を本体に取り込む。
- [x] 既存`/api/video/jobs`、動画UI、secret_chat lifespan、KARRY_API_TOKEN認証を維持し、既存動画UIから無料プレビューを実行できるようにする。
- [x] 最新mainに対する統合テストと既存の回帰テストを通し、PRとして納品する。
