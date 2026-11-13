"""The run receiver: a token, the list of agents, a Job from a template, and one run for one key.

The receiver runs in process, against a fake of the Kubernetes API. The tests
read each Job that it makes and check that a caller changes nothing in it but
the name, the key and the input.
"""

import copy
import json
import os
import tempfile
import threading
import time
import unittest

import support  # noqa: F401  (sets the import path)
from support import EXAMPLES, Asgi, fake_kube

import httpx
import yaml

from run_job import result
from run_receiver import app as receiver_app
from run_receiver import config
from run_receiver.kube import Kube

TOKEN = "test-run-receiver-token-0123456789abcdef"
NAMESPACE = "agents-workflows"
AGENT = "renovate-reviewer"
EXAMPLE = os.path.join(EXAMPLES, "agents.yaml")
INPUT = {"repo": "example-org/example-gitops", "pr": 7, "head_sha": "3f1c2a9b8d7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"}


def result_log(**fields) -> str:
    return "INFO run_job: starting\n" + result.line(fields) + "\n"


class ReceiverTest(unittest.TestCase):
    record_seconds = 0.0  # the loop that keeps results: off, but in KeptResult

    def setUp(self):
        self.kube = fake_kube.FakeKube().__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        token_file = os.path.join(self.tmp.name, "token")
        with open(token_file, "w") as f:
            f.write(fake_kube.TOKEN)
        self.templates = config.load(EXAMPLE)
        receiver = receiver_app.Receiver(
            TOKEN,
            self.templates,
            Kube(self.kube.url, token_file, "/no/ca.crt"),
            NAMESPACE,
            poll_seconds=0.05,
            record_seconds=self.record_seconds,
        )
        self.app = Asgi(receiver_app.create_app(receiver)).__enter__()
        self.http = httpx.Client(base_url=self.app.url, timeout=30, headers={"authorization": f"Bearer {TOKEN}"})

    def tearDown(self):
        self.http.close()
        self.app.__exit__(None, None, None)
        self.kube.__exit__(None, None, None)
        self.tmp.cleanup()

    def post(self, **body):
        body.setdefault("agent", AGENT)
        body.setdefault("input", INPUT)
        body.setdefault("key", "renovate-review-7-3f1c2a9")
        return self.http.post("/runs", json=body)

    def finish_soon(self, key, delay=0.3, **kwargs):
        threading.Timer(delay, lambda: self.kube.finish(receiver_app.job_name(key), **kwargs)).start()


class Token(ReceiverTest):
    def test_health_needs_no_token(self):
        self.assertEqual(httpx.get(f"{self.app.url}/healthz").status_code, 200)

    def test_a_call_with_no_token_or_a_wrong_token_fails(self):
        for headers in [
            {},
            {"authorization": "Bearer wrong"},
            {"authorization": TOKEN},
            {"authorization": f"Basic {TOKEN}"},
            {"authorization": f"Bearer {TOKEN}x"},
        ]:
            with self.subTest(headers=headers):
                r = httpx.post(
                    f"{self.app.url}/runs",
                    headers=headers,
                    json={"agent": AGENT, "input": INPUT, "key": "k1", "wait": False},
                )
                self.assertEqual(r.status_code, 401)
                r = httpx.get(f"{self.app.url}/runs/k1", headers=headers)
                self.assertEqual(r.status_code, 401)
        self.assertEqual(self.kube.jobs, {})

    def test_a_short_token_is_refused_at_the_start(self):
        with self.assertRaises(config.ConfigError):
            receiver_app.Receiver("short", self.templates, None, NAMESPACE)


class AgentList(ReceiverTest):
    def test_an_agent_not_in_the_list_fails(self):
        r = self.post(agent="some-other-agent", wait=False)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.kube.jobs, {})

    def test_bodies_that_are_not_valid(self):
        for body, code in [
            ({"key": "has space"}, 400),
            ({"key": ""}, 400),
            ({"key": "x" * 201}, 400),
            ({"agent": ""}, 400),
            ({"input": "text"}, 400),
            ({"input": [1]}, 400),
            ({"wait": "yes"}, 400),
            ({"image": "evil:latest"}, 400),
            ({"input": {"data": "x" * 300_000}}, 413),
        ]:
            with self.subTest(body=list(body)):
                self.assertEqual(self.post(**body).status_code, code)
        self.assertEqual(self.kube.jobs, {})


class JobFromTemplate(ReceiverTest):
    def test_the_job_is_the_template_with_the_name_key_and_input_only(self):
        r = self.post(key="key-1", wait=False)
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(r.json()["status"], "running")
        self.assertTrue(r.json()["created"])
        [job] = self.kube.creates()
        name = receiver_app.job_name("key-1")
        self.assertEqual(job["metadata"]["name"], name)
        self.assertEqual(job["metadata"]["namespace"], NAMESPACE)
        self.assertEqual(job["metadata"]["annotations"]["workflow-agents/run-key"], "key-1")
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["RUN_KEY"], "key-1")
        self.assertEqual(json.loads(env["RUN_INPUT"]), INPUT)
        self.assertEqual(env["AGENT_NAME"], AGENT)

        # Take out what the receiver sets: the rest is the template, byte for byte.
        rest = copy.deepcopy(job)
        for k in ("name", "namespace", "annotations"):
            rest["metadata"].pop(k)
        rest["metadata"]["labels"].pop("workflow-agents/agent")
        rest["spec"]["template"]["metadata"]["labels"].pop("workflow-agents/agent")
        rest["spec"]["template"]["spec"]["containers"][0]["env"] = [
            e
            for e in rest["spec"]["template"]["spec"]["containers"][0]["env"]
            if e["name"] not in ("RUN_KEY", "RUN_INPUT")
        ]
        self.assertEqual(rest, self.templates[AGENT])

    def test_a_template_that_breaks_a_rule_is_refused(self):
        with open(EXAMPLE) as f:
            base = yaml.safe_load(f)

        def changed(fn):
            data = copy.deepcopy(base)
            fn(data["agents"][AGENT]["job"])
            return data

        def pod(job):
            return job["spec"]["template"]["spec"]

        cases = {
            "name set": changed(lambda j: j["metadata"].__setitem__("name", "x")),
            "namespace set": changed(lambda j: j["metadata"].__setitem__("namespace", "default")),
            "retries": changed(lambda j: j["spec"].__setitem__("backoffLimit", 2)),
            "no time limit": changed(lambda j: j["spec"].pop("activeDeadlineSeconds")),
            "restarts": changed(lambda j: pod(j).__setitem__("restartPolicy", "OnFailure")),
            "second container": changed(lambda j: pod(j)["containers"].append({"name": "x", "image": "x"})),
            "wrong agent name": changed(lambda j: pod(j)["containers"][0]["env"][0].__setitem__("value", "other")),
            "sets the input": changed(
                lambda j: pod(j)["containers"][0]["env"].append({"name": "RUN_INPUT", "value": "{}"})
            ),
            "not a job": changed(lambda j: j.__setitem__("kind", "Pod")),
        }
        unknown = copy.deepcopy(base)
        unknown["agents"]["not-in-the-image"] = unknown["agents"].pop(AGENT)
        cases["agent not in the image"] = unknown
        cases["empty list"] = {"agents": {}}
        for label, data in cases.items():
            with self.subTest(case=label), self.assertRaises(config.ConfigError):
                config.parse(yaml.safe_dump(data))


class OneRunForOneKey(ReceiverTest):
    def test_a_second_call_with_the_same_key_makes_no_second_job(self):
        first = self.post(key="key-2", wait=False)
        second = self.post(key="key-2", wait=False)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["job"], second.json()["job"])
        self.assertEqual(len(self.kube.jobs), 1)

    def test_the_same_key_with_a_different_input_is_a_conflict(self):
        self.post(key="key-3", wait=False)
        r = self.post(key="key-3", input=dict(INPUT, pr=8), wait=False)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.kube.jobs), 1)

    def test_a_call_waits_for_the_answer_and_a_second_call_gets_the_same_answer(self):
        self.post(key="key-4", wait=False)
        self.finish_soon("key-4", log=result_log(status="succeeded", output={"status": "reviewed", "verdict": "ready"}))
        started = time.monotonic()
        r = self.post(key="key-4")
        self.assertGreater(time.monotonic() - started, 0.2)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "succeeded")
        self.assertEqual(r.json()["output"], {"status": "reviewed", "verdict": "ready"})
        again = self.post(key="key-4")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["created"])
        self.assertEqual(again.json()["output"], r.json()["output"])
        self.assertEqual(len(self.kube.jobs), 1)
        got = self.http.get("/runs/key-4")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["output"], r.json()["output"])

    def test_a_failed_run_is_a_502_with_its_error(self):
        self.post(key="key-5", wait=False)
        self.kube.finish(
            receiver_app.job_name("key-5"),
            "Failed",
            "BackoffLimitExceeded",
            result_log(status="failed", error="no answer on /ping in 180 s"),
        )
        r = self.post(key="key-5")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "no answer on /ping in 180 s")

    def test_a_run_at_its_time_limit_is_a_504(self):
        self.post(key="key-6", wait=False)
        self.kube.finish(
            receiver_app.job_name("key-6"),
            "Failed",
            "DeadlineExceeded",
            result_log(status="failed", error="the run job was stopped"),
        )
        r = self.post(key="key-6")
        self.assertEqual(r.status_code, 504)
        self.assertEqual(r.json()["status"], "timed_out")

    def test_a_job_with_no_result_line_is_a_failure(self):
        self.post(key="key-7", wait=False)
        self.kube.finish(receiver_app.job_name("key-7"), "Complete", log="no result here\n")
        r = self.post(key="key-7")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"], "the run job gave no result")

    def test_get_of_an_unknown_key(self):
        self.assertEqual(self.http.get("/runs/never-used").status_code, 404)

    def test_a_cluster_api_error_is_a_503(self):
        self.kube.fail_with = 500
        self.assertEqual(self.post(key="key-8", wait=False).status_code, 503)


def wait_until(what, check, timeout=10):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError(f"no {what} in {timeout} s")
        time.sleep(0.05)


class KeptResult(ReceiverTest):
    """The pod of an ended Job goes long before the Job (the pod GC): the answer stays."""

    record_seconds = 0.05
    OUTPUT = {"status": "skipped", "reason": "the pull request is closed"}

    def kept(self, key):
        return self.kube.annotations(receiver_app.job_name(key)).get(receiver_app.RESULT_ANNOTATION)

    def test_the_answer_stays_when_the_pod_of_the_run_is_gone(self):
        key = "kept-1"
        self.finish_soon(key, log=result_log(status="succeeded", output=self.OUTPUT))
        first = self.post(key=key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["output"], self.OUTPUT)
        self.assertEqual(json.loads(self.kept(key))["output"], self.OUTPUT)
        self.kube.gc_pod(receiver_app.job_name(key))
        for r in (self.post(key=key), self.http.get(f"/runs/{key}")):
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "succeeded")
            self.assertEqual(r.json()["output"], self.OUTPUT)
            self.assertNotIn("output_kept", r.json())

    def test_the_loop_keeps_the_result_of_a_run_that_nobody_waits_for(self):
        key = "kept-2"
        self.assertEqual(self.post(key=key, wait=False).status_code, 202)
        self.kube.finish(receiver_app.job_name(key), log=result_log(status="succeeded", output=self.OUTPUT))
        wait_until("kept result", lambda: self.kept(key))
        self.kube.gc_pod(receiver_app.job_name(key))
        r = self.http.get(f"/runs/{key}")
        self.assertEqual((r.status_code, r.json()["output"]), (200, self.OUTPUT), r.text)

    def test_the_loop_keeps_a_failure_and_reads_each_job_once(self):
        key = "kept-3"
        self.post(key=key, wait=False)
        name = receiver_app.job_name(key)
        self.kube.finish(name, "Failed", "BackoffLimitExceeded", result_log(status="failed", error="no answer"))
        wait_until("kept result", lambda: self.kept(key))
        time.sleep(0.3)  # more rounds of the loop
        self.assertEqual(len(self.kube.patches()), 1)
        self.kube.gc_pod(name)
        r = self.http.get(f"/runs/{key}")
        self.assertEqual((r.status_code, r.json()["error"]), (502, "no answer"), r.text)

    def test_the_loop_leaves_a_running_job(self):
        self.post(key="kept-4", wait=False)
        time.sleep(0.3)
        self.assertEqual(self.kube.patches(), [])

    def test_a_result_that_is_too_large_is_kept_without_its_output(self):
        key = "kept-5"
        big = {"status": "reviewed", "text": "x" * (receiver_app.RESULT_MAX_BYTES + 10)}
        self.finish_soon(key, log=result_log(status="succeeded", output=big, seconds=12.5))
        first = self.post(key=key)
        self.assertEqual(first.json()["output"], big)
        kept = json.loads(self.kept(key))
        self.assertEqual(kept, {"status": "succeeded", "seconds": 12.5, "output_kept": False})
        self.kube.gc_pod(receiver_app.job_name(key))
        r = self.http.get(f"/runs/{key}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "succeeded")
        self.assertIs(r.json()["output_kept"], False)
        self.assertNotIn("output", r.json())


class ResultGone(ReceiverTest):
    """The pod went before the receiver read its log: the answer is the state of the Job."""

    def gone(self, key, condition, reason=""):
        self.post(key=key, wait=False)
        name = receiver_app.job_name(key)
        self.kube.finish(name, condition, reason, log="")
        self.kube.gc_pod(name)
        return self.http.get(f"/runs/{key}"), name

    def test_a_complete_job_is_a_success_with_no_output(self):
        r, name = self.gone("gone-1", "Complete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "succeeded")
        self.assertIs(r.json()["output_kept"], False)
        self.assertNotIn("output", r.json())
        self.assertNotIn("error", r.json())
        # Kept as gone, so a later call gives the same answer with no pod lookup.
        self.assertEqual(
            json.loads(self.kube.annotations(name)[receiver_app.RESULT_ANNOTATION]), {"output_kept": False}
        )
        before = len([q for q in self.kube.server.requests if "/pods" in q["path"]])
        again = self.post(key="gone-1")
        self.assertEqual(again.json() | {"created": None}, r.json() | {"created": None})
        self.assertEqual(len([q for q in self.kube.server.requests if "/pods" in q["path"]]), before)

    def test_a_failed_job_is_a_failure_that_says_the_result_is_gone(self):
        r, _ = self.gone("gone-2", "Failed", "BackoffLimitExceeded")
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(r.json()["status"], "failed")
        self.assertIs(r.json()["output_kept"], False)
        self.assertIn("no longer kept", r.json()["error"])

    def test_a_job_at_its_time_limit_is_timed_out(self):
        r, _ = self.gone("gone-3", "Failed", "DeadlineExceeded")
        self.assertEqual((r.status_code, r.json()["status"]), (504, "timed_out"), r.text)
        self.assertIs(r.json()["output_kept"], False)


class KeepFails(ReceiverTest):
    def test_a_refused_patch_still_answers_from_the_log(self):
        self.kube.refuse_patch = 422
        key = "refused-1"
        self.finish_soon(key, log=result_log(status="succeeded", output={"status": "reviewed"}))
        r = self.post(key=key)
        self.assertEqual((r.status_code, r.json()["output"]), (200, {"status": "reviewed"}), r.text)
        self.assertEqual(len(self.kube.patches()), 1)
        self.assertNotIn(receiver_app.RESULT_ANNOTATION, self.kube.annotations(receiver_app.job_name(key)))

    def test_the_log_of_a_pod_that_still_stops_is_not_kept(self):
        self.kube.pod_phase = "Running"
        key = "stopping-1"
        self.post(key=key, wait=False)
        self.kube.finish(receiver_app.job_name(key), "Failed", "DeadlineExceeded", log="INFO still stopping\n")
        r = self.http.get(f"/runs/{key}")
        self.assertEqual((r.status_code, r.json()["error"]), (504, "the run reached its time limit"), r.text)
        self.assertEqual(self.kube.patches(), [])


if __name__ == "__main__":
    unittest.main()
