"""The tools of the Renovate reviewer. Each one reads the one pull request of the run.

The tools are made for each run and bound to its repository, its pull request
and its head commit, so the model cannot point a tool at a different one. No
tool writes. Each answer is text that the model reads as data, with a limit on
its length.
"""

import json

from strands import tool

from .github import PullRequest, clip, is_trusted

BODY_LIMIT = 40_000
PATCH_LIMIT = 30_000
FILE_LIMIT = 40_000
COMMENT_LIMIT = 30_000

READ_ONLY_TOOLS = (
    "pull_request_overview",
    "changed_files",
    "file_diff",
    "file_at_head",
    "trusted_comments",
    "check_runs",
)


def make_tools(pr: PullRequest, head_sha: str) -> list:
    @tool
    def pull_request_overview() -> str:
        """Read the title, the body (with the release notes that Renovate adds), the labels,
        the author, the branches and the state of the pull request under review."""
        p = pr.pull()
        return json.dumps(
            {
                "title": p.get("title"),
                "state": p.get("state"),
                "author": (p.get("user") or {}).get("login"),
                "labels": [label.get("name") for label in p.get("labels", [])],
                "base": (p.get("base") or {}).get("ref"),
                "head": (p.get("head") or {}).get("ref"),
                "head_sha": (p.get("head") or {}).get("sha"),
                "changed_files": p.get("changed_files"),
                "body": clip(p.get("body"), BODY_LIMIT),
            }
        )

    @tool
    def changed_files() -> str:
        """List each file that the pull request changes, with its status and its line counts."""
        return json.dumps(
            [
                {
                    "path": f.get("filename"),
                    "status": f.get("status"),
                    "additions": f.get("additions"),
                    "deletions": f.get("deletions"),
                }
                for f in pr.files()
            ]
        )

    @tool
    def file_diff(path: str) -> str:
        """Read the diff of one file that the pull request changes.

        Args:
            path: The path of the file, as changed_files lists it.
        """
        for f in pr.files():
            if f.get("filename") == path:
                return clip(f.get("patch") or "(no text diff: a binary or a very large file)", PATCH_LIMIT)
        return f"The pull request does not change {path}."

    @tool
    def file_at_head(path: str) -> str:
        """Read one file of the repository as it is at the head commit of the pull request.

        Args:
            path: The path of the file from the root of the repository.
        """
        try:
            return clip(pr.file_at(path, head_sha), FILE_LIMIT)
        except ValueError as e:
            return str(e)

    @tool
    def trusted_comments() -> str:
        """Read the comments on the pull request from the owner, members, collaborators and apps,
        for example the rendered diff of the manifests that CI posts. Other comments are left out."""
        return json.dumps(
            [
                {
                    "author": (c.get("user") or {}).get("login"),
                    "created_at": c.get("created_at"),
                    "body": clip(c.get("body"), COMMENT_LIMIT),
                }
                for c in pr.comments()
                if is_trusted(c)
            ]
        )

    @tool
    def check_runs() -> str:
        """List the checks of the head commit, with their state and their result."""
        return json.dumps(
            [
                {"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion")}
                for c in pr.check_runs(head_sha)
            ]
        )

    return [pull_request_overview, changed_files, file_diff, file_at_head, trusted_comments, check_runs]
