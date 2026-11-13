"""One contract case for each workflow agent: its fakes, its environment, a good input and a check.

test_contract.py fails when an agent of the image has no case here, so a new
agent cannot ship without a test of its two paths.
"""

import contextlib

from . import fake_github, mock_model

ANSWER_MARKER = "MARKER-MODEL-ANSWER-8812"
REVIEW_TEXT = (
    "Verdict: needs-changes\n\n**What changes.** The chart goes from 1.4.0 to 2.0.0.\n\n"
    f"**Breaking changes.** The value `server.port` moved to `service.port`. {ANSWER_MARKER}\n\n"
    "**Changes this pull request needs.** Move `server.port` in the values.\n\n"
    "**Steps with no revert.** None.\n\n**Checks.** Build Success passed."
)


class RenovateReviewerCase:
    name = "renovate-reviewer"

    @contextlib.contextmanager
    def running(self):
        with (
            fake_github.FakeGitHub() as gh,
            mock_model.server(mock_model.reviewer_script(REVIEW_TEXT, fake_github.DIFF_PATH)) as model,
        ):
            self.github, self.model = gh, model
            yield self

    def env(self) -> dict[str, str]:
        return {
            "AGENT_NAME": self.name,
            "MODEL_GATEWAY_URL": self.model.url,
            "MODEL_GATEWAY_KEY": mock_model.KEY,
            "MODEL_ID": "mock-model",
            "GITHUB_TOKEN": fake_github.TOKEN,
            "GITHUB_API_URL": self.github.url,
            "REVIEWER_REPOSITORIES": fake_github.REPO,
        }

    def good_input(self) -> dict:
        return {"repo": fake_github.REPO, "pr": fake_github.NUMBER, "head_sha": fake_github.HEAD}

    def markers(self) -> list[str]:
        """Text of the run that must never get to a span."""
        return [fake_github.BODY_MARKER, ANSWER_MARKER, "UNTRUSTED-COMMENT"]

    def check_output(self, test, output: dict) -> None:
        test.assertEqual(output["status"], "reviewed")
        test.assertEqual(output["verdict"], "needs-changes")
        test.assertTrue(output["comment_url"])


CASES = {c.name: c for c in [RenovateReviewerCase()]}
