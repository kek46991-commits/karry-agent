# SadTalker外部連携

Render内ではSadTalkerモデルやTorchを動かさず、設定したHugging Face Gradio Spaceへ検証済み人物画像と既存TTSのWAVを送ります。専用の非同期アダプターは送信・待機・ダウンロードを一度だけ実行し、期限超過や異常応答を安全な理由コードで返します。CPU処理は既存FFmpegによる軽量合成だけです。

`/api/video/jobs`へ`lip_sync`を追加し、ONの場合は有料動画プロバイダーではなくSadTalker→既存ローカル合成の経路を使います。音声は一度だけ生成し、画像と実音声があれば外部連携を試します。未設定・混雑・失敗・音声生成失敗はローカルへ戻し、完成動画が口パクなのか静止画合成なのかをAPIとUIに明示します。SQLプロジェクトAPIと無料プレビューボタンはそのまま保持します。

外部接続先は既定では空で、UIのトグルも既定OFFです。ON時は外部への画像・音声送信を明示します。人物画像は利用権・本人同意を確認して利用します。無料公開Spaceは稼働や待ち時間を保証しません。確認時点の公式SpaceはBuild errorで、これを稼働済みとは扱いません。

構成は`src/karry_agent/sadtalker.py`（非同期アダプター）、`sadtalker_worker.py`（有限時間の外部SDK通信）、`video_suite_api.py`（既存ジョブ経路への統合）、`video_suite.py`（結果の再合成と公開ビュー）、`static/index.html`（トグルと結果表示）です。既存UIの視覚スタイルを引き継ぎ、新しい装飾や別アプリは作りません。
