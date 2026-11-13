# Karry Observer (Android 観察・診断アプリ)

Android側の観察エージェントです。iPhoneの操作エージェント(Swift/App Intents)が
Karry-Agentへ送った操作イベントとエラー診断だけを、オーナー認証のうえで表示します。

- Android端末のメッセージ・写真・ファイル・位置情報などは一切読み取りません。
- 端末専用トークンは Android Keystore で暗号化して保存します。
- 観察トークンは1台のiPhone操作端末だけに紐づきます(`observed_device_id`)。

## 連携するバックエンド
`docs/mobile-agent-pair.md` と `src/karry_agent/mobile_agents.py`。
観察用エンドポイント:
- `GET /api/mobile-agent/observers/{observer_id}/devices/{device_id}/actions`
- `GET /api/mobile-agent/observers/{observer_id}/devices/{device_id}/diagnostics`

## ビルド
```
cd android-observer
./gradlew :app:testDebugUnitTest :app:assembleDebug
```
エミュレーターからローカルのKarry-Agentへ接続する場合は `http://10.0.2.2:<port>` を使用します。
