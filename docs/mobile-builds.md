# モバイルビルドと配布準備の自動化

`python3 scripts/mobile_release.py` が、既存のAndroid観察アプリ（1/3）、iOS操作アプリ（2/4）、検証用ファイル、ZIPを生成します。Androidの操作版2を新設するものではありません。

**「ビルド成功」と「実機へ配布可能」は別です。** Appleの署名資格情報・登録端末、HTTPS公開先、対象ドメインの権限は自動生成できません。このCLIは本番へのデプロイやDNS変更、Apple/iCloudへのログイン、端末の許可画面の回避を行いません。

## Android（Linux / macOS）

必要環境: Python 3.10+、Java 17、Android SDK（API 35、build-tools、platform-tools）。既存SDKを `ANDROID_HOME` / `ANDROID_SDK_ROOT` または標準の保存場所から検出します。

```sh
python3 scripts/mobile_release.py --platform android --output dist/android-test
```

両flavorのユニットテスト、debug APKビルド、APK署名の検証を実行します。起動済みのエミュレータがちょうど1台ある場合は `--install-android` でそのエミュレータへのインストールも行えます。物理端末には自動インストールしません。APKはテスト専用のdebug署名で、端末側の「不明なアプリのインストール」許可が必要な場合があります。

## iOS（Mac + Xcode）

Xcode、利用可能なiOS Simulator runtime、およびXcodeGenが必要です。MacのiCloudアカウントを変更しません。

```sh
# Appleアカウント不要: Simulator向け、XCTestも実行
python3 scripts/mobile_release.py --platform ios --ios-mode simulator --ios-test \
  --bundle-id com.kishi.karryagent2 --output dist/ios-simulator

# 署名なしの実機向けRelease。実機へインストールできるIPAではありません
python3 scripts/mobile_release.py --platform ios --ios-mode unsigned \
  --bundle-id com.kishi.karryagent2 --output dist/ios-unsigned

# 4をビルドする場合はschemeとbundle IDを両方指定
python3 scripts/mobile_release.py --platform ios --ios-scheme KarryOperator4 \
  --bundle-id com.kishi.karryagent4 --output dist/ios-agent4
```

署名済みアドホックには、対象bundle ID・Team・登録端末に一致する期限内のプロビジョニングプロファイルと、その証明書に対応する秘密鍵がMacのキーチェーンに必要です。利用可能な証明書がない場合は **blocked** となり、署名なしへの暗黙の切り替えはしません。

```sh
python3 scripts/mobile_release.py --platform ios --ios-mode adhoc \
  --bundle-id com.kishi.karryagent2 --team-id YOUR_TEAM_ID \
  --profile /secure/path/app.mobileprovision \
  --output dist/ios-adhoc
```

`--platform all` は両方のSDKがあるMac用です。LinuxではAndroidを作成できてもiOSはblockedとなり終了コード2です。出力ディレクトリは毎回新しい場所を指定してください。既存成果物を再利用して成功と誤認することを防ぐため、空でない場所は拒否します。

## ドメイン検証ファイルとローカルモック

ビルド時に以下を自動生成します。ビルドなしで生成するには `--platform prepare` を使います。

- `verification/.well-known/karry-agent-verification.txt`
- `verification/dns.txt`
- `verification/verification.json`
- `verification/loopback-test.json`

何も指定しなければ `example.invalid` と明確なローカル専用文字列の **fixture_only** です。本番認証に使えません。内蔵HTTPモックは `127.0.0.1` の空きポートだけで一時起動し、well-knownの内容一致と終了を自動確認します。アプリのAPIを公開せず、本番の認証・公開IP検証には変更を加えません。

実ドメイン用には、所有権確認サービスが発行した **公開用challenge**（`karry-agent=...`）をファイルで渡します。オーナートークンや承認リンクのトークンではありません。

```sh
python3 scripts/mobile_release.py --platform prepare --domain your-domain.com \
  --challenge-file /path/public-challenge.txt --output dist/domain-preparation
```

生成されるTXT名は `_karry-agent-verification.<domain>` です。これは **prepared_not_published** であり、所有者承認・公開・検証完了を意味しません。生成だけでドメイン承認は成立せず、Appleの署名とも無関係です。

## OTA構成

署名済みアドホックが実際に成功した時のみ `--ota-base-url https://your-domain.com/ota` を追加すると、`ota/manifest.plist`、署名済みIPA、`ota/install.html` を生成します。未署名またはSimulatorのアプリからは生成しません。メタデータはexportされたIPAから取得します。

このコマンドはHTTPSサーバーへアップロードしません。URLで示した場所への公開、有効なTLS証明書、対象端末の登録とインストール承認が別途必要です。配布先が未指定の場合、OTA配布を完了したとは報告しません。

## 成果物と結果

- `Karry-mobile-builds.zip`: 指定したビルド成果物、検証ファイル、結果、簡易案内のみ。秘密鍵や作業用ディレクトリは同梱しません。
- `app-verification.txt`: SHA-256整合性リスト。Apple署名検証やドメイン認証の証明ではありません。
- `result.json`: 各工程の `succeeded` / `failed` / `blocked` と、残る手作業を明示。未完了工程があれば終了コード2です。

これらはアプリの配布準備であり、バックエンドの公開・端末登録を代行しません。Androidの観察対象IDは対応するiPhoneの端末ID全文に合わせてください。

Apple公式: [Ad Hocプロファイル](https://developer.apple.com/help/account/provisioning-profiles/create-an-ad-hoc-provisioning-profile/) / [登録端末への配布](https://developer.apple.com/documentation/xcode/distributing-your-app-to-registered-devices)
