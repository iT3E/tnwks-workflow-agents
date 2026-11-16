"""The fakes of the test cluster: the model gateway, GitHub and the trace collector.

Usage, in the image under test, in the cluster: fakes.py model|github|collector PORT

Each one is the fake of tests/support on 0.0.0.0:PORT, and answers
GET /_test/requests with a summary of each request it got, for the test.
tests/kind/manifests.py mounts this file and tests/support from a ConfigMap.

- model: the scripted model of the Renovate reviewer. It answers 401 unless the
  Authorization header holds the test key, so a 200 shows that the proxy of the
  sandbox put the key in: the sandbox only has a placeholder. A request for pull
  request #99 waits SLOW_SECONDS first, for the test of a stopped run.
- github: the fake GitHub for pull request 7, and a second one for pull request
  99. It answers 401 unless the header holds the test token. The calls of the
  GitHub App (support/fake_github_app.py), from the providers command of the
  setup Job, go to the fake App and are left out of /_test/requests, which
  shows the calls of the reviewer only.
- collector: keeps each OTLP body.
"""

import json
import sys
import time

import support  # noqa: F401  (the import path)
from support import Server, fake_github, fake_github_app, mock_model
from support.cases import REVIEW_TEXT

SLOW_NUMBER = 99
SLOW_SECONDS = 600


def header(handler, name: str) -> str:
    return handler.headers.get(name) or ""


def auth_kind(value: str, secret: str) -> str:
    if not value:
        return "none"
    if value == f"Bearer {secret}":
        return "key"
    if "openshell:resolve" in value:
        return "placeholder"
    return "other"


def model_app():
    inner = mock_model.make_app(mock_model.reviewer_script(REVIEW_TEXT, fake_github.DIFF_PATH))

    def app(handler, method, path, raw):
        if header(handler, "authorization") != f"Bearer {mock_model.KEY}":
            return 401, {"error": {"message": "fake model: not the test key"}}, None
        if f"pull request #{SLOW_NUMBER} " in raw.decode(errors="replace"):
            time.sleep(SLOW_SECONDS)
        return inner(handler, method, path, raw)

    return app, mock_model.KEY


def github_app():
    main = fake_github.FakeGitHub()
    slow = fake_github.FakeGitHub()
    slow.pull["number"] = SLOW_NUMBER
    seven = f"/{fake_github.NUMBER}"
    github_app_fake = fake_github_app.FakeGitHubApp()

    def app(handler, method, path, raw):
        if fake_github_app.is_app_path(path):
            return github_app_fake.app(handler, method, path, raw)
        for kind in ("pulls", "issues"):
            prefix = f"/repos/{fake_github.REPO}/{kind}/{SLOW_NUMBER}"
            if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
                return slow.app(handler, method, path.replace(f"/{SLOW_NUMBER}", seven, 1), raw)
        return main.app(handler, method, path, raw)

    return app, fake_github.TOKEN


def collector_app():
    def app(handler, method, path, raw):
        if method == "POST" and path.startswith("/v1/traces"):
            return 200, b"", {"content-type": "application/x-protobuf"}
        return 404, b"", None

    return app, ""


def summary(request: dict, secret: str) -> dict:
    headers = {k.lower(): v for k, v in request["headers"].items()}
    body = request["body"] or b""
    return {
        "method": request["method"],
        "path": request["path"].split("?", 1)[0],
        "auth": auth_kind(headers.get("authorization", ""), secret),
        "bytes": len(body),
        "slow": f"pull request #{SLOW_NUMBER} ".encode() in body,
        "effort": effort_of(body),
    }


def effort_of(body: bytes):
    try:
        return ((json.loads(body) or {}).get("output_config") or {}).get("effort")
    except (ValueError, AttributeError):
        return None


def main() -> int:
    kind, port = sys.argv[1], int(sys.argv[2])
    inner, secret = {"model": model_app, "github": github_app, "collector": collector_app}[kind]()

    def app(handler, method, path, raw):
        if path == "/_test/requests":
            seen = [
                r
                for r in handler.server.requests
                if not r["path"].startswith("/_test/") and not fake_github_app.is_app_path(r["path"])
            ]
            return 200, [summary(r, secret) for r in seen], None
        return inner(handler, method, path, raw)

    with Server(app, port=port, host="0.0.0.0") as srv:
        print(f"fake {kind} on 0.0.0.0:{srv.port}", flush=True)
        srv.thread.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
