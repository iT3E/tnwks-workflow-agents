#!/usr/bin/env bash
# Print the state of the test cluster to the job log, when a step failed.
# Usage: tests/kind/dump.sh <work dir>
set -uo pipefail

work="${1:?usage: dump.sh <work dir>}"
[[ -s "$work/kubeconfig" ]] || exit 0
kx() { "$work/bin/kubectl" --kubeconfig "$work/kubeconfig" --context kind-tnwks-image-test "$@"; }
ns=agents-workflows

section() { echo; echo "==== $*"; }

section "pods"
kx get pods -A -o wide
section "events"
kx get events -A --sort-by=.lastTimestamp | tail -80
section "sandboxes"
kx get sandboxes.agents.x-k8s.io -A -o wide 2>/dev/null
section "jobs"
kx -n "$ns" get jobs -o wide
section "log: the setup Job"
kx -n "$ns" logs job/kind-setup --tail=150
section "log: the run receiver"
kx -n "$ns" logs deploy/run-receiver --tail=150
for job in $(kx -n "$ns" get jobs -o name | grep -v kind-setup); do
  section "log: $job"
  kx -n "$ns" logs "$job" --all-containers --tail=150
done
section "log: the OpenShell gateway"
kx -n "$ns" logs -l app.kubernetes.io/name=openshell --all-containers --tail=200 --prefix
section "log: the sandbox pods"
for pod in $(kx -n "$ns" get pods -o name | grep -v -e 'openshell-0' -e certgen -e run-receiver -e kind-setup); do
  kx -n "$ns" logs "$pod" --all-containers --tail=80 --prefix
done
section "log: the fakes (the last 40 lines each)"
kx -n ai logs deploy/litellm-app --tail=40
kx -n monitoring logs deploy/alloy --tail=40
kx -n test-support logs deploy/github --tail=40
