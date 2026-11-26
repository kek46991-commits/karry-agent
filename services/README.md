# services/

このリポジトリは「中枢（ジミーコア）」と「現場機能」を分けて運用します。

| 区分 | 場所 | デプロイ | 役割 |
| --- | --- | --- | --- |
| 中枢 | [`jimmy-core/`](jimmy-core/) | Render `jimmy-core`（Root Directory = `services/jimmy-core`） | 記憶・状態・思考・指示出し |
| 現場（既存） | リポジトリ直下（`app.py`, `src/karry_agent/`） | Render `karry-agent-1` ほか | 下表の現場機能をまとめて提供 |

ジミーコアは `karry_agent` を import しません（`tests/test_api.py::test_core_does_not_import_field_modules` で検査）。
現場機能は `POST /dispatch` の宛先（`JIMMY_AGENT_<NAME>_URL`）として呼び出します。

## 現場機能の分離マップ

将来それぞれ別サービス・別リポジトリへ切り出すときは、下表のファイルをまとめて移します
（例: `git subtree split` / `git filter-repo --path ...` で履歴ごと移動）。

| エージェント名 | 分離対象 | 主なファイル |
| --- | --- | --- |
| `shop` | A: ECショップ（Keli Shop、Stripe 決済、商品管理） | `src/karry_agent/shop_api.py`, `shop_store.py`, `stripe_checkout.py`, `chat_product.py`, `product_images.py`, `alibaba1688.py`, `alibaba_api.py`, `alibaba_scrape.py`, `metaverse_assets.py`, `static/shop.html` |
| `video` | B: 動画・メディア制作（リップシンク、動画生成パイプライン） | `src/karry_agent/video_suite.py`, `video_suite_api.py`, `video_projects.py`, `video_edit.py`, `video_edit_api.py`, `chat_video.py`, `lipsync.py`, `lipsync_remote.py`, `sadtalker.py`, `sadtalker_worker.py`, `media_generation.py`, `media_repository.py`, `image_generation.py`, `chat_webtoon.py`, `webtoon/`, `.github/workflows/video-edit.yml` |
| `device` | C: 端末監視・遠隔操作（iPhone/Android 画面共有、ReplayKit、自動操作） | `src/karry_agent/mobile_agents.py`, `ota_enrollment.py`, `autonomy/`, `device_app.py`, `ios/`, `android/`, `android-observer/`, `ota/`, `.github/workflows/ios.yml`, `ota-karry-agent-2.yml` |
| `killswitch` | 安全装置 | 未実装（`JIMMY_AGENT_KILLSWITCH_URL` を登録すると、キルスイッチ作動中でも呼び出せる唯一の宛先になる） |

既存の `karry-agent-1` は本番（ポートフォリオ）として動いているため、現場コードの物理的な移動は
各現場エージェントを別 URL で立ち上げるタイミングで 1 つずつ行います。
