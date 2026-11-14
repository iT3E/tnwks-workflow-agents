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
| `200` | `{"key", "agent", "job", "created", "status": "succeeded", "output": {...}}` | The run ended, and the agent answered. `output` is the `output` of the contract. `"output_kept": false` in place of `output` when the output is no longer kept (below) |
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

**A retry is a new key.** The same key always gets the same answer, also a
failure, for as long as the Job stays. So a caller that tries again puts an
attempt number at the end of the key, for example
`renovate-review-<pr>-<sha7>-<n>` with `n` from 1:

- It tries again only after a `failed` (`502`) or `timed_out` (`504`)
  answer, with `n + 1`, and at most 3 attempts in all. It never tries again
  after a `2xx` answer.
- After a call with no answer (a lost connection, a `503`), it calls again
  with the same key: that gets the same run, not a second one.
- The receiver does not change for this: each attempt is its own Job, and the
  Job of each attempt stays as a record. The agent itself must be safe to run
  twice for the same work (the Renovate reviewer posts one comment for each
  head).

**The answer stays with the Job, not with its pod.** The answer of a run is
the result line in the log of its pod. A cluster can delete that pod seconds
after it ends: the pod GC (`terminated-pod-gc-threshold`) deletes the oldest
terminated pods of the whole cluster over the threshold, every 20 s. So the
receiver does not wait for the end of a run to read its log:

- For each running run, it follows the log of the pod as it is written
  (`follow=true`, with its `pods/log` right). It starts on the call that
  makes or finds the run, and from its loop (every
  `RUN_RECEIVER_RECORD_SECONDS`) for a run that no call started, as after a
  restart of the receiver. One follower for one run.
- The run job writes the result line last, just before it exits, and the
  pod GC deletes only a pod that has ended. So the line is in hand before the
  pod can go.
- The receiver keeps the result in the annotation `workflow-agents/result` of
  the Job, and a later call reads the annotation.
- A result of more than 64 KiB (as compact JSON) is kept without its
  `output`: each call gets the `status`, and `"output_kept": false` in place
  of `output`.
- When the receiver was down for the whole end of a run, and the pod went
  before it came back, the answer is the state of the Job: `succeeded`
  (`200`), `failed` (`502`) or `timed_out` (`504`), with `"output_kept":
  false` and no `output`. A failed run says so in `error`. The Job state is
  true: the run job ends with exit code 0 only when the agent answered and
  its sandbox was deleted.
- A kept result changes only an annotation of the Job (a merge patch of
  `metadata.annotations`). If the patch fails, the receiver still answers
  from what it read, and logs one line.

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
| Tools | The OpenShell CLI (`/usr/local/bin/openshell`, the release of the sandbox gateway) and the `ssh` client, which the CLI runs for the port forward of the run job |
| Port | 8080 for `serve` and `run-receiver` |

| Command (`args`) | Part | Kubernetes rights of its service account |
| --- | --- | --- |
| `serve` (or none) | A workflow agent, for the agent of `AGENT_NAME` | None. In a sandbox it has no service account token |
| `run-receiver` | The run receiver, a Deployment and a Service | A Role in its own namespace: `jobs` (`batch`): `create`, `get`, `list`, `patch` (the annotation of the kept result only); `pods`: `list`; `pods/log`: `get` |
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
| `MODEL_GATEWAY_URL` | The root URL of the model gateway. The agent calls its Anthropic Messages API, `POST /v1/messages` (a trailing `/v1` is removed) |
| `MODEL_GATEWAY_KEY` | The key of the model gateway, sent as `Authorization: Bearer`. In a sandbox the provider sets a placeholder, and the supervisor puts the key in that header outside the sandbox |
| `MODEL_ID` | Optional: the model name at the gateway, default `agents-workflows` |
| `REVIEW_EFFORT` | Optional: the effort of each model call, sent as `output_config.effort`: `low`, `medium`, `high`, `xhigh` or `max`. Default `high`. Any other value fails the run |
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
`run-<7 hex of the key>-<start time in base 36>` (OpenShell takes at most 19 characters).

### Environment of `run-receiver`

| Variable | What it holds |
| --- | --- |
| `RUN_RECEIVER_TOKEN` | The token of the callers, at least 32 characters, from a Secret |
| `RUN_RECEIVER_CONFIG` | The list of agents. Default `/etc/run-receiver/agents.yaml` |
| `POD_NAMESPACE` | Its namespace, from the downward API. Each Job goes there |
| `RUN_RECEIVER_MAX_INPUT_BYTES`, `RUN_RECEIVER_POLL_SECONDS` | Optional: 262144, and 2 |
| `RUN_RECEIVER_RECORD_SECONDS` | Optional: the wait between two rounds of the loop that follows the runs no call started and keeps the results of ended Jobs. Default 10; `0` turns the loop off |

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

Each provider comes from a provider profile in
[`examples/providers/`](examples/providers/): the gateway imports the profile
(`openshell profile import -f`), and `openshell provider create --type <id>`
makes the provider with the credential. The profile names the address, the
paths and the binaries where the proxy of the sandbox puts the credential in.
The sandbox holds only a placeholder.

| Provider (profile) | Variable in the sandbox | Address and paths |
| --- | --- | --- |
| `model-gateway` (`workflow-model-gateway`) | `MODEL_GATEWAY_KEY`, as `Authorization: Bearer` | `POST /v1/messages` at `litellm-app.ai.svc.cluster.local:4000` |
| `github-reviewer` (`workflow-github-reviewer`) | `GITHUB_TOKEN`, as `Authorization: Bearer` | `api.github.com`: `GET` on `/repos/{repo}/pulls/*`, `/pulls/*/files`, `/contents/**`, `/issues/*/comments`, `/commits/*/check-runs`, and `POST /repos/{repo}/issues/*/comments`, for one repository |

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

The model is the Strands Anthropic model, on the Anthropic Messages API of
the model gateway. Each call sends the effort (`output_config.effort`), and
the history only grows: no turn is trimmed or summarized
(`NullConversationManager`), so each call starts with the messages of the
call before it. A model bridge behind the gateway matches a call to its live
conversation that way.

## Tests

`task test` builds the image and runs `bin/image-test`, as CI does: a smoke
test, then each test in `tests/` inside the image. The model, GitHub, the
trace collector, the Kubernetes API and the OpenShell CLI are fakes in
`tests/support/`, on 127.0.0.1 in the same container.

| Test | What it covers |
| --- | --- |
| `test_contract.py` | The two paths for each agent of the image. The image with no command serves `AGENT_NAME` on 8080. Each span of a run is at the collector before the answer, and no text of the run is in a span |
| `test_renovate_reviewer.py` | Read tools only, one comment, the gates, the allowed GitHub paths, no untrusted comment in the model input |
| `test_run_receiver.py` | The token, the list of agents, the Job from the template, one run for one key, the status codes, and the kept result when the pod is gone |
| `test_run_job.py` | The whole run with a fake CLI, the delete on each failure and on SIGTERM, and the sweep |
| `kind/test_on_kind.py` | CI only, on the host of the job: the whole run on a kind cluster (below) |

### The test cluster

In CI, `bin/image-test` then makes a kind cluster with one node
([`tests/kind/up.sh`](tests/kind/up.sh)) and runs
[`tests/kind/test_on_kind.py`](tests/kind/test_on_kind.py) on it. The
cluster has Agent Sandbox and the OpenShell chart of the release of the
`openshell-release` stage of the Dockerfile, in the namespace
`agents-workflows` (the sandbox gateway of the trust domain); the providers
of `examples/providers/`; the run receiver with its Role and the list of
`examples/agents.yaml`; and fakes in the image under test: the model gateway
at `litellm-app.ai`, the trace collector at `alloy.monitoring`, and GitHub in
`test-support`. The tests:

1. A run through `POST /runs` answers `200` with the review, the model and
   GitHub got the test credentials from the providers (the sandbox has only a
   placeholder), the spans got to the collector, and no sandbox is left. The
   same key gets the same run, also after the pod of the run is deleted.
   The cluster has the pod GC of the platform (`terminated-pod-gc-threshold:
   1`), and the test deletes the run pod the moment it ends: the answer still
   has the output.
2. A second key for the same head is skipped: one comment for one head.
3. A run job that gets SIGTERM in the middle of a run deletes its sandbox,
   and the sweep (`--dry-run`) finds the sandbox by its label first.

Each kubectl and helm call names the kubeconfig file of the test cluster.
When a step fails, [`tests/kind/dump.sh`](tests/kind/dump.sh) prints the
pods, the events and the logs to the job log. The pins of the cluster are in
[`.github/test-cluster/Dockerfile`](.github/test-cluster/Dockerfile)
(Renovate) and in `up.sh` (kind, helm, kubectl, by hand), the same as in the
agent image repository. Never run it on a workstation that runs a live
cluster: `up.sh` refuses unless `CI=true` or `IMAGE_TEST_KIND=1`.

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
  `renovate/template` PRs. OpenShell (the `openshell-release` stage and the
  CLI) is one group that never merges by itself: the checksums of the CLI
  change by hand, with the chart release in tnwks-ops.
