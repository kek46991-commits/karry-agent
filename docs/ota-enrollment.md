# 自動UDID取得とOTA配信（Karry-Agent 2）

従業員・テスターがUDIDを自分で調べなくても、iPhoneのSafariから登録ページを開くだけで、UDIDの自動送信 → 登録 → OTAインストールまで進める仕組みです。`src/karry_agent/ota_enrollment.py` が提供します。

**前提（Apple側の制約）**: ad hoc配布では「登録済みUDIDを含めて署名したIPA」しかインストールできません。新しいUDIDは、受信後に管理者がApple Developerに端末登録し、`scripts/mobile_release.py --ios-mode adhoc` で再ビルドして `KARRY_OTA_DIR` を更新する必要があります。それまで対象端末には「登録完了（ビルド待ち）」画面が表示されます。

## 流れ

1. `GET /ota/` — 登録ページ。「デバイスを登録して進む」を押す。
2. `GET /ota/profile` — 30分有効の使い捨てトークンを発行し、Appleの構成プロファイル（`PayloadType: Profile Service`）を返す。要求する属性は **UDID・PRODUCT・VERSION のみ**（IMEI・ICCID・シリアルは要求しない）。
3. iOSが設定アプリでプロファイルをインストールすると、端末がPKCS7署名付きplistを `POST /api/register-udid/{token}` へ送信。サーバーはplistを取り出してUDIDを検証・保存し、`301 → /ota/status/{token}` を返す。
4. `GET /ota/status/{token}` —
   - 署名済みIPAがそのUDIDを含む → `itms-services://?action=download-manifest&url=…/ota/manifest.plist` へ自動遷移（ボタンも表示）。
   - 含まない → 登録完了・ビルド待ちの案内（UDIDは一部マスク表示）。
   - トークン無効/期限切れ → 410、端末データ不正 → 400 の簡潔なガイド。
5. `GET /ota/manifest.plist` / `GET /ota/app.ipa` — `KARRY_OTA_DIR` の署名済みIPAから配信。

## 設定

| 環境変数 | 内容 |
| --- | --- |
| `KARRY_OTA_ENROLLMENT=1` | 機能を有効化（未設定なら全ルート404） |
| `KARRY_OTA_DIR` | 署名済み `.ipa` を1つ置くディレクトリ。`provisioned_devices.json`（`mobile_release.py` が出力）があればそれを、なければIPA内プロファイルの `ProvisionedDevices` を参照 |
| `KARRY_OTA_REGISTRY` | 登録UDIDの保存先JSON（既定 `data/ota_devices.json`、`memory` で非永続） |
| `KARRY_PUBLIC_BASE_URL` | 公開URL（省略時はリクエストの `Host` / `X-Forwarded-Proto` から判定）。HTTPS必須 |
| `KARRY_OTA_INVITE_CODE` | 任意。設定すると登録ページで招待コードを要求 |
| `KARRY_OTA_PROFILE_SIGNING_CERT` / `_KEY` | 任意。PEMを指定すると `openssl smime` でプロファイルに署名（未署名でも動作するがiOSに「未検証」と表示） |

管理者用: `GET /api/ota/devices`（オーナートークン）で登録UDIDと「現在のビルドに含まれるか」を一覧、`DELETE /api/ota/devices/{udid}` で削除。一覧のUDIDをApple Developer（Devices）に登録して再ビルドしてください。

## 安全上の扱い

- 受信エンドポイントはトークンで1回だけ使え、30分で失効。端末データをログや応答に出力しません。
- 取得するのはUDID・機種・iOSバージョンのみ。端末内データや他の識別子は要求しません。
- `itms-services` はHTTPSでのみ動作します。本番のTLS証明書、ドメイン、ホスティングは別途必要です。
- 構成プロファイルは登録後に削除できます（設定 → 一般 → VPNとデバイス管理）。

## 署名済みビルドの自動公開（GitHub Actions「OTA Karry-Agent 2」）

Apple の資格情報は GitHub Secrets（`APPLE_TEAM_ID` / `ASC_KEY_ID` / `ASC_ISSUER_ID` / `ASC_KEY_P8`）だけに置きます。
Actions で **OTA Karry-Agent 2** を「Run workflow」すると、macOS ランナーが次を無人で行います。

1. `/ota/` で登録済みの UDID（`KARRY_API_TOKEN` シークレットがある場合に `GET /api/ota/devices` から取得）と入力欄の UDID を、`scripts/asc_devices.py` で Apple Developer に端末登録（既知の端末はそのまま）。
2. `scripts/mobile_release.py --ios-mode adhoc --signing-mode automatic` で Ad Hoc 署名 IPA を作成（Xcode 自動署名がプロファイルを再生成し、登録端末を含めます）。
3. `karry-agent-2.ipa` / `provisioned_devices.json` / `build.json` を GitHub Release の固定タグ `ota-latest` に更新します。Render は再デプロイせず、`KARRY_OTA_RELEASE_BASE`（既定はその固定タグ）からメタデータとIPAを読みます。

新しい iPhone が `/ota/` で登録されるたびに、このワークフローをもう一度実行すれば、その端末も「インストール」に切り替わります。秘密鍵はランナーの一時ファイルにのみ書き、ビルド後に削除します。

### 登録と同時に自動実行する

Render に `KARRY_GITHUB_TOKEN`（対象リポジトリに Actions: write を持つ fine-grained PAT）を置くと、
`/api/register-udid` が UDID を受け取った直後にサーバーが同じワークフローを `workflow_dispatch` で起動します
（`KARRY_GITHUB_REPO` 既定 `kek46991-commits/karry-agent`、`KARRY_OTA_WORKFLOW` 既定 `ota-karry-agent-2.yml`）。
Render のディスクは再デプロイで消えるため、台帳に頼らずこの経路で UDID を GitHub 側へ渡します。
現在のビルドが既にその端末を含む場合は起動しません。トークンが無い場合は従来どおり手動で「Run workflow」します。
