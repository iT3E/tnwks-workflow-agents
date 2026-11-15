"""A fake of the Kubernetes API for the Sandbox records of one namespace.

The records are JSON files in <state>/records/, the same directory that the
fake OpenShell CLI (fake_openshell.py) writes: its `sandbox create` adds the
record of the sandbox, as the sandbox gateway does, and its `sandbox delete`
removes it, unless a mode of the CLI makes the gateway leave it. A test can add
a record by hand with `add()`, for example one that the gateway forgot.

The fake answers a list (with a label selector of `key=value` terms) and a
delete, which checks the uid precondition and the Foreground propagation. Its
attributes change one answer:

    fail_with      a status code for each call (403: no right)
    delete_answer  404 (the record went just before the delete) or 409 (a new record)
    stuck          a delete marks the record and never removes it (a finalizer that never ends)
"""

import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import Server

TOKEN = "test-sandbox-reader-token"
NAMESPACE = "agents-workflows"


def record(sandbox: str, created: float | None = None, labels: dict | None = None, name: str = "") -> dict:
    """A Sandbox record as OpenShell makes it: <workspace>--<sandbox>, with its labels."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if created is None else created))
    base = {
        "openshell.ai/managed-by": "openshell",
        "openshell.ai/sandbox-name": sandbox,
        "openshell.ai/sandbox-workspace": "default",
        "openshell.ai/sandbox-id": uuid.uuid4().hex,
    }
    return {
        "apiVersion": "agents.x-k8s.io/v1beta1",
        "kind": "Sandbox",
        "metadata": {
            "name": name or f"default--{sandbox}",
            "namespace": NAMESPACE,
            "uid": str(uuid.uuid4()),
            "creationTimestamp": stamp,
            "labels": base if labels is None else labels,
        },
    }


class FakeSandboxes:
    def __init__(self, state: Path):
        self.dir = Path(state) / "records"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fail_with: int | None = None
        self.delete_answer: int | None = None
        self.stuck = False
        self.server = Server(self.app)

    def __enter__(self):
        self.server.__enter__()
        return self

    def __exit__(self, *exc):
        self.server.__exit__(*exc)

    @property
    def url(self) -> str:
        return self.server.url

    def add(self, rec: dict) -> dict:
        (self.dir / f"{rec['metadata']['name']}.json").write_text(json.dumps(rec))
        return rec

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def deletes(self) -> list[dict]:
        return [
            {"path": r["path"], "body": json.loads(r["body"] or b"{}")}
            for r in self.server.requests
            if r["method"] == "DELETE"
        ]

    def _all(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.dir.glob("*.json"))]

    def app(self, handler, method, path, raw):
        if handler.headers.get("authorization") != f"Bearer {TOKEN}":
            return 401, {"kind": "Status", "code": 401}, None
        if self.fail_with:
            return self.fail_with, {"kind": "Status", "code": self.fail_with}, None
        url = urlparse(path)
        m = re.match(rf"^/apis/agents\.x-k8s\.io/v1beta1/namespaces/{NAMESPACE}/sandboxes(?:/([^/]+))?$", url.path)
        if not m:
            return 404, {"kind": "Status", "code": 404}, None
        if method == "GET" and not m.group(1):
            terms = [t for t in parse_qs(url.query).get("labelSelector", [""])[0].split(",") if t]
            want = dict(t.split("=", 1) for t in terms)
            items = [r for r in self._all() if all(r["metadata"]["labels"].get(k) == v for k, v in want.items())]
            return 200, {"kind": "SandboxList", "items": items}, None
        if method == "DELETE" and m.group(1):
            file = self.dir / f"{m.group(1)}.json"
            body = json.loads(raw or b"{}")
            if body.get("propagationPolicy") != "Foreground":
                return 422, {"kind": "Status", "message": "the test wants Foreground"}, None
            if not file.exists():
                return 404, {"kind": "Status", "code": 404}, None
            if self.delete_answer == 404:
                file.unlink()
                return 404, {"kind": "Status", "code": 404}, None
            if self.delete_answer == 409:
                return 409, {"kind": "Status", "code": 409}, None
            rec = json.loads(file.read_text())
            if (body.get("preconditions") or {}).get("uid") != rec["metadata"]["uid"]:
                return 409, {"kind": "Status", "code": 409}, None
            if self.stuck:
                rec["metadata"]["deletionTimestamp"] = "2026-11-15T00:00:00Z"
                file.write_text(json.dumps(rec))
                return 202, rec, None
            file.unlink()
            return 200, rec, None
        return 405, {"kind": "Status", "code": 405}, None
