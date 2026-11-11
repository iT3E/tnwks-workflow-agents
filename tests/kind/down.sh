#!/usr/bin/env bash
# Delete the test cluster that tests/kind/up.sh made.
# Usage: tests/kind/down.sh <work dir>
set -uo pipefail

work="${1:?usage: down.sh <work dir>}"
if [[ -x "$work/bin/kind" ]]; then
  "$work/bin/kind" delete cluster --name tnwks-image-test --kubeconfig "$work/kubeconfig"
fi
