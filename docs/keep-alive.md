# Render Keep-Alive

面接時の初回起動待ちを減らすため、GitHub Actions から `karry-agent-1` の `/health` を10分間隔で呼び出します。

- Workflow: `.github/workflows/render-keep-alive.yml`
- URL: `https://karry-agent-1.onrender.com/health`
- 実行間隔: 10分
- 認証情報: 不要
- 手動実行: GitHub の Actions 画面で `Keep Render demo warm` → `Run workflow`

これは外部からの定期HTTPアクセスであり、Render Free のスリープ回避を補助します。GitHub Actions のスケジュール実行には遅延や停止があり得るため、面接直前には手動で Workflow を実行し、続けて `/health` とライブデモを一度開いてください。最も確実な常時稼働が必要な場合は、Render の有料常時稼働プラン、または UptimeRobot / Better Uptime などの5分間隔監視を検討します。

Keep-Alive は `/health` のみを呼び出します。Gemini API、Google Calendar、個人予定は読み取りません。
