# 1688 商品リサーチ（Open API → 法的審査 → 価格計算）

karry-agent の FastAPI（Render の `karry-agent-1`）に `/api/alibaba/*` を追加しています。画面と同じドメインで動くので、
別の Node/Express サーバーや CORS 用のリバースプロキシは不要です（`Dockerfile` / `render.yaml` の起動コマンドはそのまま）。

```
画面 (static/index.html「🛒 1688 商品リサーチ」)
  └─ POST /api/alibaba/evaluate  (Bearer KARRY_API_TOKEN)
       ├─ 1688 Open API  alibaba.cross.productInfo  (HMAC-SHA1 署名, App Key/Secret + access_token)
       ├─ 法的審査  Gemini (JSON) ＋ キーワード規則 → 商標権 / 薬機法 / PSE を SAFE / REJECT
       └─ 価格計算  人民元原価 → 日本円の販売価格・利益
```

## データ取得

- 1688 の公式 Open API だけを使います。ページのスクレイピングやボット対策の回避はしません。
- API: `com.alibaba.product:alibaba.cross.productInfo:1`
  - URL: `https://gw.open.1688.com/openapi/param2/1/com.alibaba.product/alibaba.cross.productInfo/{appKey}`
  - 引数は `productId` です。
- 署名は公式 Python SDK（`aop/api/base.py`）と同じ方式です: `HMAC-SHA1(appSecret, urlPath + sorted(key+value))` を大文字の 16 進で `_aop_signature` に入れます。
- 跨境（クロスボーダー）の铺货関係がない商品は、`product_forbidden`（403）になります。1688 側で「一键铺货」してください。
- 画像は `https://cbu01.alicdn.com/` の絶対 URL にそろえます。返すのは、取得した商品の画像（最大 12 枚）だけです。

## 法的審査

- 審査は次の 3 項目で、それぞれ SAFE か REJECT を返します: `trademark`（商標権）、`pharmaceutical`（薬機法）、`pse`（電気用品安全法）。
- Gemini（`GEMINI_API_KEY`、モデルは `KARRY_1688_MODEL`）とキーワード規則の両方で判定します。
  - どちらかが REJECT なら REJECT です（安全側に倒します）。
  - Gemini が使えないときは、規則だけで判定します（`engine: "rules"`）。
- 自動判定です。出品前に専門家の確認が必要です（`disclaimer`）。

## 価格計算

```
goods   = 原価(CNY) × 為替 × (1 + 代行手数料率)
duty    = (goods + 国際送料) × 関税・消費税率
landed  = goods + 国際送料 + duty
price   = ceil(landed / (1 − 販売手数料率 − 目標利益率) / 10) × 10
```

- 既定値は `KARRY_CNY_JPY_RATE` などの環境変数で決めます。`pricing` を送ると、その 1 回だけ上書きできます。
- 原価には、段階価格と SKU 価格のうち最も安いものを使います。

## API（すべて `Authorization: Bearer $KARRY_API_TOKEN` が必要）

| Method | Path | 内容 |
| --- | --- | --- |
| GET | `/api/alibaba/config` | 設定済みかどうかと価格の既定値（キーは返しません） |
| POST | `/api/alibaba/fetch` | `{"product": "<URL or ID>"}` → 商品 JSON |
| POST | `/api/alibaba/evaluate` | `{"product": "...", "pricing": {...}}` → 商品・審査・価格 |

## 本番（Render）

`karry-agent-1` の Environment に `KARRY_1688_APP_KEY` / `KARRY_1688_APP_SECRET` / `KARRY_1688_ACCESS_TOKEN` を入れてください（価格の既定値は任意）。
main にマージすると自動でデプロイされます。

## キー未設定時の表示と、設定後の切り替え

- 認証情報は、リクエストのたびに環境変数から読み直します（`client_from_env` / `missing_env`）。
  - Render で 3 つのキーを保存すると再デプロイされ、その直後から公式 API への実際の通信になります。コードの変更は要りません。
  - モックは本番のコードにはありません。テストにだけあります。
- `GET /api/alibaba/config` は `missing` を返します。中身は未設定の変数名だけで、値は返しません。
- キーが未設定のあいだ、画面には次を表示し、「取得して審査・価格計算」ボタンは押せなくします。
  - 「⚠ 1688 API認証キー未設定：Renderの環境変数を設定してください」の警告
  - 設定手順（open.1688.com → 認可 → Render の Environment → 「設定を再確認」）

## 法務ダッシュボード（`GET /api/alibaba/logs`）

- 取得と審査を 1 回ごとに記録します。記録する項目は、時刻、商品 ID・タイトル、画像の枚数、SAFE / REJECT と各項目の理由、販売価格、またはエラーの種類（`product_forbidden`（一键铺货が未許可）、`alibaba_auth`、`alibaba_unavailable` など）です。
- 最新 200 件をサーバーのメモリに持ちます（再起動で消えます）。サーバーのログにも 1 行ずつ出します。キーとトークンは記録しません。
- 画面の「⚖ 法務ダッシュボード」は、審査のたびと、パネルを開いているあいだ 15 秒ごとに更新します。
