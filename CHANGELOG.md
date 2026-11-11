# Changelog

Notable changes to this repository, newest first. Each PR adds a Changie
fragment under `.changes/unreleased/`; merging to `main` releases them.

## v0.1.0 - 2026-11-11

### Added

* The image of the workflow agents, from the app template: the container contract (POST /invocations and GET /ping on port 8080, with the spans flushed before each answer and no text of a run in a span), and the Renovate reviewer as a Strands agent with read tools only and one review comment for each head
* The run job: it starts the workflow agent in a sandbox of the trust domain with its providers and policy, waits for GET /ping through the port forward, sends the input in the body of POST /invocations, prints the answer, and deletes the sandbox, also on an error and on SIGTERM. The sweep deletes each run sandbox past its time limit. The image holds the OpenShell CLI 0.1.7
* The run receiver: POST /runs with a token, an agent name from the list in git, an input and the key of the run. It makes one Job from the template of that agent, changes nothing else of it, and gives back the answer. A second call with the same key gets the same run. GET /runs/{key} gives the state of a run
* The provider profiles of the Renovate reviewer (examples/providers/), the service names of the platform in the examples, and the whole run on a kind cluster in CI: the run receiver, a Job from the template, a real sandbox with its providers, the delete on SIGTERM and the sweep
