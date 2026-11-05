# Self-cloning sandbox API

`POST /api/agent/clone` creates an isolated `sandbox/<slug>` branch in the repository configured on the server. On Render, the repository, branch, and source SHA default to `RENDER_GIT_REPO_SLUG`, `RENDER_GIT_BRANCH`, and `RENDER_GIT_COMMIT`, so the clone starts from the code currently running in that environment. Outside Render, configure `KARRY_CLONE_REPOSITORY`, `KARRY_CLONE_BASE_BRANCH` (default `main`), and optionally `KARRY_CLONE_SOURCE_SHA`. The endpoint never accepts a repository URL, workspace ID, arbitrary environment variables, or provider credentials from the caller.

All requests require the owner token in `Authorization: Bearer …` or `X-Owner-Token`. Branch creation uses `KARRY_CLONE_GITHUB_TOKEN` and `KARRY_CLONE_REPOSITORY=owner/repository`. Grant the GitHub token access only to that repository, with Contents read/write permission. The branch slug must be lowercase and may contain letters, numbers, and hyphens.

Render provisioning is opt-in. The default request creates only the GitHub branch:

```json
{"slug":"experiment-2026-10"}
```

To create a Render Web Service as well, configure `KARRY_CLONE_RENDER_API_KEY` with service-creation access limited to the intended workspace, `KARRY_CLONE_RENDER_OWNER_ID`, and optionally `KARRY_CLONE_RENDER_REGION` (default `oregon`) and `KARRY_CLONE_SERVICE_PREFIX` (default `karry-agent-sandbox`). Render sandbox provisioning is restricted to the free plan to prevent accidental paid resources. Then explicitly confirm resource creation:

```json
{
  "slug":"experiment-2026-10",
  "provision_render":true,
  "confirm_resource_creation":true
}
```

Render autodeploy is limited to commits whose checks pass. Production environment variables and secrets are never copied to the sandbox service. If Render provisioning fails after branch creation, the response reports the created branch as `partial_success`; the branch is intentionally not deleted automatically.

Integration tokens must be stored as deployment secrets and must not be committed to the repository. The service creation payload follows the [Render Create Service API](https://api-docs.render.com/reference/create-service), and branch references use the [GitHub Create a Reference API](https://docs.github.com/en/rest/git/refs#create-a-reference).
