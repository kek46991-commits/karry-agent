# GitHub automation (`github_repository` tool)

Karry-Agent can push its own code changes to GitHub and merge them without a local checkout, `gh` or stored Git credentials. It talks to the GitHub REST API directly, so it also works on Render. The code is in `src/karry_agent/github_repo.py`.

## Flow (`ship`)

1. Create a new `agent/<slug>-<YYYYMMDD>-<random>` branch from the latest base branch (`main` by default).
2. Commit all files in **one commit**, using the Git Data API (blobs → tree → commit → ref). The base branch is never written directly.
3. Open a PR into the base branch.
4. Wait for CI (`KARRY_GIT_CHECK_TIMEOUT`, default 600 seconds).
5. Merge with squash, **only when all of these are true**:
   - `KARRY_GIT_AUTO_MERGE=1`
   - every check run finished as `success` / `neutral` / `skipped`. Zero reported checks counts as a failure.
   - the PR is open, comes from an `agent/*` branch in the same repository (not a fork), and targets the base branch
   - the PR head is still the commit created in step 2. The merge call pins that SHA.
   - the PR does not touch a protected path
6. Delete the merged branch.

When any condition fails, the PR stays open and the result explains why (`reason`), for example `auto_merge_disabled`, `checks_failed`, `checks_pending` or `head_changed`.

## Operations

| operation | Purpose |
|---|---|
| `status` | Shows whether a token is set, the allowed repositories, the base branch and whether auto-merge is on. The token is never returned. |
| `ship` | The flow above. Pass `files: [{path, content}]`, or `paths` to read files from `KARRY_WORKSPACE`. Use `merge=false` to stop after CI, and `wait_for_checks=false` to stop after opening the PR. |
| `pr_status` | Returns the PR state, head SHA and CI summary. |
| `merge_pr` | Merges a PR later. Requires `confirm_merge=true` and `expected_sha` (the reviewed head SHA), and applies the same checks as `ship`. |

## Safety rules

- Only repositories listed in `KARRY_GIT_ALLOWED_REPOS` can be changed.
- These paths are rejected before any GitHub call:
  - `.github/`, `.devin/`, `.agents/`
  - `render.yaml`, `Dockerfile`, dependency manifests, `.env*`
  - keys and secret files
  - `app.py` and the authentication, self-healing and automation modules (`optimus_core.PROTECTED_PATHS`, plus this module, `git_workspace.py`, `auth_token.py`, `feature_flags.py`)
  - absolute paths, `..`, and paths that start with `-`
- File content is rejected if it contains:
  - a credential pattern (GitHub, AWS, Google, Render, OpenAI, Slack or a private key)
  - the value of any secret environment variable
- Each `ship` accepts at most 20 files of up to 200 KB each.
- The tool takes only an operation name and fixed arguments; it never runs shell strings. Errors include the HTTP status, but never the token.

## Configuration

| Variable | Value |
|---|---|
| `KARRY_GIT_GITHUB_TOKEN` | Fine-grained PAT limited to the allowed repositories. Needs Contents: Read & write, Pull requests: Read & write, and Checks: Read. |
| `KARRY_GIT_ALLOWED_REPOS` | Comma-separated `owner/name` list, e.g. `kek46991-commits/karry-agent` |
| `KARRY_GIT_BASE_BRANCH` | Base branch, `main` by default |
| `KARRY_GIT_AUTO_MERGE` | Set to `1` to merge automatically after green CI. Leave unset to stop at an open PR. |
| `KARRY_GIT_CHECK_TIMEOUT` | Seconds to wait for CI, from 0 to 1800 (default 600) |

Render deploys `main` automatically (`autoDeploy`), so an auto-merged change goes to production. Keep `KARRY_GIT_AUTO_MERGE` unset unless you want that.
