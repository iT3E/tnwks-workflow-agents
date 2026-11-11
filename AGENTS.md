# AGENTS.md

How to work in this repo. It follows the app-repo standard in
[iT3E/app-repo-template](https://github.com/iT3E/app-repo-template), which
manages most of the repo setup (`.copier-answers.yml` lists the release);
tnwks-ops is the reference for anything not covered there.

## What this is

The workflow agents of tnwks and their run path, as one image (Python 3.13,
Strands): each workflow agent with the container contract (`POST
/invocations`, `GET /ping` on port 8080), the run receiver, the run job and
the sweep. CI builds `ghcr.io/it3e/tnwks-workflow-agents`, which is public.
tnwks-ops runs it in the namespace of the `workflows` trust domain and pins
`:<version>@sha256`; the manifests, the list of agents with their Job
templates, the sandbox policies, the providers and every secret live there,
not here. `README.md` has the API of the run receiver and the image contract.
The design is effort 0003 of tnwks-coe (the agent platform).

Rules that hold for each change:

- **An agent never decides.** A workflow agent reads and answers. No tool
  merges, approves, pushes or changes a label. A write that a run needs (the
  review comment) is code of the agent, not a tool of the model, and the
  provider of the agent allows that one path.
- **The prompt and the tools are code in a public image.** No household
  name, hostname, address, account name, private repository detail or
  secret in the code, the prompt, the tests or the examples. Tests use the
  fakes in `tests/support/` with made-up data.
- **No credential reaches a sandbox.** The run job passes non-secret
  variables only (`AGENT_ENV_*`, and it refuses a name that looks like a
  credential); each credential comes through a provider of the sandbox
  gateway.
- **A new agent** is one package in `src/agents/`, one line in
  `src/agents/__init__.py`, and one case in `tests/support/cases.py`
  (`test_contract.py` fails without it). Its Job template and provider go
  to tnwks-ops.
- **No real model or GitHub call in a test**, and no cluster but the kind
  cluster that `bin/image-test` makes in CI (`tests/kind/up.sh`, only with
  `CI=true` or `IMAGE_TEST_KIND=1`). Never set `IMAGE_TEST_KIND=1` on a
  workstation that runs a live cluster, and never point a test at the
  default `kubectl` context.

## Change workflow

- Every change goes through a PR, squash-merged. Never push to `main`.
  GitHub Free has no rulesets on private repos, so nothing enforces this
  except you: merge with `gh-merge-safe.sh <pr> iT3E/<this repo>`, which
  refuses unless every check passed. Renovate merges its own PRs only when
  every check is green (`ignoreTests: false`).
- Install the hooks once per clone: `task precommit:init`.
- Add a Changie fragment (`changie new`, under `.changes/unreleased/`) or
  label the PR `skip-changelog` (docs, CI-only, chores). Merging fragments
  cuts a release: `release.yaml` tags `vX.Y.Z` and builds `:X.Y.Z`. Kinds:
  Fixed/Security are a patch, Added/Changed/Deprecated/Removed a minor,
  Breaking a major.
- Renovate PRs carry no fragment. A merged one that changed what goes into
  the image (dependencies, the Dockerfile) releases a patch by itself
  (`auto-fragment`, "Dependency updates"); template and CI-only updates
  release nothing. A PR by a person always brings its own fragment.
- Image tags: only a release tag pushes, as `:<version>` and
  `:sha-<short>`; `main` builds without pushing. tnwks-ops pins
  `:<version>@sha256`, so a release reaches the cluster only when a
  tnwks-ops PR moves that pin.
- Checks: `build.yaml` (compileall, the Docker build and its tests), `privacy`, `betterleaks`, `changelog`,
  `actionlint` (workflow changes only), and the labeler. Locally: `task test`.
  The tests run inside the image (`bin/image-test`), so the host needs
  Docker only.
- Dependencies: `requirements.txt` (the packages the code imports, one
  release each) and `constraints.txt` (the full frozen set). Change a
  package in both, resolved in the base image of the `Dockerfile`. The
  OpenShell CLI release in the `Dockerfile` moves with its checksums and
  with the sandbox gateway chart in tnwks-ops.
- The workflows are thin callers of the reusable workflows in
  iT3E/github-workflows, pinned by the template, and the files listed in
  app-repo-template's README are managed by Copier. Change them there; a
  template release reaches this repo as a
  `copier update` PR (Renovate, label `renovate/template`, merged by
  Renovate when green, or `task template:update`). A local edit to a managed file survives
  updates, but keep those rare.

## Privacy

The image is public, and the same rules as tnwks-ops apply here: no family
names (first names, nicknames, surname), personal email addresses, schools,
ages, addresses or anything else that says who is who. That covers files,
paths, commit messages, your git identity, PR and issue titles and bodies,
comments and reviews.

- `bin/privacy-check` checks the tnwks-ops denylist. CI reads it from the
  `PRIVACY_TERMS` secret (`task privacy:sync` refreshes it); locally it
  reads `$PRIVACY_TERMS` or a tnwks-ops checkout at `../tnwks-ops`.
- Before posting a PR body, comment or review, write it to a file, run
  `bin/privacy-check --text < file`, and post with `--body-file`.
- Exceptions (`.github/privacy-allow`, `privacy:allow` on a line) are the
  owner's call. Don't add one to get a PR through.
- Never put a credential in a file or doc. Secrets are SOPS Secrets in
  tnwks-ops and reach the service as env vars.

## Writing

Plain prose, short sentences. No emdashes in new prose (use a comma, colon
or parentheses). No AI-attribution trailers in commits.
