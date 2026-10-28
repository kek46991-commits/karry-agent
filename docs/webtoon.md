# 小説→縦読みマンガ（Webtoon）パイプライン（実験機能）

`src/karry_agent/webtoon/` は、小説の本文から 9:16 の縦読みマンガのコマ画像（`panel_001.png` …）を作ります。

| モジュール | 役割 |
| --- | --- |
| `script_parser.py` | 物語ごとに登場人物（顔・髪・服・小物・眼鏡の有無）とコマ割りを抽出。Gemini の構造化出力を使い、失敗時は決定的なパーサー（「」＝セリフ、（）＝心の声、！＝叫び、小声・……＝ささやき、地の文＝ナレーション）へ切り替え |
| `character_manager.py` | 固定のキャラクターシートを全コマのプロンプトの先頭に付ける（`glasses: ON/OFF` を明記）。`is_consistent` で全プロンプトが同じシートで始まるかを検証 |
| `bubble_renderer.py` | 画像AIに文字を書かせず、Pillow で吹き出しを描く。`normal` 楕円＋しっぽ、`thought` 雲、`shout` ギザギザ、`whisper` 点線、`narration` 半透明の帯 |
| `publisher.py` | 1080×1920（9:16）に中央トリミングし、連番 PNG・`manifest.json`・ZIP を出力 |
| `pipeline.py` | 上の4つをつなぐ。絵は Gemini（`GEMINI_IMAGE_MODEL`、`ImageConfig(aspect_ratio="9:16")`）で生成し、キーが無い・失敗したコマは、同じキャラクターシートから描く簡易絵コンテに切り替え |

**配置の決め方**: 画像を 3×6 のマスに分けて輪郭の多さを測り、ナレーションは上下の帯のうち絵が少ない方へ置きます。吹き出しは左右の列の空いたマスにだけ置き、顔のある中央の列には置きません。

**フォント**: `KARRY_WEBTOON_FONT` → システムの Noto CJK → 固定版の Noto Sans JP（SHA-256 検証付きでダウンロードし `~/.cache/karry-agent/fonts` に保存）の順に探します。見つからなければ `font_unavailable` を返します（文字化けした画像は作りません）。

## チャットから使う

`KARRY_WEBTOON=1`（マンガだけを公開）または `KARRY_EXPERIMENTAL_FEATURES=1` のとき、チャットで1行目に「この小説をマンガにして」、2行目以降に本文（12,000文字まで）を送るとジョブを開始します（`src/karry_agent/chat_webtoon.py`）。「4コマで」のようにコマ数も指定できます（1〜12、既定6）。返答の `マンガジョブID: <id>` を画面が見つけて進み具合を表示し、完成したコマ画像と「ZIPを保存」ボタンをチャット内に出します。

## API（所有者トークン必須・`KARRY_WEBTOON=1` または `KARRY_EXPERIMENTAL_FEATURES=1` のときだけ有効）

- `POST /api/webtoon/jobs` `{"novel_text": "...", "max_panels": 6}` → 202
- `GET /api/webtoon/jobs/{job_id}` → 状態・登場人物・各コマの吹き出し配置・`consistent_prompts`
- `GET /api/webtoon/jobs/{job_id}/panels/{n}` → PNG
- `GET /api/webtoon/jobs/{job_id}/archive` → `panel_001.png`… と `manifest.json` の ZIP

ジョブはプロセス内メモリに最大10件まで保存します（再起動で消えます）。
