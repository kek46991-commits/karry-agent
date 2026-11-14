# Karry-Agent 1 / Karry-Agent 3 (Android 観察・診断アプリ)

Android側の観察エージェントです。iPhoneの操作エージェント(Swift/App Intents)が
Karry-Agentへ送った操作イベントとエラー診断だけを、オーナー認証のうえで表示します。

- Android端末のメッセージ・写真・ファイル・位置情報などは一切読み取りません。
- 端末専用トークンは Android Keystore で暗号化して保存します。
- 観察トークンは1台のiPhone操作端末だけに紐づきます(`observed_device_id`)。

## セットアップログ
起動時に画面上部へ英語の生ログ（`key: yes|no`）を表示します。ネットワーク権限、Keystore、バックエンド到達性、登録状態、`ready` を自動判定し、質問は `enroll: register this observer now?` の Yes/No だけです。説明画面はありません。全体の状態一覧は `docs/mobile-readiness-log.md` を参照してください。

## 連携するバックエンド
`docs/mobile-agent-pair.md` と `src/karry_agent/mobile_agents.py`。
観察用エンドポイント:
- `GET /api/mobile-agent/observers/{observer_id}/devices/{device_id}/actions`
- `GET /api/mobile-agent/observers/{observer_id}/devices/{device_id}/diagnostics`

同じ監視コードから、独立インストールできる2つのAPKを生成します。

- `agent1`: Karry-Agent 1。既定で `operation-iphone` を観察。
- `agent3`: Karry-Agent 3。別application IDで、既定で `operation-iphone-4` を観察。

iPhone側は端末IDの末尾にランダムな文字列を付けます。登録前に、観察対象IDを **iPhoneアプリに表示される端末ID全体**（末尾を含む）へ変更してください。上の既定値だけでは自動接続しません。複数のAndroid端末で使う場合は観察者IDも別々にしてください。

## ビルド
```
cd android-observer
./gradlew \
  :app:testAgent1DebugUnitTest :app:assembleAgent1Debug \
  :app:testAgent3DebugUnitTest :app:assembleAgent3Debug
```
エミュレーターからローカルのKarry-Agentへ接続する場合は `http://10.0.2.2:<port>` を使用します。
