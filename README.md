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
| The run job | `src/run_job/job.py` | Starts a sandbox, sends the input, prints the answer, deletes the sandbox |
| The sweep | `src/run_job/sweep.py` | Deletes each run sandbox that is older than its time limit |
| The run receiver | `src/run_receiver/` | The API below. It checks the token and the agent name, and makes one Job for one key |

## The run receiver API

Each call but `/healthz` needs the header `Authorization: Bearer <token>`.
The token is `RUN_RECEIVER_TOKEN`, from a Secret. A call with no token or a
wrong one gets `401`.

### `POST /runs`

```json
{"agent": "renovate-reviewer", "input": {"repo": "owner/name", "pr": 7, "head_sha": "<40 hex>"}, "key": "renovate-review-7-<sha>", "wait": true}
```

| Field | Rule |
| --- | --- |
| `agent` | A name in the list of agents (the config file). Another name gets `404` |
| `input` | A JSON object: the `input` of the contract of that agent. At most `RUN_RECEIVER_MAX_INPUT_BYTES` (256 KiB) as compact JSON, or `413` |
| `key` | The key of the run: 1 to 200 characters, letters, digits, `.`, `_`, `-`, and a letter or digit first. It names the Job (`run-<20 hex of sha256(key)>`) |
| `wait` | Optional, default `true`: answer when the run ends. `false`: answer at once |

Any other field gets `400`. The caller sets nothing of the Job but these.

| Status | Body | When |
| --- | --- | --- |
| `200` | `{"key", "agent", "job", "created", "status": "succeeded", "output": {...}}` | The run ended, and the agent answered. `output` is the `output` of the contract |
| `202` | `{"key", "agent", "job", "created", "status": "running"}` | `wait` is `false`, or the run did not end in its time limit plus 60 s |
| `400` | `{"error"}` | The body is not valid |
| `401` | `{"error"}` | No token, or a wrong one |
| `404` | `{"error"}` | The agent is not in the list |
| `409` | `{"error", "key"}` | The key belongs to a run with a different agent or input |
| `413` | `{"error"}` | The input is too large |
| `502` | `{"key", "agent", "job", "created", "status": "failed", "error"}` | The run failed: the agent answered with an error, or the sandbox did not start |
| `503` | `{"error"}` | The cluster API did not answer |
| `504` | `{"key", "agent", "job", "created", "status": "timed_out", "error"}` | The Job reached its time limit (`activeDeadlineSeconds`) |

**One run for one key.** A second call with the same key, the same agent and
the same input makes no second Job. It gets `"created": false` and the same
run: `202` while it runs, and the same answer when it ended. With a different
agent or input it gets `409`. The Job, and so the key, stays until
`ttlSecondsAfterFinished` of the template (7 days in the example); after that
the same key makes a new run.

### `GET /runs/{key}`

The state of the run of that key, with the same bodies and codes as `POST`
(no `created`). `404` when no Job has that key.

### `GET /healthz`

`200 {"status": "ok"}`, with no token. For the probes.

## The image contract

| Item | Value |
| --- | --- |
| Image | `ghcr.io/it3e/tnwks-workflow-agents:<version>@sha256:<digest>`. A `vX.Y.Z` release pushes `:X.Y.Z` and `:sha-<short>`. `main` pushes nothing, and nothing pushes `:latest` |
| User | UID 10001, GID 10001, `HOME=/tmp`. Code in `/app/src`, read-only |
| Entrypoint | `python3 /app/src/entrypoint.py`, default command `serve` |
| Root file system | Read-only works. The run job and the sweep need `/tmp` writable (an `emptyDir`): the CLI config goes there |
| Port | 8080 for `serve` and `run-receiver` |

| Command (`args`) | Part | Kubernetes rights of its service account |
| --- | --- | --- |
| `serve` (or none) | A workflow agent, for the agent of `AGENT_NAME` | None. In a sandbox it has no service account token |
| `run-receiver` | The run receiver, a Deployment and a Service | A Role in its own namespace: `jobs` (`batch`): `create`, `get`; `pods`: `list`; `pods/log`: `get` |
| `run-job` | The run job, a Job from the template | None (`automountServiceAccountToken: false`). It talks to the sandbox gateway with the client certificate only |
| `sweep [--dry-run]` | The time limit of the sandboxes, a CronJob | None, as the run job |

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

### Environment of `run-job`

The Job template of each agent sets these. The run receiver adds `RUN_KEY`
and `RUN_INPUT` only.

| Variable | What it holds |
| --- | --- |
| `AGENT_NAME` | The agent. It must be the name of the template in the list |
| `RUN_KEY`, `RUN_INPUT` | The key of the run, and the input as a JSON object. Set by the receiver |
| `SANDBOX_IMAGE` | The image of the sandbox: this image, at the same digest as the run job |
| `SANDBOX_POLICY` | The path of the sandbox policy file (a ConfigMap). It must list `/app` as read-only ([`examples/sandbox-policy.yaml`](examples/sandbox-policy.yaml)) |
| `SANDBOX_PROVIDERS` | The providers of the agent, comma separated |
| `SANDBOX_CPU`, `SANDBOX_MEMORY` | Optional limits of the sandbox |
| `AGENT_ENV_<NAME>` | Sets `<NAME>` in the sandbox, for example `AGENT_ENV_MODEL_ID`. One line, at most 8 KiB. A name with KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL or PRIVATE is refused: a credential goes through a provider |
| `OPENSHELL_GATEWAY_URL` | The sandbox gateway of the trust domain, `https://<service>.<namespace>.svc.cluster.local:8080` |
| `OPENSHELL_MTLS_DIR` | The client certificate (`ca.crt`, `tls.crt`, `tls.key`), from the Secret. Default `/etc/openshell/mtls` |
| `OPENSHELL_GATEWAY_NAME` | The name of the gateway in the CLI config. Default `workflows` |
| `READY_TIMEOUT_SECONDS`, `RUN_TIMEOUT_SECONDS` | The wait for `/ping` (180) and for the answer (1800) |

The run job prints one result line, `workflow-run-result: {...}`, as its last
line on standard output; the receiver reads it from the pod log. Exit codes:
0 the run succeeded, 1 the agent answered with an error, 2 the environment is
not complete, 3 the sandbox did not start or answer, 4 no answer in time, 5
the pod was stopped, 6 the sandbox was not deleted. Each sandbox gets the
labels `workflow-run=true` and `workflow-agent=<name>`, and the name
`run-<12 hex of the key>-<start time in base 36>`.

### Environment of `run-receiver`

| Variable | What it holds |
| --- | --- |
| `RUN_RECEIVER_TOKEN` | The token of the callers, at least 32 characters, from a Secret |
| `RUN_RECEIVER_CONFIG` | The list of agents. Default `/etc/run-receiver/agents.yaml` |
| `POD_NAMESPACE` | Its namespace, from the downward API. Each Job goes there |
| `RUN_RECEIVER_MAX_INPUT_BYTES`, `RUN_RECEIVER_POLL_SECONDS` | Optional: 262144, and 2 |

### Environment of `sweep`

`OPENSHELL_GATEWAY_URL`, `OPENSHELL_MTLS_DIR` and `OPENSHELL_GATEWAY_NAME` as
for the run job, and `SWEEP_MAX_AGE_SECONDS` (default 3600). Set it above the
`activeDeadlineSeconds` of each template.

### The list of agents and the Job template

[`examples/agents.yaml`](examples/agents.yaml) is the form of the config file,
and the tests load it. For each agent: a `batch/v1` Job with no name and no
namespace, `backoffLimit: 0`, an `activeDeadlineSeconds`, `restartPolicy:
Never`, one container named `run-job` with `AGENT_NAME` set to the name of
the agent, and no `RUN_KEY` or `RUN_INPUT`. The receiver refuses to start on a
template that breaks one of these, or that names an agent that the image does
not have. It changes a template in these places only: `metadata.name`,
`metadata.namespace`, the label `workflow-agents/agent` (on the Job and the
pod), the annotations `workflow-agents/run-key` and
`workflow-agents/fingerprint`, and the two variables.

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
test, then each test in `tests/` inside the image. The model, GitHub, the
trace collector, the Kubernetes API and the OpenShell CLI are fakes in
`tests/support/`, on 127.0.0.1 in the same container.

| Test | What it covers |
| --- | --- |
| `test_contract.py` | The two paths for each agent of the image. The image with no command serves `AGENT_NAME` on 8080. Each span of a run is at the collector before the answer, and no text of the run is in a span |
| `test_renovate_reviewer.py` | Read tools only, one comment, the gates, the allowed GitHub paths, no untrusted comment in the model input |
| `test_run_receiver.py` | The token, the list of agents, the Job from the template, one run for one key, the status codes |
| `test_run_job.py` | The whole run with a fake CLI, the delete on each failure and on SIGTERM, and the sweep. The whole run on a kind cluster runs in CI only, with `KIND_E2E=1` and the kind setup |

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
  `renovate/template` PRs. The OpenShell CLI never merges by itself: its
  checksums change by hand, with the chart release in tnwks-ops.
