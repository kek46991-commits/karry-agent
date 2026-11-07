from karry_agent.core import SYSTEM_INSTRUCTION
from karry_agent.github_workflow import GITHUB_AUTO_SYNC_SYSTEM_PROMPT


def test_github_workflow_policy_is_injected_into_agent_system_instruction():
    assert GITHUB_AUTO_SYNC_SYSTEM_PROMPT in SYSTEM_INSTRUCTION
    for required_rule in (
        "version` operation",
        "run_git_command",
        "confirm_initialization=true",
        "status` operation",
        "専用の短い作業ブランチ",
        "main` へ直接commit/pushしない",
        "git diff --cached",
        "force push",
        "PR pendingだけをGitHub連携完了と表現しない",
    ):
        assert required_rule in GITHUB_AUTO_SYNC_SYSTEM_PROMPT


def test_github_workflow_policy_never_recommends_blind_staging_or_main_push():
    assert "git add .`、`git add -A`" in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
    assert "git push -u origin main" not in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
    assert "git push -f" not in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
    assert "non-force push" in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
    assert "任意コマンド文字列は実行しません" in GITHUB_AUTO_SYNC_SYSTEM_PROMPT


def test_github_workflow_policy_keeps_secret_chat_outside_external_operations():
    assert "トークン" in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
    assert "秘密鍵" in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
    assert "PR作成だけの依頼ではmerge、deploy、本番変更を行わない" in GITHUB_AUTO_SYNC_SYSTEM_PROMPT


def test_github_workflow_policy_describes_safe_remote_auto_merge():
    for rule in ("`github_repository`", "`agent/*`", "SHA固定でマージ", "`KARRY_GIT_AUTO_MERGE=1`",
                 "CIが1件も報告されない場合は失敗扱い", "マージ済みと表現しない", "`confirm_merge=true`"):
        assert rule in GITHUB_AUTO_SYNC_SYSTEM_PROMPT
