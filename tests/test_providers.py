"""The providers command (`entrypoint.py providers`) against a fake OpenShell CLI and a fake GitHub App.

The command runs as a child process, as in the CronJob. The fake CLI keeps the
store of the gateway in files and keeps a hash of each credential, never the
value. The fake GitHub App checks the signature of each JWT with the public
key of a key pair that the test makes. The log lines are the ones that the
alerts and the runbook of tnwks-ops read, so the tests check them as text.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (sets the import path)
from support import EXAMPLES, HERE, Process, Server, base_env, fake_github, fake_github_app

FAKE_CLI = os.path.join(HERE, "fake_openshell.py")
MODEL_KEY = "test-model-gateway-key-not-a-secret"
PROFILES = ("workflow-github-reviewer", "workflow-model-gateway")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Providers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        home = self.tmp / "home"
        home.mkdir()
        mtls = self.tmp / "mtls"
        mtls.mkdir()
        for n in ("ca.crt", "tls.crt", "tls.key"):
            (mtls / n).write_text("test file, not a certificate\n")
        self.profiles = self.tmp / "profiles"
        shutil.copytree(os.path.join(EXAMPLES, "providers"), self.profiles)
        cli = self.tmp / "openshell"
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
        cli.chmod(0o755)
        key, public = self.tmp / "app.pem", self.tmp / "app.pub"
        subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
        subprocess.run(
            ["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True
        )
        self.github = fake_github_app.FakeGitHubApp(public_key=str(public))
        server = Server(self.github.app)
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.env = base_env(
            HOME=str(home),
            OPENSHELL_BIN=str(cli),
            OPENSHELL_GATEWAY_URL="https://sandbox-gateway.test:8080",
            OPENSHELL_MTLS_DIR=str(mtls),
            PROVIDER_PROFILES_DIR=str(self.profiles),
            MODEL_GATEWAY_KEY=MODEL_KEY,
            GITHUB_APP_ID=fake_github_app.APP_ID,
            GITHUB_APP_KEY_FILE=str(key),
            GITHUB_REPOSITORY=fake_github.REPO,
            GITHUB_APP_PERMISSIONS="contents:read,pull_requests:write,checks:read",
            GITHUB_API_URL=server.url,
            FAKE_OPENSHELL_STATE=str(self.state),
        )

    def run_providers(self, **env) -> tuple[int, str, str]:
        proc = Process(["providers"], self.env | env)
        proc.proc.wait(timeout=60)
        out, err = proc.stop()
        return proc.proc.returncode, out, err

    def calls(self) -> list[list[str]]:
        path = self.state / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def stored(self, kind: str, name: str) -> dict:
        return json.loads((self.state / kind / f"{name}.json").read_text())

    def assertNoSecret(self, out: str, err: str):
        for secret in (fake_github.TOKEN, MODEL_KEY):
            self.assertNotIn(secret, out + err)
            self.assertNotIn(secret, json.dumps(self.calls()))

    def test_a_first_run_imports_the_profiles_and_creates_the_providers(self):
        code, out, err = self.run_providers()
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        for pid in PROFILES:
            self.assertIn(f"profile {pid}: imported", lines)
            digest = hashlib.sha256((self.profiles / f"{pid}.yaml").read_bytes()).hexdigest()
            self.assertEqual(self.stored("profiles", pid)["annotations"]["workflow-agents/source-sha256"], digest)
        self.assertIn("provider model-gateway: created", lines)
        self.assertIn("provider github-reviewer: created", lines)
        self.assertEqual(lines[-1], "workflow-providers result=ok")
        self.assertIn("app-drift result=ok detail=none", err.splitlines())
        self.assertRegex(err, r"(?m)^github app token: installation 77, example-org/example-gitops only, ")
        self.assertEqual(self.stored("providers", "model-gateway")["sha256"], sha(MODEL_KEY))
        self.assertEqual(self.stored("providers", "github-reviewer")["sha256"], sha(fake_github.TOKEN))
        self.assertEqual(
            self.github.minted,
            [
                {
                    "repositories": ["example-gitops"],
                    "permissions": {"contents": "read", "pull_requests": "write", "checks": "read"},
                }
            ],
        )
        self.assertNoSecret(out, err)
        # The gateway is added with the client certificate in the config of the CLI.
        self.assertIn(
            [
                "--color",
                "never",
                "gateway",
                "add",
                "https://sandbox-gateway.test:8080",
                "--local",
                "--name",
                "workflows",
            ],
            self.calls(),
        )
        self.assertNotIn(["profile", "lint"], [c[4:6] for c in self.calls()])

    def test_a_second_run_keeps_the_profiles_and_updates_the_providers(self):
        self.assertEqual(self.run_providers()[0], 0)
        code, out, err = self.run_providers()
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        for pid in PROFILES:
            self.assertIn(f"profile {pid}: unchanged", lines)
            self.assertEqual(self.stored("profiles", pid)["resource_version"], 1)
        self.assertIn("provider model-gateway: updated", lines)
        self.assertIn("provider github-reviewer: updated", lines)
        self.assertEqual(lines[-1], "workflow-providers result=ok")
        self.assertEqual(len(self.github.minted), 2)
        self.assertNoSecret(out, err)

    def test_a_changed_profile_is_updated_with_the_stored_resource_version(self):
        self.assertEqual(self.run_providers()[0], 0)
        path = self.profiles / "workflow-model-gateway.yaml"
        path.write_text(path.read_text() + "# a change in git\n")
        code, out, err = self.run_providers()
        self.assertEqual(code, 0, err)
        self.assertIn("profile workflow-model-gateway: updated", out.splitlines())
        self.assertIn("profile workflow-github-reviewer: unchanged", out.splitlines())
        stored = self.stored("profiles", "workflow-model-gateway")
        self.assertEqual(stored["resource_version"], 2)
        self.assertEqual(
            stored["annotations"]["workflow-agents/source-sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )

    def failed(self, mode: str, step: str, name: str, first: bool = False):
        if first:
            self.assertEqual(self.run_providers()[0], 0)
        minted = len(self.github.minted)
        code, out, err = self.run_providers(FAKE_OPENSHELL_MODE=mode)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(err.splitlines()[-1], f"workflow-providers result=failed step={step} name={name}")
        self.assertNotIn("result=ok", out)
        return minted, err

    def test_a_gateway_that_does_not_answer_fails_before_any_token(self):
        minted, err = self.failed("gateway-down", "gateway", "https://sandbox-gateway.test:8080")
        self.assertEqual(len(self.github.minted), minted)
        self.assertIn("connection refused", err)

    def test_each_failed_step_before_the_token_names_itself_and_mints_nothing(self):
        cases = [
            ("profile-import-fails", "profile-import", "workflow-github-reviewer", False),
            ("profile-export-fails", "profile-export", "workflow-github-reviewer", True),
            ("provider-create-fails", "provider-create", "model-gateway", False),
            ("provider-update-fails", "provider-update", "model-gateway", True),
        ]
        for mode, step, name, first in cases:
            with self.subTest(mode):
                shutil.rmtree(self.state, ignore_errors=True)
                minted, _ = self.failed(mode, step, name, first)
                self.assertEqual(len(self.github.minted), minted)

    def test_a_changed_profile_that_the_gateway_refuses_fails_profile_update(self):
        self.assertEqual(self.run_providers()[0], 0)
        path = self.profiles / "workflow-github-reviewer.yaml"
        path.write_text(path.read_text() + "# a change in git\n")
        self.failed("profile-update-fails", "profile-update", "workflow-github-reviewer")

    def test_an_app_with_more_than_git_asks_logs_drift_and_still_gets_a_scoped_token(self):
        self.github.installation_permissions["administration"] = "write"
        self.github.repository_selection = "all"
        code, out, err = self.run_providers()
        self.assertEqual(code, 0, err)
        self.assertIn(
            "app-drift result=drift detail=extra:installation:administration,extra:installation:repository_selection",
            err.splitlines(),
        )
        self.assertEqual(
            self.github.minted[-1]["permissions"], {"contents": "read", "pull_requests": "write", "checks": "read"}
        )
        self.assertEqual(out.splitlines()[-1], "workflow-providers result=ok")

    def test_an_app_with_less_than_git_asks_fails_github_token_and_keeps_the_old_provider(self):
        self.assertEqual(self.run_providers()[0], 0)
        before = self.stored("providers", "github-reviewer")
        del self.github.app_permissions["checks"]
        code, out, err = self.run_providers()
        self.assertEqual(code, 1, out + err)
        lines = err.splitlines()
        self.assertIn("app-drift result=drift detail=missing:app:checks", lines)
        self.assertRegex(err, r"(?m)^github app token: the App has less than git asks \(app:checks\): no token")
        self.assertEqual(lines[-1], "workflow-providers result=failed step=github-token name=github-reviewer")
        self.assertIn("provider model-gateway: updated", out.splitlines())
        self.assertNotIn("provider github-reviewer", out)
        self.assertEqual(self.stored("providers", "github-reviewer"), before)
        self.assertEqual(len(self.github.minted), 1)

    def test_a_token_with_other_permissions_than_git_is_not_used(self):
        self.github.token_permissions = {
            "contents": "write",
            "pull_requests": "write",
            "checks": "read",
            "metadata": "read",
        }
        code, out, err = self.run_providers()
        self.assertEqual(code, 1, out + err)
        self.assertRegex(
            err, r"(?m)^github app token: the token does not have exactly the permissions of git \(token:contents\)"
        )
        self.assertEqual(
            err.splitlines()[-1], "workflow-providers result=failed step=github-token name=github-reviewer"
        )
        self.assertFalse((self.state / "providers" / "github-reviewer.json").exists())
        self.assertNoSecret(out, err)

    def test_a_wrong_key_fails_github_token(self):
        other = self.tmp / "other.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(other), "2048"], check=True, capture_output=True)
        code, out, err = self.run_providers(GITHUB_APP_KEY_FILE=str(other))
        self.assertEqual(code, 1, out + err)
        self.assertRegex(err, r"(?m)^github app token: GET /app: 401")
        self.assertEqual(
            err.splitlines()[-1], "workflow-providers result=failed step=github-token name=github-reviewer"
        )

    def test_a_bad_permission_list_fails_github_token(self):
        code, out, err = self.run_providers(GITHUB_APP_PERMISSIONS="contents:admin")
        self.assertEqual(code, 1, out + err)
        self.assertRegex(
            err, r"(?m)^github app token: GITHUB_APP_PERMISSIONS: 'contents:admin' is not name:read or name:write"
        )
        self.assertEqual(self.github.minted, [])

    def test_no_gateway_url_is_a_config_error(self):
        env = dict(self.env)
        del env["OPENSHELL_GATEWAY_URL"]
        proc = Process(["providers"], env)
        proc.proc.wait(timeout=60)
        out, err = proc.stop()
        self.assertEqual(proc.proc.returncode, 2, out + err)
        self.assertEqual(
            err.splitlines()[-1], "workflow-providers result=failed step=config name=OPENSHELL_GATEWAY_URL"
        )
        self.assertEqual(self.calls(), [])

    def test_no_client_certificate_fails_the_gateway_step(self):
        shutil.rmtree(self.env["OPENSHELL_MTLS_DIR"])
        code, out, err = self.run_providers()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(
            err.splitlines()[-1], "workflow-providers result=failed step=gateway name=https://sandbox-gateway.test:8080"
        )

    def assertFailedCleanly(self, code: int, out: str, err: str, step: str, name: str):
        """Exit 1, the failed line last, no traceback, no secret."""
        self.assertEqual(code, 1, out + err)
        self.assertEqual(err.splitlines()[-1], f"workflow-providers result=failed step={step} name={name}")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("result=ok", out)
        self.assertNoSecret(out, err)

    def test_an_answer_of_200_that_is_not_json_fails_github_token(self):
        cases = [
            ("/app", b"<html>a proxy page</html>", "GET /app: the answer is not JSON"),
            ("/app", b"\xff\xfe", "GET /app: the answer is not JSON"),
            (
                f"/repos/{fake_github.REPO}/installation",
                b"[]",
                f"GET /repos/{fake_github.REPO}/installation: the answer is not a JSON object",
            ),
            (
                f"/app/installations/{fake_github_app.INSTALLATION_ID}/access_tokens",
                f"token={fake_github.TOKEN} and not JSON".encode(),
                f"POST /app/installations/{fake_github_app.INSTALLATION_ID}/access_tokens: the answer is not JSON",
            ),
        ]
        for path, raw, reason in cases:
            with self.subTest(path=path, raw=raw[:8]):
                self.github.raw = {path: raw}
                code, out, err = self.run_providers()
                self.assertFailedCleanly(code, out, err, "github-token", "github-reviewer")
                self.assertIn(f"github app token: {reason}", err.splitlines())

    def test_a_profile_that_is_not_utf8_fails_profile_import(self):
        (self.profiles / "workflow-model-gateway.yaml").write_bytes(b"id: workflow-model-gateway\n\xff\xfe\n")
        code, out, err = self.run_providers()
        self.assertFailedCleanly(code, out, err, "profile-import", "workflow-model-gateway")
        self.assertIn(
            "profile workflow-model-gateway.yaml: the file can not be read as UTF-8 text (UnicodeDecodeError)",
            err.splitlines(),
        )
        self.assertEqual(self.github.minted, [])

    def test_an_unexpected_error_fails_the_step_that_runs_and_logs_only_its_type(self):
        cases = [
            ("profile-list-not-utf8", "gateway", "https://sandbox-gateway.test:8080", False),
            ("profile-export-not-utf8", "profile-export", "workflow-github-reviewer", True),
            ("provider-get-not-utf8", "provider-create", "model-gateway", False),
        ]
        for mode, step, name, first in cases:
            with self.subTest(mode):
                shutil.rmtree(self.state, ignore_errors=True)
                if first:
                    self.assertEqual(self.run_providers()[0], 0)
                minted = len(self.github.minted)
                code, out, err = self.run_providers(FAKE_OPENSHELL_MODE=mode)
                self.assertFailedCleanly(code, out, err, step, name)
                self.assertEqual(
                    err.splitlines()[-2], f"workflow-providers: an unexpected UnicodeDecodeError in step {step}"
                )
                self.assertNotIn("0xff", err)
                self.assertEqual(len(self.github.minted), minted)

    def test_each_result_line_starts_the_line(self):
        _, out, err = self.run_providers(FAKE_OPENSHELL_MODE="gateway-down")
        for line in (out + err).splitlines():
            if "result=" in line:
                self.assertRegex(line, r"^[a-z][a-z0-9-]{0,39} result=[a-z]{1,20}(?: |$)")


if __name__ == "__main__":
    unittest.main()
