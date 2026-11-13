# 自律実行（タスク分割・状態保存・分岐統合）と見守り端末連携

`src/karry_agent/autonomy/` と `/api/autonomy/*` を追加しました。実験機能のため、有効になるのは `KARRY_EXPERIMENTAL_FEATURES=1` のときだけです（本番の既定は 404）。

## 流れ

```text
指示 ──plan_subtasks──▶ t1 t2 t3 …（各サブタスクは budget_tokens 以下）
        │ 箇条書きは 1 項目＝1 サブタスク。長すぎる項目は文単位、それでも長ければ文字単位で分割
        │ 「それをもとに」「上記を使って」などは直前の項目に依存させる（依存は後ろ向きだけなので循環しない）
        │ 種類で振り分け: video / image / research / general
        ▼
AutonomousRunner ── 依存が揃ったものから最大 max_workers 個を並列実行（分岐）
        │ 実行前: 見守りゲートが allow / deny / hold を判定
        │ 状態は遷移ごとに JSON へ保存（running にしてから実行するので、落ちても再開時に分かる）
        ▼
default_merge（または merger）── 完了した結果を t1, t2 … の順に 1 つの最終出力へまとめる
```

| ハンドラ | 処理 |
| --- | --- |
| `general` / `research` | 既存の `AgentCore.run`（全体の目的と依存先の結果を前置） |
| `image` / `video` | 既存のタスクチャット経路 `_execute`（`session_id` 必須）。画像生成・動画オートパイロット・保存・チャット表示は従来どおり |

## ループ・セッション切れ対策

| 仕組み | 既定 |
| --- | --- |
| サブタスクごとの試行回数 `max_attempts`（再試行は 1 回まで） | 2 |
| 実行全体の上限（サブタスク数 × `max_attempts`） | — |
| クラッシュ後の再開回数 `max_resumes`（`running` のまま残った実行を再開した回数） | 5 |
| 制限時間 `deadline_seconds`（超えたら `paused`。再開できる） | 1800 秒 |
| 空の結果は失敗として扱う／同じ内容のサブタスクは計画時に 1 つにまとめる | — |

再開は `POST /api/autonomy/runs/{id}/resume`。完了済みのサブタスクは保存された結果を使い、再実行しません。プロセス再起動後の自動再開は行いません（安全のため、所有者か見守り端末が明示的に再開します）。

## 保存先

| 変数 | 内容 |
| --- | --- |
| `KARRY_AUTONOMY_STORAGE` | `file`（既定）または `memory` |
| `KARRY_AUTONOMY_DIR` | 保存フォルダ（既定 `data/autonomy/`、Git 管理外）。実行ごとに `<id>.json`（状態）と `<id>.audit.jsonl`（監査ログ）を権限 0600 で作成 |

Render のディスクは再デプロイで消えます。本番で長期保存する場合は永続ディスクを `KARRY_AUTONOMY_DIR` に割り当ててください。保存は 1 プロセス前提です。

## 見守り端末（検閲エージェント）との連携

見守り端末そのものはこのアプリに含みません。アプリ側にあるのは、監査ログを出すことと、見守り側の判定・信号に従うことだけです。

| 方向 | 方式 | 設定 |
| --- | --- | --- |
| アプリ → 見守り | 全イベントを `POST {URL}/events` へ送信（送れなくても実行は止めない。見守り側は監査ログを取得して補える） | `KARRY_MONITOR_URL`（https 必須。ローカル開発のみ `http://localhost`） |
| アプリ → 見守り | サブタスクの実行前に `POST {URL}/review` を呼び、`{"verdict": "allow"\|"deny"\|"hold", "reason": "..."}` を受け取る | `KARRY_MONITOR_MODE=enforce` |
| 署名 | 送信本文に `X-Karry-Signature: sha256=<HMAC>` を付け、監査ログの各行にも `sig` を付ける | `KARRY_MONITOR_SECRET` |
| 見守り → アプリ | `GET /api/autonomy/runs/{id}/audit?after=N`（`verified` でハッシュ連鎖と署名を検証）／`POST /api/autonomy/runs/{id}/signal` `{"action": "pause"\|"stop"\|"resume"}` | ヘッダー `X-Monitor-Token: <KARRY_MONITOR_TOKEN>`。このトークンでは監査ログの取得と信号の送信しかできず、実行の作成や結果の閲覧はできない |
| 検閲ファイル | 実行前に毎回読み込む JSON。見守り端末が差し替えれば即座に反映される | `KARRY_MONITOR_POLICY_FILE` |

検閲ファイルの例:

```json
{"halt": false, "reason": "", "blocked_kinds": ["video"], "blocked_patterns": ["rm\\s+-rf", "DROP\\s+TABLE"]}
```

判定の優先順位は「停止・一時停止の信号 → 設定エラー → 検閲ファイル → 見守り端末の判定」です。各判定の結果は次のとおりです。

| 判定 | 結果 |
| --- | --- |
| `deny` | そのサブタスクを失敗にする |
| `hold` | 実行全体を `paused` にする |

検閲ファイルや見守り端末を設定しているのに読めない・つながらない場合は `hold` になります（フェイルクローズ）。

監査ログは `prev`（前の行のハッシュ）と `hash = sha256(prev + 行の正規化JSON)` で連鎖しているので、途中の行を書き換えると検証に失敗します。行の中の秘密情報はマスクしてから保存します。

## API（所有者は `Authorization: Bearer <KARRY_API_TOKEN>`）

| メソッド | パス | 内容 |
| --- | --- | --- |
| POST | `/api/autonomy/runs` | `{"instruction", "session_id"?, "budget_tokens"?, "dry_run"?}`。`dry_run` は分割結果だけを返す。それ以外は計画を保存して裏で実行し、202 を返す |
| GET | `/api/autonomy/runs` / `/runs/{id}` | 一覧・詳細（各サブタスクの状態と最終出力） |
| POST | `/api/autonomy/runs/{id}/resume` | 中断・一時停止した実行を再開する |
| POST | `/api/autonomy/runs/{id}/signal` | 所有者または見守り端末が pause / stop / resume を送る |
| GET | `/api/autonomy/runs/{id}/audit` | 所有者または見守り端末が監査ログを取得する |
| GET | `/api/autonomy/monitor` | 見守り設定の有無（秘密値は返さない） |

## 2台構成（作業端末＋見守り端末）

会社が管理する端末の第1フェーズでは、司令塔を入れず、作業端末と見守り端末だけをつなぎます。

| 画面 | URL | できること |
| --- | --- | --- |
| 見守り端末 | `/monitor` | 作業端末の接続状態（最終応答・電池・画面が表か裏か）、共有中の画面プレビュー、タスクの進み具合と最終出力、監査ログ（改ざん検証付き）、一時停止・再開・停止、端末の登録解除 |
| 作業端末 | `/companion` | 15 秒ごとに状態を送信、タスクの状況表示、本人が押したときだけ画面共有（共有中は赤い帯を常時表示し、いつでもやめられる） |

- **初回設定は1回だけ：** URL を開き、パスワード（`KARRY_DEVICE_ENROLL_PASSWORD`）を入れて「利用開始」を押すと端末登録が終わります。
  - 端末には HttpOnly Cookie（約 400 日）が保存されます。サーバー側には SHA-256 ハッシュだけを `KARRY_AUTONOMY_DIR/devices.json`（0600）に保存します。
  - 書き込み系の API には `X-Karry-Device: 1` ヘッダーが必須なので、他サイトからの不正な送信（CSRF）は通りません。
  - パスワードの失敗が続くと、一時的にロックします。
  - 登録できる端末は最大 20 台です。
- **ホーム画面に追加：** 両画面とも Web アプリのマニフェストと Service Worker を持っているので、「ホーム画面に追加」で普通のアプリのように開けます。
- **権限：**
  - 作業端末は、自分の状態と画面の送信、タスク概要の閲覧だけができます。
  - 見守り端末は、閲覧と停止系の操作ができます。タスクの新規作成はどちらの端末からもできません（所有者トークンが必要）。
  - 見守り端末から作業端末を遠隔操作する機能はありません。
- **画面共有の制限：**
  - ブラウザの画面共有（`getDisplayMedia`）を使うため、PC の Chrome / Edge などでは他のアプリの画面も共有できます。
  - Android / iPhone のブラウザは画面共有に対応していないので、状態とタスクの共有だけになります。
  - スマホの画面ミラーリングには、Android の画面キャプチャ権限（MediaProjection）を使う専用アプリが必要で、次のフェーズで追加します。
  - プレビューは最新 1 枚だけをメモリに保持し（JPEG / PNG / WebP、600KB まで）、共有をやめると消えます。

| 変数 | 内容 |
| --- | --- |
| `KARRY_DEVICE_ENROLL_PASSWORD` | 端末登録用のパスワード。未設定なら登録 API は 503 |
