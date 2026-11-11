#!/bin/sh
# The providers of the test cluster, as the ops repository makes them for the
# workflow trust domain. It runs in the setup Job (tests/kind/manifests.py), in
# the image under test, with the client certificate of the sandbox gateway.
# The credentials are the test values of tests/support, not secrets.
set -eu
dir="$HOME/.config/openshell/gateways/kind/mtls"
mkdir -p "$dir"
cp /mtls/ca.crt /mtls/tls.crt /mtls/tls.key "$dir/"
chmod 0600 "$dir"/*
osh() { openshell --color never -g kind "$@"; }

echo "== gateway add $GATEWAY_URL"
openshell --color never gateway add "$GATEWAY_URL" --local --name kind

python3 /setup/profiles.py /setup /tmp/profiles "$FAKE_GITHUB_HOST" "$FAKE_GITHUB_PORT"
for f in /tmp/profiles/*.yaml; do
  echo "== profile lint and import: $f"
  osh profile lint -f "$f"
  osh profile import -f "$f"
done
osh profile list

echo "== providers"
osh provider create --name model-gateway --type workflow-model-gateway --credential "MODEL_GATEWAY_KEY=$MODEL_KEY"
osh provider create --name github-reviewer --type workflow-github-reviewer --credential "GITHUB_TOKEN=$GITHUB_KEY"
osh provider list
