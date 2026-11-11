"""The Renovate reviewer: it reads one pull request and writes one comment, and nothing more.

It gets read tools only. The code, not the model, posts the one comment of a
run. The gates (allowed repository, open, same head, no review yet) are code.
"""

import asyncio
import json
import os
import re
import unittest
from unittest import mock

import support  # noqa: F401  (sets the import path)
from support import fake_github, mock_model
from support.cases import REVIEW_TEXT, RenovateReviewerCase

from agents import renovate_reviewer
from agents.renovate_reviewer import agent as reviewer
from agents.renovate_reviewer.github import PullRequest
from agents.renovate_reviewer.tools import READ_ONLY_TOOLS
from contract import InputError

ALLOWED_PATHS = [
    ("GET", rf"^/repos/{fake_github.REPO}/pulls/{fake_github.NUMBER}(\?.*)?$"),
    ("GET", rf"^/repos/{fake_github.REPO}/pulls/{fake_github.NUMBER}/files(\?.*)?$"),
    ("GET", rf"^/repos/{fake_github.REPO}/contents/[^?]+\?ref={fake_github.HEAD}$"),
    ("GET", rf"^/repos/{fake_github.REPO}/issues/{fake_github.NUMBER}/comments(\?.*)?$"),
    ("GET", rf"^/repos/{fake_github.REPO}/commits/{fake_github.HEAD}/check-runs(\?.*)?$"),
    ("POST", rf"^/repos/{fake_github.REPO}/issues/{fake_github.NUMBER}/comments$"),
]


def run(case, payload):
    with mock.patch.dict(os.environ, case.env()):
        return asyncio.run(renovate_reviewer.invoke(payload))


class Review(unittest.TestCase):
    def setUp(self):
        self.case = RenovateReviewerCase()
        self.ctx = self.case.running()
        self.ctx.__enter__()
        self.gh, self.model = self.case.github, self.case.model

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def model_requests(self):
        return [json.loads(r["body"]) for r in self.model.requests]

    def test_a_review_posts_one_comment_with_the_verdict(self):
        out = run(self.case, self.case.good_input())
        self.assertEqual(out["status"], "reviewed")
        self.assertEqual(out["verdict"], "needs-changes")
        writes = self.gh.writes()
        self.assertEqual(len(writes), 1)
        body = json.loads(writes[0]["body"])["body"]
        self.assertIn(f"head={fake_github.HEAD}", body)
        self.assertIn("Verdict: `needs-changes`", body)
        self.assertIn("The owner decides.", body)
        self.assertIn("`server.port` moved to `service.port`", body)

    def test_the_model_gets_read_tools_only_and_the_key_of_the_gateway(self):
        run(self.case, self.case.good_input())
        for r, body in zip(self.model.requests, self.model_requests()):
            self.assertEqual(
                r["headers"].get("Authorization") or r["headers"].get("authorization"), f"Bearer {mock_model.KEY}"
            )
            names = sorted(t["function"]["name"] for t in body.get("tools", []))
            self.assertEqual(names, sorted(READ_ONLY_TOOLS))
        for name in READ_ONLY_TOOLS:
            self.assertNotRegex(name, r"merge|approve|push|label|comment_on|write|post")

    def test_each_github_call_is_on_an_allowed_path(self):
        run(self.case, self.case.good_input())
        self.assertTrue(self.gh.requests)
        for r in self.gh.requests:
            ok = any(r["method"] == m and re.match(p, r["path"]) for m, p in ALLOWED_PATHS)
            self.assertTrue(ok, f"{r['method']} {r['path']}")

    def test_a_comment_from_an_untrusted_author_does_not_get_to_the_model(self):
        run(self.case, self.case.good_input())
        seen = json.dumps(self.model_requests())
        self.assertIn("No change to the rendered manifests", seen)
        self.assertNotIn("UNTRUSTED-COMMENT", seen)

    def test_a_second_run_on_the_same_head_posts_nothing(self):
        run(self.case, self.case.good_input())
        calls = len(self.model.requests)
        out = run(self.case, self.case.good_input())
        self.assertEqual(out["status"], "skipped")
        self.assertIn("already", out["reason"])
        self.assertEqual(len(self.gh.writes()), 1)
        self.assertEqual(len(self.model.requests), calls)

    def test_a_head_that_moved_is_not_reviewed(self):
        self.gh.pull["head"]["sha"] = "0" * 40
        out = run(self.case, self.case.good_input())
        self.assertEqual(out["status"], "skipped")
        self.assertIn("head moved", out["reason"])
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.model.requests, [])

    def test_a_closed_pull_request_is_not_reviewed(self):
        self.gh.pull["state"] = "closed"
        out = run(self.case, self.case.good_input())
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.gh.writes(), [])
        self.assertEqual(self.model.requests, [])

    def test_a_repository_not_in_the_list_is_refused(self):
        payload = dict(self.case.good_input(), repo="example-org/other-repo")
        with self.assertRaises(InputError):
            run(self.case, payload)
        self.assertEqual(self.gh.requests, [])

    def test_inputs_that_are_not_valid(self):
        good = self.case.good_input()
        for bad in [
            {},
            dict(good, pr=0),
            dict(good, pr="7"),
            dict(good, pr=True),
            dict(good, head_sha="abc"),
            dict(good, repo="no-slash"),
            dict(good, repo="../x/y"),
        ]:
            with self.subTest(payload=bad), self.assertRaises(InputError):
                run(self.case, bad)


class Parts(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(reviewer.verdict_of(REVIEW_TEXT), "needs-changes")
        self.assertEqual(reviewer.verdict_of("**Verdict:** `one-way`\nx"), "one-way")
        self.assertEqual(reviewer.verdict_of("text\nverdict: Ready"), "ready")
        self.assertEqual(reviewer.verdict_of("Verdict: merge it"), "unclear")
        self.assertEqual(reviewer.verdict_of("no verdict line"), "unclear")

    def test_a_file_path_cannot_leave_the_repository(self):
        pr = PullRequest("http://127.0.0.1:9", "t", fake_github.REPO, 1)
        for path in ["../secrets", "/etc/passwd", "a/../../b", "a\\b", ""]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                pr.file_at(path, fake_github.HEAD)
        pr.close()

    def test_no_config_is_a_config_error(self):
        from contract import ConfigError

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
            reviewer.check_config()


if __name__ == "__main__":
    unittest.main()
