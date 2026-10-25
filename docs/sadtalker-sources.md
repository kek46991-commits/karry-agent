# SadTalker連携で確認した公式仕様

SadTalkerは人物の静止画像と音声から話す顔の動画を生成するモデルで、公式READMEはHugging Face Spaceと外部サービスへのリンクを提供しています。RenderにはモデルやTorchの推論環境を導入しません。[1]

公式Spaceの`app.py`では、画像・音声のほか前処理、still、enhancer、batch、256/512解像度、pose、renderer、expression、reference、idle、length、blinkを含む16個の入力を`SadTalker.test`へ渡しています。確認したページではSpaceがBuild errorでした。このSpaceを必ず無料で利用できるとは案内しません。[2]

Gradioクライアントの`submit`はジョブを返し、`result(timeout)`には待機期限を指定できます。実装はSDKの同期通信を専用子プロセスへ隔離し、親の期限超過時には子プロセスも回収します。外部ジョブ自体のキャンセルやデータ削除の完了は保証しません。[3] [4]

## References

[1]: https://github.com/OpenTalker/SadTalker "SadTalker official repository"
[2]: https://huggingface.co/spaces/vinthony/SadTalker/blob/main/app.py "Official SadTalker Space application"
[3]: https://www.gradio.app/docs/python-client/client "Gradio Python Client reference"
[4]: https://www.gradio.app/docs/python-client/job "Gradio Python Job reference"
