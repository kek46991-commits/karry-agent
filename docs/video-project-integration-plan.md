# 動画プロジェクト管理の統合

SQLite／PostgreSQLのプロジェクトCRUD、アップロード画像＋ローカルTTSの無料MP4プレビュー、認証情報がある場合だけ明示実行する非同期AIアダプターを、既存Karry-agentへ組み込みます。既存PR30の動画生成UI・`/api/video/jobs`・シークレットチャット・既存認証は保持します。

追加対象は`src/karry_video/`の動画パッケージ、`src/karry_agent/video_projects.py`のライフサイクル／認証ブリッジ、`app.py`の取り込み、依存定義、Dockerのローカルメディアツール、既存動画UIの0円プレビューボタンです。プロジェクトAPIは`/api/video/projects`、生成は`/api/video/generate/preview`と`render`です。ジョブ状態・ダウンロードは既存ルートと競合しない`/api/video/project-jobs/*`へ配置します。

本体の所有者トークン`KARRY_API_TOKEN`を認証境界にし、`VIDEO_API_TOKEN`では迂回できません。元のsecret_chat lifespanを保持し、動画DB起動失敗がチャット全体を停止させないようにします。画像とMP4は非公開ストレージに保存します。本番DBの接続情報・秘密値はコードへ含めません。

外部有料生成の実テスト、mainへのマージ、Render本番の設定変更はこの実装段階では行いません。検証後に機能ブランチをpushし、変更をまとめたPRを作成します。
