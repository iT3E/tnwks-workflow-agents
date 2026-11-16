#!/bin/sh
# The providers of the test cluster, made by the providers command of the
# image under test, as the CronJob workflow-providers of the ops repository
# makes them for the workflow trust domain. It runs in the setup Job
# (tests/kind/manifests.py), with the client certificate of the sandbox
# gateway, against the real OpenShell CLI and gateway. The GitHub App is the
# fake of tests/support (in the fake GitHub), and its key is made here. The
# credentials are the test values of tests/support, not secrets.
#
# It runs the command twice: the first run imports the profiles and creates
# the providers, the second finds the profiles unchanged and updates the
# providers. Each run must end with "workflow-providers result=ok".
set -eu

python3 /setup/profiles.py /setup "$PROVIDER_PROFILES_DIR" "$FAKE_GITHUB_HOST" "$FAKE_GITHUB_PORT"
openssl genrsa -out "$GITHUB_APP_KEY_FILE" 2048 2>/dev/null

for round in first second; do
  echo "== providers, $round run"
  python3 /app/src/entrypoint.py providers 2>&1 | tee "/tmp/providers-$round.log"
  tail -n 1 "/tmp/providers-$round.log" | grep -qx 'workflow-providers result=ok'
done
grep -q '^profile workflow-model-gateway: imported$' /tmp/providers-first.log
grep -q '^provider github-reviewer: created$' /tmp/providers-first.log
grep -q '^profile workflow-model-gateway: unchanged$' /tmp/providers-second.log
grep -q '^provider github-reviewer: updated$' /tmp/providers-second.log
grep -q '^app-drift result=ok detail=none$' /tmp/providers-second.log
echo "== providers: both runs ok"
