"""A fake of the Kubernetes API calls of the run receiver: Jobs, their pods and the pod log.

A test ends a Job with `finish(name, condition, reason, log)`. The fake keeps
each request, so a test can count the creates and read the Job bodies.
"""

import json
import re
import threading
from urllib.parse import parse_qs, urlparse

from . import Server

TOKEN = "test-service-account-token"


class FakeKube:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.logs: dict[str, str] = {}
        self.fail_with: int | None = None
        self.lock = threading.Lock()
        self.server = Server(self.app)

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *exc):
        self.server.__exit__(*exc)

    @property
    def url(self) -> str:
        return self.server.url

    def creates(self) -> list[dict]:
        return [json.loads(r["body"]) for r in self.server.requests if r["method"] == "POST"]

    def finish(self, name: str, condition: str = "Complete", reason: str = "", log: str = "") -> None:
        with self.lock:
            job = self.jobs[name]
            job["status"] = {"conditions": [{"type": condition, "status": "True", "reason": reason}]}
            self.logs[name] = log

    def app(self, handler, method, path, raw):
        if handler.headers.get("authorization") != f"Bearer {TOKEN}":
            return 401, {"kind": "Status", "code": 401}, None
        if self.fail_with:
            return self.fail_with, {"kind": "Status", "code": self.fail_with}, None
        url = urlparse(path)
        p, query = url.path, parse_qs(url.query)
        m = re.match(r"^/apis/batch/v1/namespaces/([^/]+)/jobs(?:/([^/]+))?$", p)
        if m and method == "POST" and not m.group(2):
            job = json.loads(raw)
            name = job["metadata"]["name"]
            with self.lock:
                if name in self.jobs:
                    return 409, {"kind": "Status", "reason": "AlreadyExists", "code": 409}, None
                job["metadata"]["creationTimestamp"] = "2026-11-11T00:00:00Z"
                job["status"] = {}
                self.jobs[name] = job
            return 201, job, None
        if m and method == "GET" and m.group(2):
            job = self.jobs.get(m.group(2))
            return (200, job, None) if job else (404, {"kind": "Status", "code": 404}, None)
        m = re.match(r"^/api/v1/namespaces/([^/]+)/pods$", p)
        if m and method == "GET":
            selector = query.get("labelSelector", [""])[0]
            job_name = selector.split("=", 1)[1] if selector.startswith("batch.kubernetes.io/job-name=") else ""
            if job_name in self.logs:
                pod = {"metadata": {"name": f"{job_name}-pod", "creationTimestamp": "2026-11-11T00:00:01Z"}}
                return 200, {"items": [pod]}, None
            return 200, {"items": []}, None
        m = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)-pod/log$", p)
        if m and method == "GET":
            return 200, self.logs.get(m.group(2), "").encode(), {"content-type": "text/plain"}
        return 404, {"kind": "Status", "code": 404}, None
