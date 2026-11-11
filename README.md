# tnwks-workflow-agents

The workflow agents of tnwks and the parts that run them, as one image. A
workflow agent does one job, for example the review of a Renovate update.
It runs one time in a sandbox of its trust domain, behind a run receiver that
takes a call with a token, makes a Job from a fixed template, and gives back
the answer. The design is effort 0003 of tnwks-coe (the agent platform).

```text
trigger --POST /runs, token--> run receiver --Job from the template--> run job
                                                                         |
          openshell sandbox create (this image, `serve`, AGENT_NAME) <---+
          port forward with the client certificate, GET /ping, POST /invocations
          answer, then sandbox delete (also on an error and on SIGTERM)
```

| Part | Where | What it does |
| --- | --- | --- |
| The container contract | `src/contract/` | `POST /invocations` and `GET /ping` on port 8080 for the agent that `AGENT_NAME` names. It flushes the spans of a run before its answer, and keeps the text of a run out of each span |
| The workflow agents | `src/agents/<name>/` | One package each: its loop, its prompt, its tools. Today: `renovate-reviewer` |

## The image contract

| Item | Value |
| --- | --- |
| Image | `ghcr.io/it3e/tnwks-workflow-agents:<version>@sha256:<digest>`. A `vX.Y.Z` release pushes `:X.Y.Z` and `:sha-<short>`. `main` pushes nothing, and nothing pushes `:latest` |
| User | UID 10001, GID 10001, `HOME=/tmp`. Code in `/app/src`, read-only |
| Entrypoint | `python3 /app/src/entrypoint.py`, default command `serve` |
| Port | 8080 for `serve` |

| Command (`args`) | Part | Kubernetes rights of its service account |
| --- | --- | --- |
| `serve` (or none) | A workflow agent, for the agent of `AGENT_NAME` | None. In a sandbox it has no service account token |

### Environment of `serve` (the agent, in the sandbox)

| Variable | What it holds |
| --- | --- |
| `AGENT_NAME` | The agent to serve. The run job sets it with `--env` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Optional: the trace collector (OTLP over HTTP). With it, each run flushes its spans before it answers |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | Set by the code to `gen_ai_latest_experimental,gen_ai_unredacted_attributes=`, whatever it held: no prompt, answer or tool text in a span |
| `BIND` | Optional: the listen address, default `0.0.0.0` |

The `renovate-reviewer` agent adds:

| Variable | What it holds |
| --- | --- |
| `MODEL_GATEWAY_URL` | The OpenAI-compatible base URL of the model gateway, with `/v1` |
| `MODEL_GATEWAY_KEY` | The key of the model gateway. In a sandbox the provider sets a placeholder, and the supervisor puts the key in the `Authorization: Bearer` header outside the sandbox |
| `MODEL_ID` | The model name at the gateway |
| `GITHUB_TOKEN` | The GitHub credential, from a provider, as above |
| `GITHUB_API_URL` | Optional, default `https://api.github.com` |
| `REVIEWER_REPOSITORIES` | The repositories it may review, comma separated. Any other gets `400` |
| `REVIEWER_MAX_TURNS`, `MODEL_MAX_TOKENS` | Optional limits: model calls in one run (40), output tokens in one call (8192) |

### The providers of the Renovate reviewer

| Provider | Variable in the sandbox | Address and paths |
| --- | --- | --- |
| The model gateway | `MODEL_GATEWAY_KEY`, as `Authorization: Bearer` | `POST /v1/chat/completions` at the model gateway |
| GitHub | `GITHUB_TOKEN`, as `Authorization: Bearer` | `api.github.com`: `GET` on `/repos/{repo}/pulls/{n}`, `/pulls/{n}/files`, `/contents/**`, `/issues/{n}/comments`, `/commits/{sha}/check-runs`, and `POST /repos/{repo}/issues/{n}/comments` |

The GitHub credential needs read access to pull requests, contents and
checks, and write access to issue comments, on the reviewed repository only.
It needs no right to merge, push, approve or change a label.

## The Renovate reviewer

Input `{"repo", "pr", "head_sha"}`. The code checks the gates: the repository
is in `REVIEWER_REPOSITORIES`, the pull request is open, its head is still
`head_sha`, and no review comment for that head is there yet. Then the model
reads the pull request with six read tools (the overview with the release
notes, the changed files, a diff, a file at the head, the comments of trusted
authors, the checks) and writes a review. The code posts it as one comment,
with a verdict: `ready`, `needs-changes`, `one-way` or `blocked`. The answer
is `{"status": "reviewed" | "skipped", "repo", "pr", "head_sha", "verdict",
"comment_url", "reason"}`. The review is advice; the owner decides.

## Tests

`task test` builds the image and runs `bin/image-test`, as CI does: a smoke
test, then each test in `tests/` inside the image. The model, GitHub and the
trace collector are fakes in `tests/support/`, on 127.0.0.1 in the same
container.

| Test | What it covers |
| --- | --- |
| `test_contract.py` | The two paths for each agent of the image. The image with no command serves `AGENT_NAME` on 8080. Each span of a run is at the collector before the answer, and no text of the run is in a span |
| `test_renovate_reviewer.py` | Read tools only, one comment, the gates, the allowed GitHub paths, no untrusted comment in the model input |

## Workflow

This repo follows the app-repo standard in
[iT3E/app-repo-template](https://github.com/iT3E/app-repo-template), with
CI from [iT3E/github-workflows](https://github.com/iT3E/github-workflows);
`AGENTS.md` has the short version.

- Changes go through PRs, never straight to `main`. Merge only when every
  check is green (`gh-merge-safe.sh <pr> iT3E/tnwks-workflow-agents`).
- `task precommit:init` installs the hooks: betterleaks, the privacy
  denylist (staged files, git identity, commit message) and ruff format.
- Each PR adds a Changie fragment (`changie new`) or carries the
  `skip-changelog` label. Merging fragments cuts a release (`vX.Y.Z` tag,
  GitHub Release, image `:X.Y.Z`).
- Renovate keeps dependencies current. Patch, digest and non-0.x minor
  updates merge themselves once CI is green; majors wait for review on the
  Renovate Dashboard issue. Template releases arrive as their own
  `renovate/template` PRs.
