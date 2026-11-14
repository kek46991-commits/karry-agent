# Karry-Agent 2 / Karry-Agent 4（iPhone操作）

`/api/mobile-agent` の **操作側** iPhone アプリ（SwiftUI / iOS 17+）。同じ安全な実装から、別bundle ID・別Keychain・別端末IDの Karry-Agent 2 と Karry-Agent 4 を生成します。観察・診断は別の Android アプリが担当し、このアプリは診断ダッシュボードを持ちません。

## 動作

1. サーバーURLと端末IDを入力し、オーナートークン（`KARRY_API_TOKEN`）で `POST /devices/enroll` を1回だけ呼びます。オーナートークンは保存しません。
2. 返ってきた端末専用トークンだけを Keychain（`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`）に保存します。発行済みトークンを直接入力することもできます。
3. 「今すぐアクションを確認」、自動確認（アプリ表示中のみ10秒ごと）、または App Intent「Karryのアクションを確認」で `POST /devices/{id}/claim` を呼び、1件だけ受け取ります。
4. `ActionPlanner` がバックエンドと同じ許可リスト・フィールド制限で検証し、固定の iOS API に割り当てて実行します。
5. 短い要約（最大300文字）と `error_code` を `POST /actions/{id}/report` に送ります。送信に失敗した結果は次回の確認で再送します。

| アクション | iOS API | 報告する内容 |
| --- | --- | --- |
| `open_app` | 固定の許可リスト（カレンダー・リマインダー・マップ・メッセージ・メール・ショートカット・ミュージック・設定）の URL スキーム | bundle ID |
| `open_url` | `UIApplication.open`（HTTPS のみ、認証情報付き URL は拒否） | ホスト名のみ |
| `create_calendar_event` | EventKit 書き込み専用アクセス（既存予定は読まない） | 作成したことのみ |
| `create_reminder` | EventKit リマインダー（作成のみ） | 作成したことのみ |
| `request_location` | Core Location `requestWhenInUseAuthorization` + `requestLocation` | 精度のみ（座標は送らない） |

### 位置履歴（オプトイン）

「位置履歴をバックグラウンドで記録」をオンにし、iOSの位置情報を「常に」にすると、アプリを閉じていても Core Location の大幅変更位置サービス（おおむね数百m〜の移動ごと）で座標を `POST /api/mobile-agent/devices/{id}/location` へ送ります。オフにする、登録解除する、またはiOS設定で許可を外すと停止します。iOSのステータスバーに位置情報の利用表示が出ます。サーバーは `DATABASE_URL` があれば PostgreSQL の `mobile_location_history` に30日間（取得は最大1000件）保存し、なければメモリ保持です。監視用Androidの「位置履歴」で時系列に確認でき、タップで地図アプリを開きます。
| `choose_file` | システムのドキュメントピッカー（`fileImporter`） | 件数と拡張子のみ（名前・内容は読まない） |
| `choose_photo` | システムの写真ピッカー（`PhotosPicker`、ライブラリ権限不要） | 件数と形式のみ |
| `draft_message` | `MFMessageComposeViewController`（送信はユーザーが行う） | 送信/キャンセルのみ |

ペイロードは文字列・数値として扱うだけで、コードやコマンドとして解釈しません。未知のアクション・フィールドは `invalid_payload` として報告し、何も実行しません。

## セットアップログ
画面上部の `setup` セクションに英語の生ログ（`enrolled: no`、`calendar_write: not_determined` など）を表示します。未決定の権限があれば `calendar_write: request now? (system prompt)` のような質問を1つだけ出し、Yes でそのままiOSの許可ダイアログ（拒否済みなら設定アプリ）へ、No でスキップします。自動確認（10秒、表示中のみ）は既定でオンです。説明画面はありません。

## ペアの登録

- Karry-Agent 1（Android）と2（iPhone）、Karry-Agent 3（Android）と4（iPhone）がテスト用の2組です。
- iPhoneの端末ID初期値は2が `operation-iphone-xxxxxxxx`、4が `operation-iphone-4-xxxxxxxx` です。末尾はアプリごとに生成されます。保存済みのIDは変更しません。
- Androidの「観察対象の操作端末ID」に、対応するiPhone画面の端末IDを **末尾までそのまま** 入れて登録します。両側のサーバーURLも同じにしてください。
- 別ペアや別端末では端末ID・観察者IDを使い回さず、各アプリで個別に登録してください。トークンをコピーする必要はありません。

## ビルドとテスト

```bash
cd ios/KarryOperator
brew install xcodegen   # project.yml を変更したときだけ必要
xcodegen generate
xcodebuild -project KarryOperator.xcodeproj -scheme KarryOperator \
  -destination 'platform=iOS Simulator,name=iPhone 17' test
xcodebuild -project KarryOperator.xcodeproj -scheme KarryOperator4 \
  -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
```

Xcodeを開かずにビルドする場合は、リポジトリ直下から `scripts/build_ios.py` を使います。出力先には新しい（空の）絶対パスを指定し、結果は `result.json` に書かれます（成功で終了コード0、ブロック3、失敗1）。

```bash
python3 scripts/build_ios.py --output "$PWD/out/ios-sim" --mode simulator --test
python3 scripts/build_ios.py --output "$PWD/out/ios-unsigned" --mode unsigned
python3 scripts/build_ios.py --output "$PWD/out/ios-adhoc" --mode adhoc   # 署名素材がなければ blocked
```

- Bundle ID は `--bundle-id`（既定: `com.kishi.karryagent2`、`--scheme KarryOperator4` では `com.kishi.karryagent4`）で、`KARRY_BUNDLE_ID` / `KARRY4_BUNDLE_ID` ビルド設定として渡されます。アプリは実行時の Bundle ID を Keychain 名と自アプリ判定に使います。
- `unsigned` は署名なしの `.app` で、IPAではなくiPhoneには入りません。
- `adhoc` は既定でAPIキーによる自動署名です。`APPLE_TEAM_ID`、`APP_STORE_CONNECT_API_KEY_PATH`、`APP_STORE_CONNECT_API_KEY_ID`、`APP_STORE_CONNECT_API_ISSUER_ID` を秘密情報管理から渡すと、Xcodeが証明書・プロファイルを管理します。MacのiCloud/Xcodeアカウントへフォールバックしません。API認証不足は `blocked`、Apple側の処理失敗は `failed` で停止し、未署名へ切り替えません。
- 既存のキーチェーンとプロファイルを使う場合だけ `--signing-mode manual` を付け、必要なら `--profile` / `--signing-identity` で指定します。
- 有料Developerチーム・チームAPIキーと必要権限・対象iPhoneのUDID登録は所有者が認可する前提です。詳細は [自動署名と配布準備](../../docs/mobile-builds.md) を参照してください。

実バックエンドとの結合テスト（`BackendIntegrationTests`）は環境変数があるときだけ実行されます。

```bash
PYTHONPATH=src KARRY_TASK_STORAGE=memory KARRY_API_TOKEN=local-e2e-owner \
  .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 &
TEST_RUNNER_KARRY_E2E_BASE_URL=http://127.0.0.1:8000 \
TEST_RUNNER_KARRY_E2E_OWNER_TOKEN=local-e2e-owner \
  xcodebuild -project KarryOperator.xcodeproj -scheme KarryOperator \
  -destination 'platform=iOS Simulator,name=iPhone 17' test \
  -only-testing:KarryOperatorTests/BackendIntegrationTests
```

## 実機と署名

- 無人アドホック署名には上記APIキー経路を使います。対話的な開発用インストールでは無料Personal Teamも利用できますが、自動アドホック配布とは別です。リポジトリに `DEVELOPMENT_TEAM` や秘密鍵を保存しません。
- MacのiCloudアカウントを変更する必要はありません。Xcodeで使う署名用アカウントとiPhoneのiCloudアカウントは同一でなくても構いません。2と4の署名には同じTeamも使えますが、bundle IDは別々にしてください。
- TestFlight / App Store 配布には有料の Apple Developer Program が必要です。
- サーバーURLは HTTPS のみ（シミュレータ確認用に `localhost` / `127.0.0.1` だけ HTTP 可）。

## 制約

- iOS ではバックグラウンドで常時ポーリングできないため、確認はアプリ表示中か App Intent（ショートカット）実行時だけです。プッシュ通知は未対応です。
- iOS は bundle ID で任意のアプリを開く公開 API がないため、`open_app` は固定の許可リスト外を `app_unavailable` として報告します。
- シミュレータではメッセージ作成画面が使えないため、`draft_message` は `intent_unavailable` になります（実機で確認してください）。
- バックエンドの端末トークンは現状メモリ保存のため、サーバー再起動後は「再登録が必要」と表示されます。
