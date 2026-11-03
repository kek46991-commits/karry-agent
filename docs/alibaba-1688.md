# 1688 商品リサーチ（URL / スクショ → 法的審査 → 価格計算）

karry-agent の FastAPI（Render の `karry-agent-1`）に `/api/alibaba/*` を追加しています。1688 の API キーは使いません。
画面と同じドメインで動くので、別のサーバーや CORS 用のプロキシは不要です。

```
画面 (static/index.html「🛒 1688 商品リサーチ」: 商品 URL ＋ 任意でスクショ最大4枚・原価)
  └─ POST /api/alibaba/evaluate | /publish  (Bearer KARRY_API_TOKEN)
       ├─ alibaba_scrape.intake
       │    ├─ URL: *.1688.com のページを 1 回 GET → タイトル / alicdn 画像 / 価格 / カテゴリー
       │    └─ スクショ: Gemini Vision (JSON) → 商品名 / カテゴリー / 原価 / 仕様 / 商品写真の範囲
       ├─ 法的審査  Gemini (JSON) ＋ キーワード規則 → 商標権 / 薬機法 / PSE を SAFE / REJECT
       └─ 価格計算  (原価 × 22 × 1.05 + 1,200) × 1.4 → SAFE なら /shop に出品
```

## データ取得（`src/karry_agent/alibaba_scrape.py`）

- **URL:** `https://*.1688.com` だけを読みます（商品 ID だけでも可）。リダイレクト先も毎回確かめるので、ほかのサイトには接続しません。
  - Cookie なし・再試行なしで 1 回だけ GET します（最大 3MB）。ログイン・スライダー認証・アクセス拒否の画面になったら `page_blocked` を返し、スクショを求めます。ボット対策の回避はしません。
  - 取り出すもの: `og:title` / `<title>`、`cbu01|img.alicdn.com/img/ibank/` の画像（最大 12 枚、サイズ指定は外す）、価格の数値（最安値を原価に使う）、カテゴリー名。
  - 商品名か価格が取れず `GEMINI_API_KEY` があるときは、ページの本文を Gemini で読みます。
- **スクショ:** JPG / PNG / WEBP、最大 4 枚、1 枚 8MB まで。サーバーで JPEG（最大 1600px）に変換してから Gemini Vision に渡します。
  - Gemini が返した商品写真の範囲を 1 枚目から切り抜き、ショップの画像にします（`/api/shop/images/<id>.jpg`）。
  - スクショの読み取りには `GEMINI_API_KEY` が必要です。ないときは `vision_unavailable`（503）です。
- URL とスクショの両方があるときは、ページで取れた値を優先し、足りない部分をスクショで埋めます。ページが止められたときはスクショだけで続けます。
- 原価が読み取れないときは、画面の「原価（元）」に入力すると、その値を使います。

## 法的審査

- 審査は次の 3 項目で、それぞれ SAFE か REJECT を返します: `trademark`（商標権）、`pharmaceutical`（薬機法）、`pse`（電気用品安全法）。
- Gemini（`GEMINI_API_KEY`、モデルは `KARRY_1688_MODEL`）とキーワード規則の両方で判定します。
  - どちらかが REJECT なら REJECT です（安全側に倒します）。
  - Gemini が使えないときは、規則だけで判定します（`engine: "rules"`）。
- 自動判定です。出品前に専門家の確認が必要です（`disclaimer`）。

## 価格計算（KERRY ショップの販売価格）

```
goods      = 原価(RMB) × 為替（既定 22 円）
agent_fee  = goods × 代行手数料率（既定 5%）
landed     = goods + agent_fee + 国際送料・関税の固定枠（既定 1,200 円）
price      = ceil(landed × 販売倍率（既定 1.4 ≒ 利益率 約 29%）)
```

- 例: 100 元 → 2,200 + 110 + 1,200 = 3,510 円 → 販売価格 4,914 円（利益 1,404 円）。
- 既定値は `KARRY_CNY_JPY_RATE` / `KARRY_1688_AGENT_FEE_RATE` / `KARRY_1688_SHIPPING_JPY` / `KARRY_SHOP_MARKUP` で変えられます。画面の入力欄で 1 回ごとに上書きすることもできます。
- 原価には、段階価格と SKU 価格のうち最も安いものを使います。「本番仕入れ＆自動出品」は、この価格で `/shop` に出品します。

## API（すべて `Authorization: Bearer $KARRY_API_TOKEN` が必要）

| Method | Path | 内容 |
| --- | --- | --- |
| GET | `/api/alibaba/config` | 読み取り方式・スクショの上限・価格の既定値（キーは返しません） |
| POST | `/api/alibaba/fetch` | `{"product": "<URL or ID>", "screenshots": ["data:image/png;base64,..."], "cost_cny": 9.8}` → 商品 JSON |
| POST | `/api/alibaba/evaluate` | 上と同じ ＋ `"pricing": {...}` → 商品・審査・価格 |
| POST | `/api/alibaba/publish` | 上と同じ → SAFE なら KERRY ショップに出品（201）、REJECT なら 409 |

## 本番（Render）

1688 のキーは要りません。スクショの読み取りと Gemini 審査には `GEMINI_API_KEY` を使います。main にマージすると自動でデプロイされます。

## 法務ダッシュボード（`GET /api/alibaba/logs`）

- 取得と審査を 1 回ごとに記録します。記録する項目は、時刻、商品 ID・タイトル、画像の枚数、SAFE / REJECT と各項目の理由、販売価格、またはエラーの種類（`page_blocked`（1688 が読み取りを拒否）、`vision_failed`、`product_unreadable` など）です。
- 最新 200 件をサーバーのメモリに持ちます（再起動で消えます）。サーバーのログにも 1 行ずつ出します。キーとトークンは記録しません。
- 画面の「⚖ 法務ダッシュボード」は、審査のたびと、パネルを開いているあいだ 15 秒ごとに更新します。

## KERRY ショップへの自動出品

- 法務ダッシュボードの「本番仕入れ＆自動出品」ボタンは `POST /api/alibaba/publish` を呼びます（オーナーのみ）。
  - 商品 URL / スクショを読み取り → 法的審査 → 価格計算の順に処理し、**SAFE で価格が出せたときだけ** `shop_store.ShopStore` に登録します。
  - 登録先は非公開バケットの `shop/catalog.json` です。同じ 1688 商品 ID（スクショだけのときは画像のハッシュ）なら、ショップ側の ID はそのままで情報を更新します。
  - REJECT のときは 409 を返し、出品しません。結果はどちらの場合もダッシュボードのログに残ります。
- 一般向けのショップ画面は `/shop`（`static/shop.html`）です。トークンなしで見られます。
  - 中身は、商品一覧、カテゴリー別のタブ、詳細モーダル（画像最大 12 枚のサムネイル切り替え、仕様、バリエーション）、購入申し込みフォームです。
  - 商品一覧は 30 秒ごとに更新します。
- 公開 API:
  - `GET /api/shop/products`: 原価や審査の内部情報は返しません。
  - `POST /api/shop/orders`: 名前・連絡先・数量を受け付けます。決済はしません。
- オーナー用 API: `GET /api/shop/admin/orders`、`DELETE /api/shop/admin/products/{id}`（出品の取り下げ）。
