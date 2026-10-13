# Self-Healing Core

提供された `SelfHealingEngine` を土台に、Karry-agentの共通エラー処理を実装しました。個別のPhotos、Maps、Calendar APIの修正は行わず、既存コアとAPIの捕捉経路を一つのエンジンに接続しています。

## 動作

例外が発生すると、例外型、秘密情報を除いたメッセージ、ファイル名・行番号・関数名のスタックフレームを記録します。スタックトレースは例外自身の `__traceback__` から取得するため、`except` の外でも取得できます。フレームのソース行、ローカル変数、入力プロンプト、リクエストbody、ヘッダー、URLクエリを診断ログへ保存しません。

ログはプロセス内の最大100件です。同じ例外型・分類・発生箇所の修復は既定60秒間抑制し、同じ修復処理から再び修復エンジンを起動する再帰も抑止します。ログは永続ストレージではなく、再起動で失われます。複数プロセス・複数サービス間で共有されません。

FastAPIの未捕捉例外、401と5xx、入力検証失敗、SSE実行の失敗、Geminiの失敗と再試行、ツール例外と `status:error` / `error` 戻り値、確認後のツール失敗を共通エンジンへ送ります。捕捉済みの構造化エラーには元例外がないため、スタックトレースを捏造せず空配列として記録します。

全てのHTTPエラーを500へ変換することはありません。受信401は401、入力不正は422、通常の404は404のままです。失敗したAPIは成功の200へすり替えません。既存のツールAPIはHTTP 200内で `result.status:error` を返すことがあり、引き続き内部状態を確認してください。

## 実際の修復

登録式の `RepairStrategy` は `matches`、`apply`、`verify`、任意の `rollback` を持ちます。修正後の検証がTrueになった場合に限って `status: repaired, verified: true` と記録します。修正関数が成功しただけでは修復完了としません。失敗時は可能ならロールバックし、元の障害を新たな修復例外で隠しません。

既定の実修復戦略は、`FileNotFoundError.filename` が明示設定済み `KARRY_WORKSPACE` と一致した場合の作業ディレクトリ復元です。既存親ディレクトリ内にmode 700で作成し、読み書き可能であることを確認します。任意の不足ファイルや親階層は作成しません。修復したディレクトリを検証できない場合、空である場合だけ削除して戻します。この修復は環境の回復であり、失敗済みのツールの成功を意味しません。

Geminiについては既存の回数上限付き再試行を維持しています。後続呼び出しが有効なモデル応答を返したとき、そのインシデントを `gemini_retry_recovered` として記録します。再試行上限到達や空のモデル応答は修復完了にはしません。

## 認証ヘッダー

受信した不正なBearer tokenを、サーバー内の正解tokenへ置き換えてログインさせることはしません。401を維持し、呼び出し元の認証設定が必要という診断を返します。認証値の変更・再発行・Render Secretの変更は行いません。

送信側で使う `HealingAPIClient` を用意しました。認証値は環境変数 `KARRY_API_TOKEN` または呼び出し元から渡された信頼済みの `credential_provider` から取得します。固定の本番origin `https://karry-agent-1.onrender.com` 以外へは送れず、リダイレクトも追いません。GETが401を返した場合にだけヘッダーを補正して最大1回再検証します。再検証失敗ならヘッダーを戻し、成功を装いません。POST等の実行依頼は二重実行を避けるため自動再送しません。

```python
from agent_core import HealingAPIClient

client = HealingAPIClient()
result = client.request("GET", "/api/agent/tools")
# KARRY_API_TOKENを利用。認証成功を含め result.status を確認する。
```

このクライアントはKarry-agentのコードに追加した利用可能な送信インターフェースです。Manusの接続基盤そのものへリトライ処理を差し込むものではありません。既存Google OAuthなど全てのSDKをこのクライアントへ置き換えてもいません。

## 未知の不具合とAI修復候補

未知のコード障害は修復パッチフックへ渡します。パッチ生成を有効にした場合は、単一ワーカーへキューイングし、HTTPエラーハンドラー内でAI処理の完了を待ちません。プロセス起動ごとに最大3件までです。呼び出すのは既存のGeminiクライアントで、1候補につき生成は1回、ツール選択やモデルが指定したコマンドの実行はありません。

```text
KARRY_SELF_HEALING_GENERATE_PATCHES=1
GEMINI_API_KEY=<既存の秘密設定>
GEMINI_MODEL=<既存モデル>
```

生成対象の既定allowlistは `src/karry_agent/core.py`、`src/karry_agent/tools.py`、`app.py` です。プロンプト等を除いた診断情報と、秘密値検査・サイズ検査を通過した対象ソースだけをモデルへ送ります。全てのエラーをAIへ渡すのではなく、認証、入力・設定不足、一時的なAPIエラーは分類済み診断や既存再試行で扱います。

診断情報はマスクしますが、ソース本文は変形・切断しません。送信対象は各50,000文字以下のファイルです。現在設定済みの8文字以上の秘密値を含むファイル、短い秘密値と完全一致する文字列リテラルを持つファイルは送信対象から除外します。除外したファイルの修正も受け付けません。モデルに渡すoldテキストと元のコードを厳密に照合できるようにしています。これは任意のハードコード済み秘密値を完全検出する仕組みではなく、対象ソース自体に秘密情報を埋め込まない運用が前提です。

モデルにはJSONの `edits: [{path, old, new}]` を要求します。対象外パス、パストラバーサル、不足・重複したoldテキスト、構文不正、現在設定済みの秘密値を含むnewコード、過大な変更を拒否します。生成コードは実行せず、Python ASTで構文を確認します。

候補は `/tmp/karry-repairs/` にmode 600のdiffとして保存します。`KARRY_REPAIR_ARTIFACT_DIR` で場所を変更できます。Renderの一時ファイルは再起動で失われるため、長期保存にはPRなどが必要です。候補の状態は `patch_ready`、検証範囲は **syntax_only** です。動作の修復や本番反映が成功したという状態にはしません。

## GitHub提出フック

以下を明示設定すると、生成候補を別ブランチ `self-healing/<incident_id>` とドラフトPRへ提出します。[1] [2] [3]

```text
KARRY_SELF_HEALING_CREATE_PRS=1
KARRY_REPAIR_GITHUB_REPO=kek46991-commits/karry-agent
KARRY_REPAIR_GITHUB_TOKEN=<Render用に別途設定するGitHub credential>
```

GitHub側のbaseコミットを取得し、ファイル内容が生成元と一致することを確認してから書き込みます。baseが変更済みなら候補を提出しません。mainへの書き込み、force push、自動マージ、Renderデプロイは行いません。コードは構文確認しかしていないため、PRのCIと動作レビューが必要です。複数ファイル提出の途中でGitHubエラーになった場合は未完了の候補ブランチが残る可能性があり、エンジンは `patch_failed` と記録します。自動で同じ変更を繰り返しません。

**既定はAI候補生成もGitHub提出も0（無効）です。** 既存の本番環境変数やManus接続の認証設定は変更していません。Render上のGitHub credentialは、この作業環境で利用するGitHub認証とは別に必要です。

## 運用API

`GET /api/agent/healing` でエンジン状態、`GET /api/agent/healing/incidents` で秘密情報を除去した診断ログを確認できます。どちらも既存 `KARRY_API_TOKEN` のBearer認証が必要で、token未設定時は503です。管理エンドポイントで不具合を意図的に発生させたり、修復コードを自由に登録・実行するAPIは追加していません。

## 検証範囲

テストは外部APIのモックと一時ディレクトリだけを使います。修復成功・検証失敗・ロールバック・再帰抑止・上限・秘密情報除去・受信認証境界・送信の最大1回補正・API/SSE接続・AI候補の検証・GitHubリモート差分拒否を確認します。ライブGemini呼び出し、実GitHub修復PRの自動発行、Calendar変更はテストしていません。実装用のレビューPR作成と、実行時に自動生成する修復PRは別です。

Render配置はこの実装PRをmainにマージしてから行います。PR保存の段階では本番のSelf-Healing稼働完了とは扱いません。

## References

[1]: https://docs.github.com/en/rest/git/refs "GitHub REST API: Git references"
[2]: https://docs.github.com/en/rest/repos/contents "GitHub REST API: Repository contents"
[3]: https://docs.github.com/en/rest/pulls/pulls "GitHub REST API: Pull requests"
