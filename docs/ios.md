# iPhone版 Karry 監視用 / 作業用

Android版（`android/`）と同じ2つのアプリを iPhone 向けに作るための手順です。

| アプリ | 開く画面 | できること |
| --- | --- | --- |
| Karry 監視用 | `/monitor` | 作業用の状態・画面・監査ログを見る、一時停止・停止・再開 |
| Karry 作業用 | `/companion` | 初回登録、タスク状況、**iPhone全体の画面共有**（ReplayKit） |

## 仕組み

- どちらのアプリも `WKWebView` でサーバーの画面を開きます。外部サイトは Safari で開きます。
- 作業用アプリは Android版と同じ `window.KarryNative`（`isSharing` / `startScreenShare` / `stopScreenShare`）を提供します。そのため、`static/companion.html` は変更していません。
- 画面共有は Broadcast Upload Extension（`ios/Broadcast/SampleHandler.swift`）が行います。約3秒ごとに幅720pxのJPEGを `/api/autonomy/device/frame` へ、約15秒ごとにハートビートを送ります。
- アプリと拡張機能は、App Group の共有キーチェーンを通して端末トークン（`karry_device` Cookie の値）を受け渡します。拡張機能は `X-Device-Token` ヘッダーで認証します。
- 監視用で端末の登録を解除すると、サーバーが 401 を返し、共有は自動で止まります。
- 共有を始めるたびに、iOS の「画面収録」の確認で「ブロードキャストを開始」を押す必要があります。iOS の決まりなので、省略できません。共有中は、画面上部に赤い表示が出ます。止めるには、アプリの「共有をやめる」、赤い表示、コントロールセンターのどれかを使います。

## ビルド（Mac）

1. Xcode 16 以降と [XcodeGen](https://github.com/yonaskolb/XcodeGen) を入れます（`brew install xcodegen`）。
2. `cd ios && xcodegen generate` を実行し、`KarryDevices.xcodeproj` を開きます。
3. 3つのターゲット（KarryMonitor / KarryWorker / KarryWorkerBroadcast）の Signing & Capabilities で Team を選びます。バンドルIDが使えない場合は、Build Settings の `KARRY_BUNDLE_PREFIX`（初期値 `com.kek46991.karry`）を自分用に変えます。App Group は `group.<prefix>.worker` です。
4. iPhone を USB でつなぎ、スキームを選んで実行します。初回だけ、iPhone の「設定 → 一般 → VPNとデバイス管理」で開発者を信頼します。

接続先は Build Settings の `KARRY_SERVER_URL`（初期値 `https://karry-agent-1.onrender.com`）で変えられます。

## 配布（TestFlight）

Apple Developer Program（有料）が必要です。

1. App Store Connect で、`<prefix>.monitor` と `<prefix>.worker` の2つのアプリを作ります。
2. App Store Connect API キーを「Admin」の権限で作ります（署名用の証明書を自動で作るために必要です）。
3. GitHub の Secrets に、次の4つを登録します。値はチャットに貼らないでください。
   - `APPLE_TEAM_ID`
   - `ASC_KEY_ID`
   - `ASC_ISSUER_ID`
   - `ASC_KEY_P8`（ダウンロードした `.p8` ファイルの中身を、そのまま全部貼る）
4. Actions の **iOS apps** を「Run workflow」で実行し、`testflight` にチェックを入れます。
5. 使う人の iPhone に TestFlight を入れ、招待メールから2つのアプリを入れます。

会社の端末をまとめて管理する場合は、Apple Business Manager と MDM で配る方法もあります。

## CI

`.github/workflows/ios.yml` は、PR ごとにシミュレーター用と署名なしの実機用をビルドします。署名なしの `.ipa` は、成果物（artifact）としてダウンロードできます。署名なしのままでは、iPhone に入れられません。
