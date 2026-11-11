"""The whole run on a kind cluster with the sandbox layer. CI only (bin/image-test).

tests/kind/up.sh makes the cluster: Agent Sandbox, the OpenShell chart in
`agents-workflows` (the sandbox gateway of the trust domain), the providers of
the Renovate reviewer from examples/providers/, the run receiver, and the fakes
(the model gateway and the trace collector at the service names of the
platform, and GitHub in `test-support`). Each run here goes the whole path:
POST /runs to the receiver, a Job from the template, `openshell sandbox create`
with this image, the port forward, POST /invocations, the model and GitHub
calls through the proxy of the sandbox, the answer, and the delete.

It uses the standard library only, and each kubectl call names the kubeconfig
of the test cluster (IMAGE_TEST_KUBECONFIG).
"""

import base64
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manifests  # noqa: E402
from manifests import GITHUB_PORT, RECEIVER_URL, SUPPORT_NS, WORKFLOWS_NS, fake_github  # noqa: E402

IMAGE = os.environ.get("IMAGE_REF", "")
RESULT_PREFIX = "workflow-run-result: "

# Runs in the pod of the fake GitHub: one HTTP call, and its answer as JSON.
HTTP_CALL = """
import json, sys, urllib.error, urllib.request
method, url, token, body, timeout = sys.argv[1:6]
headers = {"content-type": "application/json"}
if token:
    headers["authorization"] = "Bearer " + token
req = urllib.request.Request(url, method=method, data=body.encode() if body else None, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=float(timeout)) as r:
        code, data = r.status, r.read()
except urllib.error.HTTPError as e:
    code, data = e.code, e.read()
except Exception as e:  # no answer: the test may try again
    code, data = 0, repr(e).encode()
print(json.dumps({"status": code, "body": data.decode()}))
"""


def kx(*args, check=True, timeout=600, input_text=None) -> subprocess.CompletedProcess:
    """kubectl on the test cluster only. It never uses the default kubeconfig."""
    kubeconfig = os.environ.get("IMAGE_TEST_KUBECONFIG")
    if not kubeconfig:
        raise RuntimeError("IMAGE_TEST_KUBECONFIG is not set: no kubectl call without the test cluster")
    kubectl = os.environ.get("IMAGE_TEST_KUBECTL", "kubectl")
    cmd = [kubectl, "--kubeconfig", kubeconfig, "--context", f"kind-{manifests.CLUSTER}", *args]
    done = subprocess.run(cmd, check=False, timeout=timeout, capture_output=True, text=True, input=input_text)
    if check and done.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args[:4])} exited {done.returncode}: {done.stderr[-2000:]}")
    return done


def http(method: str, url: str, token: str = "", body: dict | None = None, timeout: int = 60) -> tuple[int, object]:
    """One HTTP call from inside the cluster, from the pod of the fake GitHub.

    A call with no answer (a Service that is not ready yet) is tried again: each
    call of the test can be sent twice, and POST /runs keeps one run for one key.
    """
    for attempt in range(6):
        code, answer = _http(method, url, token, body, timeout)
        if code != 0:
            return code, answer
        print(f"no answer from {url} ({answer}), attempt {attempt + 1}")
        time.sleep(5)
    return code, answer


def _http(method: str, url: str, token: str, body: dict | None, timeout: int) -> tuple[int, object]:
    data = json.dumps(body) if body is not None else ""
    out = kx(
        "-n",
        SUPPORT_NS,
        "exec",
        "deploy/github",
        "--",
        "python3",
        "-c",
        HTTP_CALL,
        method,
        url,
        token,
        data,
        str(timeout),
        timeout=timeout + 60,
    ).stdout
    answer = json.loads(out.strip().splitlines()[-1])
    try:
        return answer["status"], json.loads(answer["body"])
    except ValueError:
        return answer["status"], answer["body"]


def seen(ns: str, deploy: str, port: int) -> list[dict]:
    """The requests that a fake got (fakes.py, GET /_test/requests)."""
    _, records = http("GET", f"http://{deploy}.{ns}.svc.cluster.local:{port}/_test/requests")
    return records


def run_sandboxes() -> list[str]:
    """Each Sandbox of the trust domain. In the test cluster, each one is of a run."""
    done = kx("-n", WORKFLOWS_NS, "get", "sandboxes.agents.x-k8s.io", "-o", "name", check=False)
    if done.returncode != 0:
        raise AssertionError(f"kubectl get sandboxes: {done.stderr}")
    return done.stdout.split()


def wait_for(what: str, check, timeout: float, every: float = 3):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:.0f} s: {what}")
        time.sleep(every)


def job_log(name: str, container: str = "run-job") -> str:
    return kx("-n", WORKFLOWS_NS, "logs", f"job/{name}", "-c", container, check=False).stdout


def wait_job(name: str, timeout: int = 300) -> str:
    """Wait until a Job ends. Returns Complete or Failed."""

    def ended():
        job = json.loads(kx("-n", WORKFLOWS_NS, "get", "job", name, "-o", "json").stdout)
        for c in (job.get("status") or {}).get("conditions") or []:
            if c.get("status") == "True" and c.get("type") in ("Complete", "Failed"):
                return c["type"]
        return ""

    return wait_for(f"the Job {name}", ended, timeout)


@unittest.skipUnless(os.environ.get("IMAGE_TEST_KUBECONFIG"), "no test cluster (tests/kind/up.sh, CI only)")
class RunJobOnKind(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        secret = json.loads(kx("-n", WORKFLOWS_NS, "get", "secret", manifests.TOKEN_SECRET, "-o", "json").stdout)
        cls.token = base64.b64decode(secret["data"]["token"]).decode()
        cls.input = {"repo": fake_github.REPO, "pr": fake_github.NUMBER, "head_sha": fake_github.HEAD}

    def post_run(self, key: str, payload: dict, wait: bool = True) -> tuple[int, dict]:
        body = {"agent": "renovate-reviewer", "input": payload, "key": key, "wait": wait}
        return http("POST", f"{RECEIVER_URL}/runs", self.token, body, timeout=1000)

    def test_1_a_whole_run_answers_through_the_receiver_and_leaves_no_sandbox(self):
        key = f"kind-review-{uuid.uuid4().hex[:8]}"
        start = time.monotonic()
        code, body = self.post_run(key, self.input)
        print(f"\nPOST /runs: {code} in {time.monotonic() - start:.0f} s: {json.dumps(body)[:1500]}")
        if isinstance(body, dict) and body.get("job"):
            print("the run job log:\n" + job_log(body["job"]))
        github = seen(SUPPORT_NS, "github", GITHUB_PORT)
        model = seen("ai", "litellm-app", 4000)
        traces = seen("monitoring", "alloy", 4318)
        print(f"fake GitHub: {github}\nfake model: {[(r['path'], r['auth']) for r in model]}\ncollector: {traces}")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "succeeded")
        self.assertTrue(body["created"])
        self.assertEqual(body["output"]["status"], "reviewed", body)
        self.assertEqual(body["output"]["verdict"], "needs-changes")

        # The credentials: each call of the sandbox reached the fakes with the
        # test credential, which only the providers hold.
        self.assertTrue(model, "no call reached the model gateway")
        self.assertEqual({r["auth"] for r in model}, {"key"})
        self.assertEqual({r["auth"] for r in github}, {"key"})
        posts = [r for r in github if r["method"] == "POST"]
        self.assertEqual(len(posts), 1, github)
        self.assertEqual(posts[0]["path"], f"/repos/{fake_github.REPO}/issues/{fake_github.NUMBER}/comments")
        # The spans left the sandbox through the network rule of the policy.
        self.assertTrue([r for r in traces if r["path"].startswith("/v1/traces")], "no span at the collector")

        wait_for("no run sandbox left", lambda: not run_sandboxes(), 180)

        # One run for one key: the same call gets the same run.
        code, again = self.post_run(key, self.input)
        self.assertEqual(code, 200, again)
        self.assertFalse(again["created"])
        self.assertEqual(again["output"], body["output"])
        code, state = http("GET", f"{RECEIVER_URL}/runs/{key}", self.token)
        self.assertEqual((code, state["status"]), (200, "succeeded"))

    def test_2_a_second_run_for_the_same_head_is_skipped(self):
        code, body = self.post_run(f"kind-again-{uuid.uuid4().hex[:8]}", self.input)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["output"]["status"], "skipped", body)
        posts = [r for r in seen(SUPPORT_NS, "github", GITHUB_PORT) if r["method"] == "POST"]
        self.assertEqual(len(posts), 1, "a second comment for the same head")
        wait_for("no run sandbox left", lambda: not run_sandboxes(), 180)

    def test_3_a_stopped_run_job_deletes_its_sandbox_and_the_sweep_finds_it(self):
        payload = dict(self.input, pr=99)  # the fake model waits on this pull request
        key = f"kind-stop-{uuid.uuid4().hex[:8]}"
        code, body = self.post_run(key, payload, wait=False)
        self.assertEqual(code, 202, body)
        job = body["job"]

        def waiting():
            code, state = http("GET", f"{RECEIVER_URL}/runs/{key}", self.token)
            if code != 202:
                raise AssertionError(f"the run ended before its model call: {code} {state}\n{job_log(job)}")
            return [r for r in seen("ai", "litellm-app", 4000) if r["slow"]]

        wait_for("the model call of the run", waiting, 600, every=5)
        sandboxes = run_sandboxes()
        print(f"\nduring the run: {sandboxes}")
        self.assertEqual(len(sandboxes), 1, sandboxes)

        # The sweep lists the run sandboxes by their label and finds this one.
        sweep = f"sweep-{uuid.uuid4().hex[:6]}"
        kx("apply", "-f", "-", input_text=json.dumps(manifests.sweep(IMAGE, sweep, max_age=0, dry_run=True)))
        self.assertEqual(wait_job(sweep), "Complete", job_log(sweep, "sweep"))
        log = job_log(sweep, "sweep")
        print(f"the sweep (dry run):\n{log}")
        self.assertIn("would delete", log)

        # SIGTERM to the run job, as at the time limit of the Job: it deletes its sandbox.
        kx("-n", WORKFLOWS_NS, "delete", "pod", "-l", f"batch.kubernetes.io/job-name={job}", "--timeout=200s")
        wait_for("no run sandbox left", lambda: not run_sandboxes(), 180)
        self.assertEqual(wait_job(job), "Failed")
        code, state = http("GET", f"{RECEIVER_URL}/runs/{key}", self.token)
        print(f"GET /runs after the stop: {code} {state}")
        self.assertEqual(state["status"], "failed", state)


if __name__ == "__main__":
    unittest.main()
