# 人物を喋らせる：外部SadTalker連携

動画作成画面の「人物を喋らせる（SadTalker）」をONにし、人物画像と読み上げる文章を入力すると、既存TTSで音声を一度作成してから外部SadTalkerへ渡します。RenderではSadTalkerモデル・Torch・CUDAを動かしません。外部サービスが作成した口パク動画を取得し、元のTTS音声と指定の16:9／9:16・秒数で軽量MP4合成します。

接続先が未設定、混雑・待機期限超過、外部エラー、異常な動画応答の場合は、同じ音声を使って既存のローカル画像＋音声合成へ自動で切り替わります。結果は`lip_sync.status=succeeded`（口パク）または`fallback`（ローカル合成）で区別し、画面にも理由を表示します。音声自体を作れなかった場合は既存の仮音声・端末読み上げの警告を保持し、口パク成功とは表示しません。

## 接続先の設定

```dotenv
KARRY_SADTALKER_SPACE=YOUR_ACCOUNT/YOUR_SADTALKER_SPACE
KARRY_SADTALKER_API_NAME=/test
KARRY_SADTALKER_TIMEOUT=90
# private Spaceのみ必要：HF_TOKENをRenderのSecretに設定する
```

Space名はHugging Faceの`owner/name`形式で指定します。画像・音声はこの外部環境へ送信されるため、信頼できる管理者のSpaceを選び、使用権・本人同意のある人物画像を使ってください。既定の接続先は空、UIトグルはOFFです。運用中のKarry-agent認証トークンは外部へ送りません。private Space用のHF_TOKENはサーバーだけで使用し、画面や動画ジョブJSONへ返しません。

公式デモは`vinthony/SadTalker`ですが、2026年10月22日の確認ではBuild errorでした。無料公開Spaceの稼働・待ち時間・利用枠は保証できません。自分で稼働を確認した互換Spaceを設定するか、設定しない状態のローカルフォールバックを利用してください。この実装はSpaceの自動複製やGPUプランへの変更、有料外部APIの自動利用をしません。[1] [2]

公式Spaceの16入力プロファイルに合わせ、`Client.view_api()`で互換性を確認してから画像・WAVを送信します。接続先のAPIが異なる場合は推測送信せずフォールバックします。Gradio SDKだけを導入し、外部通信は期限付きの専用子プロセスに隔離します。既定90秒、最大180秒で待機を終え、子プロセスと一時ファイルを回収します。外部側で受理済みのジョブが後から完了することはあり得ますが、結果はローカル動画を上書きせず、自動再送も行いません。[3] [4]

## APIから使う

```bash
curl -X POST https://karry-agent-1.onrender.com/api/video/jobs \
  -H "Authorization: Bearer $KARRY_API_TOKEN" \
  -F 'session_id=YOUR_TASK_ID' \
  -F 'prompt=人物のナレーション動画' \
  -F 'aspect_ratio=9:16' -F 'duration_seconds=10' \
  -F 'narration_text=こんにちは。ジミーです。' -F 'voice=jimmy' \
  -F 'sadtalker=true' -F 'image=@./portrait.png'
```

SadTalkerをONにする場合は画像と読み上げる文章が必須です。非同期ジョブを202で受け取り、既存の`GET /api/video/jobs/{id}?session_id=...`で進行を確認できます。`sadtalker=true`では有料動画プロバイダーを選ばず、外部SadTalkerとローカル合成だけを使用します。既存の`lip_sync=true`のみの指定はCPU口パクとして互換性を保持します。UIでもCPU口パクとSadTalkerは排他的に選択します。TTSは既存設定を引き継ぐため、Gemini等を選ぶ設定の場合はそのサービスの料金・利用条件が適用されます。失敗時のローカル映像合成は外部動画生成APIを使いません。

「0円プレビュー」専用ボタンは従来どおりSQLプロジェクトAPIの画像＋音声合成で、SadTalkerを呼びません。口パクトグルをONにした場合も、このボタンの結果を口パク動画とは表示しません。チャット、シークレットモード、プロジェクトCRUD、既存の有料動画経路は保持しています。

## ステータスランプと長尺指定

生成した動画カードと「動画プロジェクト一覧」に、黄色（生成中）・緑色（完了）・赤色（エラー）のランプと状態の文字を表示します。未生成のプロジェクトは未生成として区別します。SadTalkerが失敗してもローカル合成が成功すれば完成として表示し、静止画フォールバックであることを別に明示します。

「一覧を更新」でSQLに保存済みの動画プロジェクトを読み込み、各カードの「長尺指定（秒）」と保存ボタンから秒数設定を変更できます。現在の対応範囲は1～120秒です。0秒や121秒以上は勝手に丸めずエラーにし、生成中の編集と更新競合も拒否します。保存は秒数設定の変更で、SadTalker等への再送信や有料生成を自動実行しません。シークレットモードや未接続時は一覧の読み込み・更新を行いません。

## References

[1]: https://github.com/OpenTalker/SadTalker "SadTalker official repository"
[2]: https://huggingface.co/spaces/vinthony/SadTalker/blob/main/app.py "Official SadTalker Space application"
[3]: https://www.gradio.app/docs/python-client/client "Gradio Python Client reference"
[4]: https://www.gradio.app/docs/python-client/job "Gradio Python Job reference"
