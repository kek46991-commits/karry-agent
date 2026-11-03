# 法務・AIダッシュボード（1688 URL ＋ 原価 → 法的審査 → 価格計算 → 下書き → 登録）

```
/admin（static/admin.html・管理者ログイン必須）
  1688 商品 URL ＋ 原価（元）＋ 商品名・カテゴリー（任意）＋ 為替 / 送料 / 代行手数料 / 倍率
  └─ POST /api/alibaba/evaluate
       ├─ alibaba_scrape.intake: 商品ページを 1 回だけ GET（任意・失敗しても原価があれば続行）
       ├─ alibaba1688.review_product: Gemini ＋ キーワード規則で SAFE / REJECT
       ├─ alibaba1688.price_product: 日本円の販売価格
       └─ ShopStore.save_draft: 非公開の下書き（shop/drafts.json）
  └─ POST /api/alibaba/publish {"draft_id"}: SAFE・価格あり・商品名ありのときだけ /shop に掲載
```

## 管理者認証

- `/admin` はログイン画面だけを返します。管理者トークン（`KARRY_API_TOKEN`）を `GET /api/admin/me` で確かめてから、ダッシュボードを表示します。
- トークンはタブの sessionStorage にだけ置きます（タブを閉じると消えます）。401 が返ったらログイン画面に戻ります。
- 管理 API（`/api/alibaba/*`、`/api/shop/admin/*`、`/api/admin/me`）はすべて Bearer トークンが必要です。`KARRY_API_TOKEN` が未設定なら 503 で閉じます。`/admin` は `noindex` です。

## 商品ページの読み取り

- `https://*.1688.com` だけを読みます（リダイレクト先も確認します）。Cookie なし・再試行なしで 1 回だけ GET します。ログイン・スライダー認証などの画面になったときは、回避せず「読めなかった」とします。
- 原価（元）を入れていれば、読めなくてもエラーにせず、入力した値で下書きを作ります。商品名が読めず入力もないときは仮の名前にし、登録はできません（商品名を入れて計算し直します）。
- スクショの自動読み取り（OCR / Gemini Vision）はありません。

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
- 原価には、段階価格と SKU 価格のうち最も安いものを使います。「ショップに登録」は、この価格で `/shop` に掲載します。

## API（すべて `Authorization: Bearer $KARRY_API_TOKEN` が必要）

| Method | Path | 内容 |
| --- | --- | --- |
| GET | `/api/admin/me` | 管理者トークンの確認 `{"admin": true}` |
| GET | `/api/alibaba/config` | 価格計算の既定値 |
| POST | `/api/alibaba/fetch` | `{"product": "<URL or ID>", "cost_cny": 100, "title": "", "category": ""}` → 商品情報 |
| POST | `/api/alibaba/evaluate` | 上の内容 ＋ `pricing` → 審査・価格・下書き（`draft`） |
| GET / DELETE | `/api/alibaba/drafts`、`/api/alibaba/drafts/{id}` | 下書きの一覧・削除 |
| POST | `/api/alibaba/publish` | `{"draft_id": "..."}` → SAFE なら 201 で掲載、それ以外は 409 と理由 |
| GET | `/api/alibaba/logs` | 審査ログ |
| GET / DELETE | `/api/shop/admin/products`、`/api/shop/admin/products/{id}` | 掲載中の商品・掲載停止 |
| GET | `/api/shop/admin/orders` | 注文 |

## 審査ログ（`GET /api/alibaba/logs`）

- 審査・登録を 1 回ごとに記録します（時刻、商品 ID・タイトル、SAFE / REJECT と各項目の理由、販売価格、エラーの種類）。最新 200 件をメモリに持ち、再起動で消えます。キーとトークンは記録しません。

## KERRY ショップ（公開）

- `/shop`（`static/shop.html`）はトークンなしで見られます。商品一覧、カテゴリー別のタブ、詳細モーダル、購入申し込みフォームです。
- `GET /api/shop/products` は掲載中の商品だけを返し、原価・審査の内部情報・下書きは返しません。`POST /api/shop/orders` は申し込みの受け付けだけで、決済はしません。
