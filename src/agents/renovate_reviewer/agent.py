"""One run of the Renovate reviewer.

Input:  {"repo": "<owner>/<name>", "pr": <number>, "head_sha": "<40 hex>"}
Output: {"status": "reviewed" | "skipped", "repo", "pr", "head_sha",
         "verdict", "comment_url", "reason"}

The gates are code, not a request to the model: the repository must be in
REVIEWER_REPOSITORIES, the pull request must be open, and its head must still
be `head_sha`. A run that finds a review comment for the same head already
posts no second one, so a retry of a run is safe.

Environment:
    MODEL_GATEWAY_URL      the OpenAI-compatible base URL of the model gateway (with /v1)
    MODEL_GATEWAY_KEY      the key of the model gateway; a placeholder in a sandbox
    MODEL_ID               the model name at the gateway
    GITHUB_TOKEN           the GitHub credential; a placeholder in a sandbox
    GITHUB_API_URL         default https://api.github.com
    REVIEWER_REPOSITORIES  the repositories that the agent may review, comma separated
    REVIEWER_MAX_TURNS     the most model calls of one run, default 40
    MODEL_MAX_TOKENS       the most output tokens of one model call, default 8192
"""

import asyncio
import os
import re

from contract import ConfigError, InputError

from . import prompt
from .github import REPO_PATTERN, SHA_PATTERN, PullRequest, is_trusted
from .tools import make_tools

NAME = "renovate-reviewer"
MARKER = "<!-- workflow-agent: renovate-reviewer head={sha} -->"
MARKER_PATTERN = re.compile(r"<!-- workflow-agent: renovate-reviewer head=([0-9a-f]{40}) -->")
VERDICT_PATTERN = re.compile(r"^\s*\**\s*verdict\s*\**\s*:\s*\**\s*`?([a-z-]+)`?", re.IGNORECASE | re.MULTILINE)
FOOTER = "This review is advice from a workflow agent. It does not approve, merge or change a label. The owner decides."
REQUIRED = ("MODEL_GATEWAY_URL", "MODEL_GATEWAY_KEY", "MODEL_ID", "GITHUB_TOKEN", "REVIEWER_REPOSITORIES")


def check_config() -> None:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise ConfigError(f"{NAME}: missing environment variables: {', '.join(missing)}")


def allowed_repositories() -> set[str]:
    return {r.strip().lower() for r in os.environ.get("REVIEWER_REPOSITORIES", "").split(",") if r.strip()}


def parse_input(payload: dict) -> tuple[str, int, str]:
    repo, number, sha = payload.get("repo"), payload.get("pr"), payload.get("head_sha")
    if not isinstance(repo, str) or not REPO_PATTERN.match(repo):
        raise InputError("`repo` must be <owner>/<name>")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise InputError("`pr` must be a positive number")
    if not isinstance(sha, str) or not SHA_PATTERN.match(sha):
        raise InputError("`head_sha` must be 40 lowercase hex characters")
    if repo.lower() not in allowed_repositories():
        raise InputError(f"the agent does not review {repo}")
    return repo, number, sha


def verdict_of(text: str) -> str:
    m = VERDICT_PATTERN.search(text)
    if m and m.group(1).lower() in prompt.VERDICTS:
        return m.group(1).lower()
    return "unclear"


def comment_body(sha: str, verdict: str, review: str) -> str:
    return "\n".join(
        [
            MARKER.format(sha=sha),
            f"**Review of `{sha[:7]}`.** Verdict: `{verdict}`.",
            "",
            review.strip(),
            "",
            "---",
            f"_{FOOTER}_",
        ]
    )


def existing_review(pr: PullRequest, sha: str) -> dict | None:
    for c in pr.comments():
        m = MARKER_PATTERN.search(c.get("body") or "")
        if m and m.group(1) == sha and is_trusted(c):
            return c
    return None


def make_model():
    from strands.models.openai import OpenAIModel

    return OpenAIModel(
        client_args={"api_key": os.environ["MODEL_GATEWAY_KEY"], "base_url": os.environ["MODEL_GATEWAY_URL"]},
        model_id=os.environ["MODEL_ID"],
        params={"max_tokens": int(os.environ.get("MODEL_MAX_TOKENS", "8192"))},
    )


async def review(pr: PullRequest, repo: str, number: int, sha: str) -> str:
    from strands import Agent
    from strands.types.agent import Limits

    agent = Agent(
        model=make_model(),
        tools=make_tools(pr, sha),
        system_prompt=prompt.SYSTEM_PROMPT,
        callback_handler=None,
        name=NAME,
        trace_attributes={"workflow.agent": NAME},
    )
    result = await agent.invoke_async(
        prompt.task(repo, number, sha),
        limits=Limits(turns=int(os.environ.get("REVIEWER_MAX_TURNS", "40"))),
    )
    if str(result.stop_reason).startswith("limit_"):
        raise RuntimeError(f"the review stopped at a limit: {result.stop_reason}")
    text = str(result).strip()
    if not text:
        raise RuntimeError("the model gave no review text")
    return text


async def invoke(payload: dict) -> dict:
    check_config()
    repo, number, sha = parse_input(payload)
    pr = PullRequest(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"), os.environ["GITHUB_TOKEN"], repo, number
    )
    out = {"repo": repo, "pr": number, "head_sha": sha}
    try:
        p = await asyncio.to_thread(pr.pull)
        head = (p.get("head") or {}).get("sha")
        if p.get("state") != "open":
            return out | {"status": "skipped", "reason": f"the pull request is {p.get('state')}"}
        if head != sha:
            return out | {"status": "skipped", "reason": f"the head moved to {str(head)[:7]}"}
        found = await asyncio.to_thread(existing_review, pr, sha)
        if found:
            return out | {
                "status": "skipped",
                "reason": "a review of this head is already there",
                "comment_url": found.get("html_url"),
            }
        text = await review(pr, repo, number, sha)
        verdict = verdict_of(text)
        posted = await asyncio.to_thread(pr.post_comment, comment_body(sha, verdict, text))
        return out | {"status": "reviewed", "verdict": verdict, "comment_url": posted.get("html_url")}
    finally:
        pr.close()
