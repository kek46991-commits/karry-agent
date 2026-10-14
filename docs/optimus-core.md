# Optimus Core（自己進化・クローン生成API）

karry-agent（第1世代）が、自分のクローン「ケリーエージェント」（第2世代以降）を生成し、自分のコードの変更案を GitHub の PR として出して、承認と CI 成功のあとに Render へ自動デプロイするための API です。実装は `src/karry_agent/optimus_core.py`、エンドポイントは `app.py` にあります。

すべてのエンドポイントは所有者の `Authorization: Bearer $KARRY_API_TOKEN` が必要です。`KARRY_API_TOKEN` が未設定のときは 503 を返します（fail closed）。

## クローン（ケリーエージェント）

| メソッド | パス | 内容 |
| --- | --- | --- |
| POST | `/api/optimus/clones` | `{"mission": "...", "specialist": "general"}` でクローンを生成します。名前は `ケリーエージェント2号`、`3号`… の連番です |
| GET | `/api/optimus/clones` | クローン一覧を返します |
| POST | `/api/optimus/clones/{clone_id}/run` | `{"prompt": "..."}` を、ミッションとペルソナ付きでクローンに実行させます |
| DELETE | `/api/optimus/clones/{clone_id}` | クローンを削除します |

- クローンは同じサーバープロセス内のエージェントです（既存の `AgentCore` と Gemini 設定を共有します）。メモリ上にだけ存在するため、再起動・再デプロイで消えます。
- 上限は `KARRY_OPTIMUS_MAX_CLONES`（既定 5、最大 20）です。1つのクローンは同時に1件だけ実行します。
- クローンには「自分のクローン生成・コード書き換え・デプロイはできない」と指示しています。Optimus の API はモデルのツールとして登録していないため、クローンが自己増殖することはありません。
- Render に別サービスとして複製する機能（課金が発生します）は、この版には含めていません。

## 自己進化（コード書き換え → PR → 自動デプロイ）

```
POST /api/optimus/evolutions           変更案のプレビュー（GitHub への書き込みなし）
POST /api/optimus/evolutions/confirm   SHA-256 を確認して optimus/* ブランチと PR を作成
GET  /api/optimus/evolutions/{pr}      PR と CI の状態
POST /api/optimus/evolutions/{pr}/deploy  CI が全て成功していればマージ → Render が自動デプロイ
GET  /api/optimus/status               設定状況・実行中コミット（RENDER_GIT_COMMIT）
```

1. **変更案**: `{"goal": "..."}` だけを送ると、Gemini（ツール無効）が1〜3個の exact old/new 編集を生成します。`edits` を直接渡すこともできます。`paths` を指定すると、送るファイルを許可リストの中から絞れます。
2. **検証**: 既存の Self-Healing と同じ `validated_changes` で、次を確認します。
   - 許可リスト内の既存 `.py` ファイルであること
   - old が1回だけ一致すること
   - 変更後のコードが構文として正しいこと
   - 秘密値が含まれていないこと
   検証を通ると unified diff と SHA-256 を返します。本番の checkout は変更しません。案の有効期限は15分で、確認待ちは最大10件です。
3. **承認**: 所有者が `approval_id` と `sha256` を送ります。一致したときだけ、GitHub API でブランチ `optimus/<id>` を main から作ってコミットし、PR を作成します。このとき、リモートの main が手元のソースと違う場合は中止します。`confirmed:false` を送ると、GitHub には触れずに取り消します。
4. **デプロイ**: `KARRY_OPTIMUS_AUTO_MERGE=1` のときだけ実行でき、所有者が `{"confirmed": true}` を送る必要があります。次の条件をすべて満たした場合に限り、head SHA を固定して squash マージします。
   - PR の head ブランチが `optimus/*` で、同じリポジトリのものであること（fork は不可）
   - base ブランチが main であること
   - PR が open で、GitHub が mergeable と判定していること
   - 変更ファイルが全て許可リスト内であること
   - CI のチェックが全て成功していること（チェックが0件のときは「待機中」として扱います）
   マージ後は、Render の `autoDeploy` が main をデプロイします。反映されたかは `/api/optimus/status` の `running_commit` で確認できます。

### 書き換えの範囲

- 既定の許可リストは `src/karry_agent/core.py`、`tools.py`、`orchestrator.py`、`url_analysis.py`、`memory.py` です。`KARRY_OPTIMUS_PATHS`（カンマ区切り）で変更できます。
- 次のファイルは、設定に関係なく常に書き換え禁止です。
  - `app.py`（認証）
  - `optimus_core.py`、`repair_pipeline.py`、`self_healing.py`、`healing_client.py`
  - `privacy.py`、`config.py`、`calendar_secrets.py`、`google_workspace.py`
  - `website_storage.py`、`task_persistence.py`、`agent_core.py`
  - `.github/`、`tests/`、`render.yaml`、`Dockerfile`、依存ファイルなどの `.py` 以外のファイル
- AI による生成は、1プロセスあたり `KARRY_OPTIMUS_MAX_GENERATIONS`（既定10回）までです。

## Render の設定

| 環境変数 | 内容 |
| --- | --- |
| `KARRY_OPTIMUS_GITHUB_TOKEN` | fine-grained PAT。対象は `kek46991-commits/karry-agent` のみ。権限は Contents: Read and write、Pull requests: Read and write、Checks: Read-only |
| `KARRY_OPTIMUS_GITHUB_REPO` | 既定 `kek46991-commits/karry-agent` |
| `KARRY_OPTIMUS_AUTO_MERGE` | `1` で deploy（マージ）を有効化。未設定なら PR 作成までで止まります |
| `KARRY_OPTIMUS_BASE_BRANCH` | 既定 `main` |

本番サービス `karry-agent-1` に設定してください。トークンは API レスポンスにもログにも出しません。

## 使用例

```bash
H="Authorization: Bearer $KARRY_API_TOKEN"; U=https://karry-agent-1.onrender.com
curl -s -H "$H" -H 'Content-Type: application/json' -d '{"mission":"競合調査担当"}' $U/api/optimus/clones
curl -s -H "$H" -H 'Content-Type: application/json' -d '{"goal":"get_current_datetime の説明文を分かりやすくする","paths":["src/karry_agent/tools.py"]}' $U/api/optimus/evolutions
# 返ってきた diff を確認してから
curl -s -H "$H" -H 'Content-Type: application/json' -d '{"approval_id":"...","sha256":"...","confirmed":true}' $U/api/optimus/evolutions/confirm
curl -s -H "$H" $U/api/optimus/evolutions/<PR番号>
curl -s -H "$H" -H 'Content-Type: application/json' -d '{"confirmed":true}' $U/api/optimus/evolutions/<PR番号>/deploy
```
