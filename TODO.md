# 統合リファクタリング

- [x] `execute_command`、`read_file`、`write_file`、`list_dir` を既存レジストリと統合する。既存のファイル保護・旧引数との互換性を維持し、任意シェル実行は明示設定とAPI認証を必要とする。
- [x] Calendar / Photos のOAuth認証を共通化し、既存のJSON Secret・tokenファイル・環境変数3点を維持する。MapsはRoutes APIに接続し、PhotosはPicker選択フローを実装する。エラーを成功として返さない。
- [x] Gemini Tool Callingの手動実行を登録し、関数ID・モデルContent・例外とユーザー操作待ちを正しく処理する。Calendarのプレビュー確認フローに end_time と description を保持する。
- [x] 既存APIにバインドし、認証付きツールAPIを追加する。既存テストと新規統合テストを実行し、GitHubへ変更ブランチとPRを保存する。
- [ ] 本番Renderへ配置し、配置されたバージョンを確認する。mainの自動デプロイ設定を考慮し、マージ前に実装内容と設定事項をユーザーに提示する。

ローカル検証: 149 passed, 2 skipped。構文、依存整合性、wheelビルド、差分チェック成功。ライブGoogle/Gemini検証は秘密情報未設定のため未実施。
GitHub PR: https://github.com/kek46991-commits/karry-agent/pull/4 。本番配置は未実施。
