# Changelog

Notable changes to this repository, newest first. Each PR adds a Changie
fragment under `.changes/unreleased/`; merging to `main` releases them.

## v0.1.3 - 2026-11-13

### Fixed

* renovate reviewer: the comment says the verdict once (a first line of the model text that holds only the verdict is dropped). run receiver: no httpx line for each idle round of the loop that keeps the results (httpx logs at WARNING in the receiver), and one INFO line when the loop keeps a result. The README gives the retry rule of a caller: an attempt number in the key

## v0.1.2 - 2026-11-13

### Fixed

* run receiver: the answer of an ended run stays when the cluster deletes its pod. The receiver keeps the result in the annotation workflow-agents/result of the Job (on the first read, and in a loop over ended Jobs), and answers the Job state with "output_kept": false when the pod went first. The Role needs jobs list and patch

## v0.1.1 - 2026-11-13

### Fixed

* The Renovate reviewer calls the Anthropic Messages API of the model gateway (POST /v1/messages, the route of the agents), sends the effort of each call in output_config (REVIEW_EFFORT, default high), and keeps the history append only (NullConversationManager), so the model bridge carries the effort and matches each call to its conversation. MODEL_ID defaults to agents-workflows, and MODEL_GATEWAY_URL is the root URL of the gateway. The image drops the OpenAI package and adds the Anthropic SDK

## v0.1.0 - 2026-11-11

### Added

* The image of the workflow agents, from the app template: the container contract (POST /invocations and GET /ping on port 8080, with the spans flushed before each answer and no text of a run in a span), and the Renovate reviewer as a Strands agent with read tools only and one review comment for each head
* The run job: it starts the workflow agent in a sandbox of the trust domain with its providers and policy, waits for GET /ping through the port forward, sends the input in the body of POST /invocations, prints the answer, and deletes the sandbox, also on an error and on SIGTERM. The sweep deletes each run sandbox past its time limit. The image holds the OpenShell CLI 0.1.7
* The run receiver: POST /runs with a token, an agent name from the list in git, an input and the key of the run. It makes one Job from the template of that agent, changes nothing else of it, and gives back the answer. A second call with the same key gets the same run. GET /runs/{key} gives the state of a run
* The provider profiles of the Renovate reviewer (examples/providers/), the service names of the platform in the examples, and the whole run on a kind cluster in CI: the run receiver, a Job from the template, a real sandbox with its providers, the delete on SIGTERM and the sweep
