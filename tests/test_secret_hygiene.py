"""Repository-level guard: no real credentials or env files are ever committed."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = re.compile(
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}|AIza[0-9A-Za-z_-]{35}|ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}"
    r"|rnd_[A-Za-z0-9]{24,}|r8_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[bp]-[0-9]{6,}-[A-Za-z0-9-]+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / line for line in out.splitlines() if line]


def test_no_env_or_credential_files_are_tracked():
    names = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
    leaked = [n for n in names if re.search(r"(^|/)(\.env(\..+)?|credentials\.json|token\.json|client_secret.*\.json|.*\.pem|.*\.key)$", n)
              and n != ".env.example"]
    assert leaked == []


def test_no_credential_shaped_strings_in_tracked_files():
    hits = []
    for path in tracked_files():
        if not path.is_file() or path.stat().st_size > 2_000_000 or path.suffix in {".png", ".jpg", ".webp", ".ico"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits += [f"{path.relative_to(ROOT)}:{m.group(0)[:8]}..." for m in PATTERNS.finditer(text)]
    assert hits == []


def test_env_example_contains_only_placeholders():
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|DATABASE_URL", key) and value:
            assert value.startswith("YOUR_") or value in {"0", "1"}, key


def test_gitignore_covers_secret_files():
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for rule in (".env", ".env.*", "!.env.example", "credentials.json", "token.json", "*.pem"):
        assert rule in rules
