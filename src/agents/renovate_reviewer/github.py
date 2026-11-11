"""The GitHub calls of the Renovate reviewer, bound to one pull request.

Each call reads, except `post_comment`. The model never gets `post_comment`:
the code of the agent posts the one review comment of a run. No call merges,
approves, pushes, or changes a label.

In a sandbox, GITHUB_TOKEN holds a placeholder. The supervisor of the sandbox
puts the real credential in each call that goes to an address and a path that
the provider allows. The paths, for that provider:

    GET  /repos/{owner}/{repo}/pulls/{number}
    GET  /repos/{owner}/{repo}/pulls/{number}/files
    GET  /repos/{owner}/{repo}/contents/{path}?ref={head sha}
    GET  /repos/{owner}/{repo}/issues/{number}/comments
    GET  /repos/{owner}/{repo}/commits/{head sha}/check-runs
    POST /repos/{owner}/{repo}/issues/{number}/comments
"""

import re
from urllib.parse import quote

import httpx

REPO_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
API_VERSION = "2022-11-28"
MAX_PAGES = 10
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


class GitHubError(RuntimeError):
    pass


def clip(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"


class PullRequest:
    """The read calls and the one write call for `repo` and pull request `number`."""

    def __init__(self, api_url: str, token: str, repo: str, number: int, timeout: float = 30.0):
        if not REPO_PATTERN.match(repo):
            raise ValueError(f"not a repository name: {repo!r}")
        self.repo = repo
        self.number = number
        self._http = httpx.Client(
            base_url=api_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "workflow-agent-renovate-reviewer",
            },
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **kwargs) -> httpx.Response:
        r = self._http.get(path, **kwargs)
        if r.status_code >= 400:
            raise GitHubError(f"GET {path} answered {r.status_code}")
        return r

    def _pages(self, path: str) -> list:
        items: list = []
        for page in range(1, MAX_PAGES + 1):
            batch = self._get(path, params={"per_page": 100, "page": page}).json()
            items.extend(batch)
            if len(batch) < 100:
                break
        return items

    # ---- reads ----
    def pull(self) -> dict:
        return self._get(f"/repos/{self.repo}/pulls/{self.number}").json()

    def files(self) -> list[dict]:
        return self._pages(f"/repos/{self.repo}/pulls/{self.number}/files")

    def file_at(self, path: str, ref: str) -> str:
        if not path or path.startswith("/") or ".." in path.split("/") or "\\" in path:
            raise ValueError(f"not a repository path: {path!r}")
        r = self._get(
            f"/repos/{self.repo}/contents/{quote(path)}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        return r.text

    def comments(self) -> list[dict]:
        return self._pages(f"/repos/{self.repo}/issues/{self.number}/comments")

    def check_runs(self, sha: str) -> list[dict]:
        data = self._get(f"/repos/{self.repo}/commits/{sha}/check-runs", params={"per_page": 100}).json()
        return data.get("check_runs", [])

    # ---- the one write ----
    def post_comment(self, body: str) -> dict:
        r = self._http.post(f"/repos/{self.repo}/issues/{self.number}/comments", json={"body": body})
        if r.status_code >= 400:
            raise GitHubError(f"POST comment answered {r.status_code}")
        return r.json()


def is_trusted(comment: dict) -> bool:
    """A comment from the owner, a member, a collaborator, or an app. Each other comment is left out."""
    user = comment.get("user") or {}
    return comment.get("author_association") in TRUSTED_ASSOCIATIONS or user.get("type") == "Bot"
