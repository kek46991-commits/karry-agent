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
