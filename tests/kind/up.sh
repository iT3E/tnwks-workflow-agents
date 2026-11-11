#!/usr/bin/env bash
# Make the test cluster of the CI job: a kind cluster with the sandbox layer
# (Agent Sandbox and the OpenShell chart in the namespace of the workflow
# trust domain), the fakes, the providers, and the run receiver, all with the
# image under test. Adapted from the agent image repository, with its pins.
#
# Usage: tests/kind/up.sh <image ref> <work dir>
#
# It runs in CI only. The kubeconfig of the cluster is a file in the work dir,
# and each kubectl and helm call names it, so no call can reach another
# cluster. tests/kind/down.sh deletes the cluster.
set -euo pipefail

image="${1:?usage: up.sh <image ref> <work dir>}"
work="${2:?usage: up.sh <image ref> <work dir>}"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
cluster=tnwks-image-test
ns=agents-workflows

if [[ "${CI:-}" != "true" && "${IMAGE_TEST_KIND:-}" != "1" ]]; then
  echo "up.sh: the test cluster is made in CI only (CI=true or IMAGE_TEST_KIND=1)" >&2
  exit 1
fi

# The tools, at a release and the sha256 of its file, as in the agent image
# repository. Renovate does not see these three pins: update them by hand,
# with the checksum file of each release.
KIND_VERSION=v0.33.0
KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
HELM_VERSION=v3.22.1
HELM_SHA256=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea
KUBECTL_VERSION=v1.37.0
KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f

if [[ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]]; then
  echo "up.sh: the tools are pinned for linux x86_64 only" >&2
  exit 1
fi

mkdir -p "$work/bin" "$work/helm"
export KUBECONFIG="$work/kubeconfig"
export HELM_CACHE_HOME="$work/helm/cache" HELM_CONFIG_HOME="$work/helm/config" HELM_DATA_HOME="$work/helm/data"

fetch() { # fetch <url> <sha256> <file>
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet -
}

step() { echo "== $(date -u +%H:%M:%S) $*"; }

step "tools: kind $KIND_VERSION, helm $HELM_VERSION, kubectl $KUBECTL_VERSION"
fetch "https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-linux-amd64" "$KIND_SHA256" "$work/bin/kind"
fetch "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" "$KUBECTL_SHA256" "$work/bin/kubectl"
fetch "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" "$HELM_SHA256" "$work/helm.tar.gz"
tar -xzf "$work/helm.tar.gz" -C "$work/bin" --strip-components=1 linux-amd64/helm
chmod +x "$work/bin/kind" "$work/bin/kubectl" "$work/bin/helm"

kind() { "$work/bin/kind" "$@"; }
kx() { "$work/bin/kubectl" --kubeconfig "$work/kubeconfig" --context "kind-$cluster" "$@"; }
hx() { "$work/bin/helm" --kubeconfig "$work/kubeconfig" --kube-context "kind-$cluster" "$@"; }

# wait_job <namespace> <job> <seconds>: until the Job is Complete; its log, and a failure if it Failed.
wait_job() {
  local deadline=$((SECONDS + $3)) state
  while ((SECONDS < deadline)); do
    state="$(kx -n "$1" get job "$2" -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type} {end}')"
    case " $state " in
      *" Complete "*) kx -n "$1" logs "job/$2"; return 0 ;;
      *" Failed "*) kx -n "$1" logs "job/$2"; echo "up.sh: the Job $2 failed" >&2; return 1 ;;
    esac
    sleep 3
  done
  kx -n "$1" logs "job/$2" || true
  echo "up.sh: the Job $2 did not end in $3 s" >&2
  return 1
}

# The pins: each `FROM <ref> AS <name>` line of the two Dockerfiles.
pin() {
  awk -v n="$1" 'toupper($1) == "FROM" && toupper($3) == "AS" && $4 == n { print $2 }' \
    "$repo/Dockerfile" "$repo/.github/test-cluster/Dockerfile"
}
tag_of() { local name="${1%@*}"; echo "${name##*:}"; }
digest_of() { echo "${1#*@}"; }

node_image="$(pin kind-node)"
controller="$(pin agent-sandbox-controller)"
anchor="$(pin openshell-release)"
openshell_version="$(tag_of "$anchor")"
controller_version="$(tag_of "$controller")"
for v in "$node_image" "$controller" "$anchor"; do
  [[ "$v" == *@sha256:* ]] || { echo "up.sh: a pin has no digest: '$v'" >&2; exit 1; }
done
# The CLI in the image must be the release of the chart.
cli_version="$(awk -F= '/^ARG OPENSHELL_VERSION=/ { print $2 }' "$repo/Dockerfile")"
if [[ "${cli_version#v}" != "$openshell_version" ]]; then
  echo "up.sh: the CLI is $cli_version and the sandbox layer is $openshell_version: one release for both" >&2
  exit 1
fi

step "cluster: $node_image"
cat > "$work/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
EOF
kind create cluster --name "$cluster" --image "$node_image" --config "$work/kind.yaml" \
  --kubeconfig "$work/kubeconfig" --wait 180s

step "load the image under test: $image"
kind load docker-image "$image" --name "$cluster"

step "Agent Sandbox $controller_version"
curl -fsSL --retry 3 -o "$work/agent-sandbox.yaml" \
  "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${controller_version}/sandbox.yaml"
controller_name="${controller%@*}"
grep -q "image: ${controller_name}\$" "$work/agent-sandbox.yaml" || {
  echo "up.sh: the release manifest of Agent Sandbox does not name ${controller_name}" >&2
  exit 1
}
sed -i "s|image: ${controller_name}\$|image: ${controller}|" "$work/agent-sandbox.yaml"
kx apply --server-side -f "$work/agent-sandbox.yaml"
kx -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s

step "OpenShell $openshell_version in $ns (chart, gateway, supervisor; sandbox runtime $(digest_of "$anchor"))"
kx create namespace "$ns"
kx label namespace "$ns" pod-security.kubernetes.io/warn=restricted
hx install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version "$openshell_version" \
  --namespace "$ns" \
  --set server.telemetryEnabled=false \
  --set sandboxRuntime.image.digest="$(digest_of "$anchor")" \
  --wait --timeout 10m

step "the fakes: the model gateway (ai/litellm-app), the trace collector (monitoring/alloy), GitHub"
python3 "$repo/tests/kind/manifests.py" fakes "$image" | kx apply -f -
kx -n ai rollout status deploy/litellm-app --timeout=300s
kx -n monitoring rollout status deploy/alloy --timeout=300s
kx -n test-support rollout status deploy/github --timeout=300s

step "the providers of the Renovate reviewer (examples/providers)"
python3 "$repo/tests/kind/manifests.py" setup "$image" | kx apply -f -
wait_job "$ns" kind-setup 300

step "the run receiver"
docker run --rm --network none \
  -v "$repo/examples:/examples:ro" -v "$repo/tests:/tests:ro" -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python3 "$image" /tests/kind/render.py "$image" "http://github.test-support.svc.cluster.local:8000" \
  > "$work/agents.json"
python3 -c 'import secrets; print(secrets.token_hex(32))' > "$work/token"
python3 "$repo/tests/kind/manifests.py" receiver "$image" "$work/agents.json" "$work/token" | kx apply -f -
kx -n "$ns" rollout status deploy/run-receiver --timeout=300s

step "ready"
kx get pods -A -o wide
