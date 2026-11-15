"""The run job: a sandbox, the input in the body, the answer, and the delete, also when the run fails.

Two levels:

* RunJobLocal runs the run job of the image against a fake OpenShell CLI. The
  fake starts the real agent of the image as the "sandbox", with only the
  variables that the run job passes, so the whole run happens: the contract,
  the mock model, the fake GitHub, the answer, the delete.
* RunJobOnKind, in tests/kind/test_on_kind.py, runs the whole run on a kind
  cluster with the sandbox layer: the receiver, a Job from the template, a real
  sandbox gateway with its providers, a real sandbox. bin/image-test runs it in
  CI only, on the host of the job, after tests/kind/up.sh.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import support  # noqa: F401  (sets the import path)
from run_job import job, records, result, sweep
from run_job.openshell import OpenShell
from support import EXAMPLES, HERE, Process, base_env, fake_github, fake_sandboxes, mock_model
from support.cases import RenovateReviewerCase
from support.fake_sandboxes import FakeSandboxes

FAKE_CLI = os.path.join(HERE, "fake_openshell.py")


class Names(unittest.TestCase):
    def test_a_sandbox_name_holds_its_start_time(self):
        now = 1_794_000_000
        name = job.sandbox_name("renovate-review-7-3f1c2a9", now)
        self.assertRegex(name, job.NAME_PATTERN)
        self.assertLessEqual(len(name), job.MAX_NAME_LENGTH)
        self.assertLessEqual(len(job.sandbox_name("k", 36**7 - 1)), job.MAX_NAME_LENGTH)  # until 2050
        self.assertEqual(job.started_at(name), now)
        self.assertIsNone(job.started_at("my-own-sandbox"))

    def test_a_credential_cannot_go_to_the_sandbox_as_a_variable(self):
        for key in [
            "AGENT_ENV_GITHUB_TOKEN",
            "AGENT_ENV_MODEL_GATEWAY_KEY",
            "AGENT_ENV_DB_PASSWORD",
            "AGENT_ENV_lower",
            "AGENT_ENV_X",
        ]:
            env = {key: "x" if key != "AGENT_ENV_X" else "a\nb"}
            with self.subTest(key=key), self.assertRaises(job.ConfigError):
                job.agent_env(env, "renovate-reviewer")
        self.assertEqual(
            job.agent_env({"AGENT_ENV_MODEL_ID": "m", "OTHER": "y"}, "a"), {"AGENT_NAME": "a", "MODEL_ID": "m"}
        )


class RunJobLocal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "state"
        self.home = self.tmp / "home"
        self.home.mkdir()
        mtls = self.tmp / "mtls"
        mtls.mkdir()
        for n in ("ca.crt", "tls.crt", "tls.key"):
            (mtls / n).write_text("test file, not a certificate\n")
        cli = self.tmp / "openshell"
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
        cli.chmod(0o755)
        self.cli_path = str(cli)
        self.token_file = self.tmp / "token"
        self.token_file.write_text(fake_sandboxes.TOKEN)
        self.api = FakeSandboxes(self.state)
        self.api.__enter__()
        self.addCleanup(self.api.__exit__, None, None, None)
        self.case = RenovateReviewerCase()
        self.ctx = self.case.running()
        self.ctx.__enter__()
        self.input = self.case.good_input()
        self.env = base_env(
            HOME=str(self.home),
            AGENT_NAME="renovate-reviewer",
            RUN_KEY="renovate-review-7-3f1c2a9",
            RUN_INPUT=json.dumps(self.input),
            SANDBOX_IMAGE="ghcr.io/it3e/tnwks-workflow-agents:0.0.0-test",
            SANDBOX_POLICY=os.path.join(EXAMPLES, "sandbox-policy.yaml"),
            SANDBOX_PROVIDERS="model-gateway,github-reviewer",
            SANDBOX_MEMORY="512Mi",
            OPENSHELL_GATEWAY_URL="https://sandbox-gateway.test:8080",
            OPENSHELL_MTLS_DIR=str(mtls),
            OPENSHELL_BIN=str(cli),
            READY_TIMEOUT_SECONDS="30",
            RUN_TIMEOUT_SECONDS="60",
            AGENT_ENV_MODEL_GATEWAY_URL=self.case.model.url,
            AGENT_ENV_MODEL_ID="mock-model",
            AGENT_ENV_GITHUB_API_URL=self.case.github.url,
            AGENT_ENV_REVIEWER_REPOSITORIES=fake_github.REPO,
            FAKE_OPENSHELL_STATE=str(self.state),
            FAKE_PROVIDER_MODEL_GATEWAY_KEY=mock_model.KEY,
            FAKE_PROVIDER_GITHUB_TOKEN=fake_github.TOKEN,
            KUBE_API_URL=self.api.url,
            KUBE_TOKEN_FILE=str(self.token_file),
            POD_NAMESPACE=fake_sandboxes.NAMESPACE,
        )

    def tearDown(self):
        for pid_file in [*self.state.glob("*.pid"), *self.state.glob("*.fwd")]:  # left by a failed test
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.ctx.__exit__(None, None, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, timeout=120, **env):
        proc = Process(["run-job"], self.env | env)
        proc.proc.wait(timeout=timeout)
        out, err = proc.stop()
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertTrue(lines and lines[-1].startswith(result.PREFIX), f"no result line\n{out}\n{err[-3000:]}")
        return proc.proc.returncode, result.find(out), err

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def sandboxes_left(self) -> list[str]:
        return [p.stem for p in self.state.glob("*.pid")]

    def test_a_run_gives_the_answer_and_deletes_the_sandbox(self):
        code, res, err = self.run_job()
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["output"]["status"], "reviewed")
        self.assertTrue(res["sandbox_deleted"])
        self.assertEqual(self.sandboxes_left(), [])
        self.assertEqual(self.api.names(), [])
        self.assertEqual(len(self.case.github.writes()), 1)

        create = next(c for c in self.calls() if "create" in c)
        flat = " ".join(create)
        self.assertIn("--policy", create)
        self.assertEqual(
            [create[i + 1] for i, a in enumerate(create) if a == "--provider"], ["model-gateway", "github-reviewer"]
        )
        self.assertIn("AGENT_NAME=renovate-reviewer", create)
        self.assertIn("--no-auto-providers", create)
        self.assertEqual(create[create.index("--") + 1 :], job.AGENT_COMMAND)
        # The input goes in the body, never on the command line; no credential goes in at all.
        self.assertNotIn(fake_github.HEAD, flat)
        self.assertNotIn(mock_model.KEY, flat)
        self.assertNotIn(fake_github.TOKEN, flat)
        order = [c[c.index("-g") + 3] if "-g" in c else c[3] for c in self.calls()]
        self.assertEqual(order, ["add", "create", "start", "stop", "delete"])

    def test_an_error_of_the_agent_fails_the_run_and_deletes_the_sandbox(self):
        code, res, _ = self.run_job(AGENT_ENV_REVIEWER_REPOSITORIES="example-org/some-other-repo")
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["http_status"], 400)
        self.assertIn("does not review", res["error"])
        self.assertEqual(self.sandboxes_left(), [])

    def test_a_sandbox_that_never_answers_ping_is_deleted(self):
        code, res, _ = self.run_job(FAKE_OPENSHELL_MODE="no-serve", READY_TIMEOUT_SECONDS="3")
        self.assertEqual(code, 3)
        self.assertIn("/ping", res["error"])
        self.assertTrue(res["sandbox_deleted"])
        self.assertEqual(self.sandboxes_left(), [])

    def test_a_create_that_fails_is_a_failed_run(self):
        code, res, _ = self.run_job(FAKE_OPENSHELL_MODE="create-fails")
        self.assertEqual(code, 3)
        self.assertIn("refused", res["error"])
        self.assertTrue(res["sandbox_deleted"])

    def test_a_delete_that_fails_fails_the_job(self):
        code, res, _ = self.run_job(FAKE_OPENSHELL_MODE="delete-fails")
        self.assertEqual(code, 6)
        self.assertEqual(res["status"], "succeeded")
        self.assertFalse(res["sandbox_deleted"])

    def test_sigterm_deletes_the_sandbox(self):
        proc = Process(["run-job"], self.env | {"FAKE_OPENSHELL_MODE": "no-serve", "READY_TIMEOUT_SECONDS": "60"})
        deadline = time.monotonic() + 30
        while not self.sandboxes_left():
            self.assertLess(time.monotonic(), deadline, "the sandbox was not made")
            time.sleep(0.1)
        proc.proc.send_signal(signal.SIGTERM)
        proc.proc.wait(timeout=60)
        out, _ = proc.stop()
        self.assertEqual(proc.proc.returncode, 5)
        res = result.find(out)
        self.assertEqual(res["error"], "the run job was stopped")
        self.assertTrue(res["sandbox_deleted"])
        self.assertEqual(self.sandboxes_left(), [])
        self.assertEqual(self.api.names(), [])

    def test_a_missing_variable_is_a_config_error_before_any_sandbox(self):
        code, res, _ = self.run_job(SANDBOX_IMAGE="")
        self.assertEqual(code, 2)
        self.assertIn("SANDBOX_IMAGE", res["error"])
        self.assertEqual(self.calls(), [])

    def gateway_sandbox(self, name: str, created: float) -> None:
        """A sandbox that the gateway knows, with its record."""
        sleeper = subprocess.Popen(["sleep", "600"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        (self.state / f"{name}.pid").write_text(str(sleeper.pid))
        self.api.add(fake_sandboxes.record(name, created))

    def sweep(self, *args, **env) -> tuple[int, str]:
        proc = Process(["sweep", *args], self.env | {"SWEEP_MAX_AGE_SECONDS": "3600"} | env)
        proc.proc.wait(timeout=120)
        _, err = proc.stop()
        return proc.proc.returncode, err

    def test_the_sweep_deletes_old_run_sandboxes_only(self):
        now = int(time.time())
        old = job.sandbox_name("old-run", now - 7200)
        fresh = job.sandbox_name("fresh-run", now - 60)
        self.gateway_sandbox(old, now - 7200)
        self.gateway_sandbox(fresh, now - 60)
        code, err = self.sweep(FAKE_OPENSHELL_EXTRA="someone-elses")
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual(sorted(self.sandboxes_left()), [fresh])
        deletes = [c[-1] for c in self.calls() if "delete" in c]
        self.assertEqual(deletes, [old])
        self.assertEqual(self.api.names(), [f"default--{fresh}"])
        self.assertEqual(self.api.deletes(), [])  # the gateway removed the record: no call to the API

    def test_the_sweep_removes_a_record_that_its_gateway_delete_left(self):
        now = int(time.time())
        old = job.sandbox_name("old-run", now - 7200)
        self.gateway_sandbox(old, now - 7200)
        code, err = self.sweep(FAKE_OPENSHELL_MODE="delete-keeps-record")
        self.assertEqual(code, 1, err[-3000:])
        self.assertIn(f"{old}: the gateway answered the delete and left the Sandbox record; removed", err)
        self.assertEqual(self.api.names(), [])
        [call] = self.api.deletes()
        self.assertTrue(call["path"].endswith(f"/sandboxes/default--{old}"))
        self.assertEqual(call["body"]["propagationPolicy"], "Foreground")
        self.assertIn("uid", call["body"]["preconditions"])

    def test_the_sweep_removes_old_records_that_the_gateway_does_not_list_and_nothing_else(self):
        now = time.time()
        forgotten = job.sandbox_name("forgotten-run", now - 7200)
        young = job.sandbox_name("young-run", now - 60)  # the gateway does not list it yet: a live run
        new_record_old_name = job.sandbox_name("odd-run", now - 7200)
        self.api.add(fake_sandboxes.record(forgotten, now - 7200))
        self.api.add(fake_sandboxes.record(young, now - 60))
        self.api.add(fake_sandboxes.record(new_record_old_name, now - 60))  # young by its creationTimestamp
        self.api.add(fake_sandboxes.record("platform-probe", now - 7200))  # not a run name
        other = job.sandbox_name("other-run", now - 7200)
        labels = {"openshell.ai/sandbox-name": other, "openshell.ai/managed-by": "someone-else"}
        self.api.add(fake_sandboxes.record(other, now - 7200, labels=labels))  # not made by OpenShell
        code, err = self.sweep()
        self.assertEqual(code, 1, err[-3000:])  # the gateway lost a sandbox: the alert shows it
        self.assertIn(f"{forgotten}: a Sandbox record that the gateway does not list; removed through the API", err)
        self.assertEqual(
            self.api.names(),
            sorted(
                [f"default--{young}", f"default--{new_record_old_name}", "default--platform-probe", f"default--{other}"]
            ),
        )
        self.assertEqual(len(self.api.deletes()), 1)

    def test_the_sweep_takes_a_404_between_the_read_and_the_delete_as_gone(self):
        forgotten = job.sandbox_name("forgotten-run", time.time() - 7200)
        self.api.add(fake_sandboxes.record(forgotten, time.time() - 7200))
        self.api.delete_answer = 404
        code, err = self.sweep()
        self.assertEqual(code, 1)  # forgotten by the gateway, so the sweep still fails once
        self.assertIn("removed through the API", err)
        self.assertNotIn("not removed", err)
        self.assertEqual(self.api.names(), [])

    def test_the_sweep_leaves_a_record_that_changed_since_its_read(self):
        forgotten = job.sandbox_name("forgotten-run", time.time() - 7200)
        self.api.add(fake_sandboxes.record(forgotten, time.time() - 7200))
        self.api.delete_answer = 409
        code, err = self.sweep()
        self.assertEqual(code, 1)
        self.assertIn("changed between its read and its delete (409); left for the next sweep", err)
        self.assertEqual(self.api.names(), [f"default--{forgotten}"])

    def test_the_sweep_in_a_dry_run_removes_nothing(self):
        now = time.time()
        old = job.sandbox_name("old-run", now - 7200)
        forgotten = job.sandbox_name("forgotten-run", now - 7200)
        self.gateway_sandbox(old, now - 7200)
        self.api.add(fake_sandboxes.record(forgotten, now - 7200))
        code, err = self.sweep("--dry-run")
        self.assertEqual(code, 0, err[-3000:])
        self.assertRegex(err, rf"{old} is 720[0-9] s old, over 3600 s: would delete")
        self.assertIn(f"{forgotten}: the gateway does not list it", err)
        self.assertIn("would remove it through the API", err)
        self.assertEqual(len(self.api.names()), 2)
        self.assertEqual(self.api.deletes(), [])

    def test_the_sweep_starts_no_removal_it_has_no_time_to_end(self):
        forgotten = job.sandbox_name("forgotten-run", time.time() - 7200)
        self.api.add(fake_sandboxes.record(forgotten, time.time() - 7200))
        code, err = self.sweep(SWEEP_DEADLINE_SECONDS="1")  # less than the worst case of one sandbox
        self.assertEqual(code, 1)
        self.assertIn("the deadline of the sweep: 1 Sandbox records are left for the next sweep", err)
        self.assertEqual(self.api.deletes(), [])

    def test_a_failed_gateway_list_leaves_each_record_to_the_age_rule(self):
        # With no list from the gateway, no record is known to it: an old one is
        # removed through the API, and a young one (maybe a live run) stays.
        now = time.time()
        old = job.sandbox_name("old-run", now - 7200)
        young = job.sandbox_name("young-run", now - 60)
        self.gateway_sandbox(old, now - 7200)
        self.gateway_sandbox(young, now - 60)
        code, err = self.sweep(FAKE_OPENSHELL_MODE="list-fails")
        self.assertEqual(code, 1, err[-3000:])
        self.assertIn("the list of the gateway:", err)
        self.assertEqual(self.api.names(), [f"default--{young}"])
        self.assertEqual(len(self.api.deletes()), 1)

    def test_the_sweep_with_no_right_to_the_records_fails(self):
        old = job.sandbox_name("old-run", time.time() - 7200)
        self.gateway_sandbox(old, time.time() - 7200)
        self.api.fail_with = 403
        code, err = self.sweep()
        self.assertEqual(code, 1)
        self.assertIn("the list of the Sandbox records: list sandboxes: HTTP 403", err)

    def test_the_sweep_reports_a_record_stuck_in_deletion(self):
        # In process, with a clock that a sleep moves on, so that the waits take no time.
        clock = Clock()
        forgotten = job.sandbox_name("forgotten-run", time.time() - 7200)
        self.api.add(fake_sandboxes.record(forgotten, time.time() - 7200))
        self.api.stuck = True
        api = records.Records(
            base_url=self.api.url,
            namespace=fake_sandboxes.NAMESPACE,
            token_file=str(self.token_file),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        cli = OpenShell(self.cli_path, "workflows")
        os.environ["FAKE_OPENSHELL_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FAKE_OPENSHELL_STATE", None)
        with self.assertLogs("sweep", level="ERROR") as logs:
            code = sweep.Sweep(cli, api, 3600, 420, dry_run=False).run()
        self.assertEqual(code, 1)
        self.assertIn(f"still there {sweep.API_WAIT} s after its delete through the API", "\n".join(logs.output))
        self.assertGreaterEqual(clock.now, sweep.API_WAIT)
        self.assertEqual(len(self.api.deletes()), 1)  # one delete, then the wait


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class RunRecords(unittest.TestCase):
    def test_only_a_record_that_openshell_made_for_a_run_name_is_a_run_record(self):
        name = job.sandbox_name("k", time.time())
        self.assertTrue(records.is_run_record(fake_sandboxes.record(name)))
        self.assertFalse(records.is_run_record(fake_sandboxes.record("platform-probe")))
        self.assertFalse(records.is_run_record(fake_sandboxes.record("run-zzzzzzz-1")))
        theirs = {"openshell.ai/managed-by": "other", "openshell.ai/sandbox-name": name}
        self.assertFalse(records.is_run_record(fake_sandboxes.record(name, labels=theirs)))
        self.assertFalse(records.is_run_record({"metadata": {"name": f"default--{name}"}}))

    def test_the_age_of_a_record_is_that_of_its_younger_clock(self):
        now = time.time()
        rec = fake_sandboxes.record(job.sandbox_name("k", now - 7200), now - 100)
        self.assertLess(records.age_seconds(rec, now), 200)
        rec = fake_sandboxes.record(job.sandbox_name("k", now - 100), now - 7200)
        self.assertLess(records.age_seconds(rec, now), 200)
        rec["metadata"]["creationTimestamp"] = "not a time"
        self.assertIsNone(records.age_seconds(rec, now))

    def test_a_delete_refuses_a_record_that_is_not_a_run_record(self):
        api = records.Records(base_url="http://127.0.0.1:9", namespace="ns", token_file="/nonexistent")
        with self.assertRaises(records.RecordError):
            api.delete(fake_sandboxes.record("platform-probe"))


if __name__ == "__main__":
    unittest.main()
