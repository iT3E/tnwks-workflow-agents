"""A fake of the Kubernetes API calls of the run receiver: Jobs, their pods and the pod log.

A test ends a Job with `finish(name, condition, reason, log)`, and deletes its
pod with `gc_pod(name)`, as the pod GC of a cluster does. A merge patch of a
Job sets its annotations, unless `refuse_patch` holds a status code (as an
admission policy that refuses it). The fake keeps each request, so a test can
count the creates and read the Job bodies.
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
        self.refuse_patch: int | None = None
        self.pod_phase = "Succeeded"  # the phase that finish() gives the pod
        self.phases: dict[str, str] = {}
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

    def patches(self) -> list[dict]:
        return [r for r in self.server.requests if r["method"] == "PATCH"]

    def annotations(self, name: str) -> dict:
        return self.jobs[name]["metadata"].get("annotations") or {}

    def finish(self, name: str, condition: str = "Complete", reason: str = "", log: str = "") -> None:
        with self.lock:
            job = self.jobs[name]
            job["status"] = {"conditions": [{"type": condition, "status": "True", "reason": reason}]}
            self.logs[name] = log
            self.phases[name] = self.pod_phase

    def start_pod(self, name: str, log: str = "") -> None:
        """The pod of the Job runs, with this log so far."""
        with self.lock:
            self.logs[name] = log
            self.phases[name] = "Running"

    def write(self, name: str, text: str) -> None:
        with self.lock:
            self.logs[name] = self.logs.get(name, "") + text

    def end_pod(self, name: str, phase: str = "Succeeded") -> None:
        with self.lock:
            self.phases[name] = phase

    def complete(self, name: str, condition: str = "Complete", reason: str = "") -> None:
        """The Job reads as ended, and its pod stays as it is (or gone)."""
        with self.lock:
            self.jobs[name]["status"] = {"conditions": [{"type": condition, "status": "True", "reason": reason}]}

    def gc_pod(self, name: str) -> None:
        """Delete the pod of the Job and so its log, as the pod GC of the cluster does."""
        with self.lock:
            self.logs.pop(name, None)
            self.phases.pop(name, None)

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
        if m and method == "GET":
            label = query.get("labelSelector", [""])[0]
            with self.lock:
                items = [j for j in self.jobs.values() if label in (j["metadata"].get("labels") or {})]
            return 200, {"items": json.loads(json.dumps(items))}, None
        if m and method == "PATCH" and m.group(2):
            if handler.headers.get("content-type") != "application/merge-patch+json":
                return 415, {"kind": "Status", "code": 415}, None
            if self.refuse_patch:
                return self.refuse_patch, {"kind": "Status", "code": self.refuse_patch}, None
            patch = json.loads(raw)
            if set(patch) != {"metadata"} or set(patch["metadata"]) != {"annotations"}:
                return 422, {"kind": "Status", "message": "the test allows an annotation patch only"}, None
            with self.lock:
                job = self.jobs.get(m.group(2))
                if not job:
                    return 404, {"kind": "Status", "code": 404}, None
                job["metadata"].setdefault("annotations", {}).update(patch["metadata"]["annotations"])
            return 200, job, None
        m = re.match(r"^/api/v1/namespaces/([^/]+)/pods$", p)
        if m and method == "GET":
            selector = query.get("labelSelector", [""])[0]
            job_name = selector.split("=", 1)[1] if selector.startswith("batch.kubernetes.io/job-name=") else ""
            if job_name in self.logs:
                phase = self.phases.get(job_name, self.pod_phase)
                state = {"running": {}} if phase == "Running" else {"terminated": {"exitCode": 0}}
                pod = {
                    "metadata": {"name": f"{job_name}-pod", "creationTimestamp": "2026-11-11T00:00:01Z"},
                    "status": {"phase": phase, "containerStatuses": [{"name": "run-job", "state": state}]},
                }
                return 200, {"items": [pod]}, None
            return 200, {"items": []}, None
        m = re.match(r"^/api/v1/namespaces/([^/]+)/pods/([^/]+)-pod/log$", p)
        if m and method == "GET":
            # With follow=true too: the log so far, and the end of the stream.
            if m.group(2) not in self.logs:
                return 404, {"kind": "Status", "code": 404}, None
            return 200, self.logs[m.group(2)].encode(), {"content-type": "text/plain"}
        return 404, {"kind": "Status", "code": 404}, None
