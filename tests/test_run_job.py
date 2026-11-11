"""The run job: a sandbox, the input in the body, the answer, and the delete, also when the run fails.

RunJobLocal runs the run job of the image against a fake OpenShell CLI. The
fake starts the real agent of the image as the "sandbox", with only the
variables that the run job passes, so the whole run happens: the contract, the
mock model, the fake GitHub, the answer, the delete.
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
from support import EXAMPLES, HERE, Process, base_env, fake_github, mock_model
from support.cases import RenovateReviewerCase

from run_job import job, result

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
            AGENT_ENV_MODEL_GATEWAY_URL=f"{self.case.model.url}/v1",
            AGENT_ENV_MODEL_ID="mock-model",
            AGENT_ENV_GITHUB_API_URL=self.case.github.url,
            AGENT_ENV_REVIEWER_REPOSITORIES=fake_github.REPO,
            FAKE_OPENSHELL_STATE=str(self.state),
            FAKE_PROVIDER_MODEL_GATEWAY_KEY=mock_model.KEY,
            FAKE_PROVIDER_GITHUB_TOKEN=fake_github.TOKEN,
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

    def test_a_missing_variable_is_a_config_error_before_any_sandbox(self):
        code, res, _ = self.run_job(SANDBOX_IMAGE="")
        self.assertEqual(code, 2)
        self.assertIn("SANDBOX_IMAGE", res["error"])
        self.assertEqual(self.calls(), [])

    def test_the_sweep_deletes_old_run_sandboxes_only(self):
        now = int(time.time())
        old = job.sandbox_name("old-run", now - 7200)
        fresh = job.sandbox_name("fresh-run", now - 60)
        self.state.mkdir(parents=True, exist_ok=True)
        sleepers = []
        for name in (old, fresh):
            sleepers.append(subprocess.Popen(["sleep", "600"], start_new_session=True))
            (self.state / f"{name}.pid").write_text(str(sleepers[-1].pid))
        self.addCleanup(lambda: [(s.kill(), s.wait()) for s in sleepers])
        proc = Process(["sweep"], self.env | {"SWEEP_MAX_AGE_SECONDS": "3600", "FAKE_OPENSHELL_EXTRA": "someone-elses"})
        proc.proc.wait(timeout=60)
        proc.stop()
        self.assertEqual(proc.proc.returncode, 0)
        self.assertEqual(sorted(self.sandboxes_left()), [fresh])
        deletes = [c[-1] for c in self.calls() if "delete" in c]
        self.assertEqual(deletes, [old])


if __name__ == "__main__":
    unittest.main()
