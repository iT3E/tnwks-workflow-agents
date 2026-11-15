"""The Sandbox records of the run sandboxes, read and removed through the Kubernetes API.

A delete through the sandbox gateway can answer with success and leave the
Sandbox record (agents.x-k8s.io) of the sandbox: OpenShell 0.1.8 does that
when its label lookup finds no record, and it then forgets the sandbox, so a
second delete or a list through the gateway can't see it any more. So the run
job reads the record after its delete, and the sweep lists the records here as
well as through the gateway.

    GET    /apis/agents.x-k8s.io/v1beta1/namespaces/{ns}/sandboxes?labelSelector=...  list    sandboxes
    DELETE /apis/agents.x-k8s.io/v1beta1/namespaces/{ns}/sandboxes/{name}             delete  sandboxes (the sweep only)

A run record is a record that OpenShell made for a sandbox with a run name:
the label openshell.ai/managed-by=openshell, and openshell.ai/sandbox-name
with the name pattern of a run (run-<7 hex>-<base 36>). The labels that the
run job gives the sandbox stay in the store of the gateway and are not on the
record, so the name is the mark of a run here. A record with any other name or
label is never a run record.

In a pod, the client uses the service account token and CA of the pod, and
reads the token file again for each call, because Kubernetes rotates it.
"""

import os
import time
from collections.abc import Callable
from datetime import datetime

import httpx

from run_job.names import NAME_PATTERN, started_at

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
GROUP_PATH = "/apis/agents.x-k8s.io/v1beta1"
MANAGED_BY = "openshell.ai/managed-by"
SANDBOX_NAME = "openshell.ai/sandbox-name"


class RecordError(RuntimeError):
    """A read or a delete of a record that did not get an answer we can use."""


class RecordChanged(RecordError):
    """The record changed between its read and its delete (409, the uid precondition)."""


def is_run_record(record: dict) -> bool:
    labels = (record.get("metadata") or {}).get("labels") or {}
    name = labels.get(SANDBOX_NAME, "")
    return labels.get(MANAGED_BY) == "openshell" and bool(NAME_PATTERN.match(name))


def sandbox_of(record: dict) -> str:
    return ((record.get("metadata") or {}).get("labels") or {}).get(SANDBOX_NAME, "")


def age_seconds(record: dict, now: float) -> float | None:
    """The age of a run record by the younger of its two clocks: the start time in
    the sandbox name, and the creationTimestamp of the record. None when either
    one can't be read, so that the caller treats the record as young."""
    start = started_at(sandbox_of(record))
    made = (record.get("metadata") or {}).get("creationTimestamp", "")
    try:
        created = datetime.fromisoformat(made).timestamp()
    except ValueError:
        return None
    if start is None:
        return None
    return now - max(start, created)


def own_namespace() -> str:
    ns = os.environ.get("POD_NAMESPACE", "")
    if ns:
        return ns
    try:
        with open(f"{SA_DIR}/namespace", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        raise RecordError("the namespace is not known: set POD_NAMESPACE, or mount the service account token") from e


def in_cluster_url() -> str:
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise RecordError("not in a cluster: KUBERNETES_SERVICE_HOST is not set, and KUBE_API_URL is not set")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}:{port}"


class Records:
    def __init__(
        self,
        base_url: str | None = None,
        namespace: str | None = None,
        token_file: str | None = None,
        ca_file: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.base_url = base_url or os.environ.get("KUBE_API_URL") or in_cluster_url()
        self.namespace = namespace or own_namespace()
        self.token_file = token_file or os.environ.get("KUBE_TOKEN_FILE", f"{SA_DIR}/token")
        ca = ca_file or os.environ.get("KUBE_CA_FILE", f"{SA_DIR}/ca.crt")
        self.verify: str | bool = ca if os.path.isfile(ca) else True
        self.sleep = sleep
        self.monotonic = monotonic

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            with open(self.token_file, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            raise RecordError(f"no service account token at {self.token_file}") from e
        try:
            with httpx.Client(base_url=self.base_url, verify=self.verify, timeout=15) as http:
                return http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.HTTPError as e:
            raise RecordError(f"{method} sandboxes: {type(e).__name__}") from e

    def _path(self, name: str = "") -> str:
        base = f"{GROUP_PATH}/namespaces/{self.namespace}/sandboxes"
        return f"{base}/{name}" if name else base

    def _list(self, selector: str) -> list[dict]:
        r = self._call("GET", self._path(), params={"labelSelector": selector})
        if r.status_code != 200:
            raise RecordError(f"list sandboxes: HTTP {r.status_code} {r.text[:200]}")
        items = r.json().get("items")
        return items if isinstance(items, list) else []

    def of_sandbox(self, sandbox: str) -> list[dict]:
        """The records of one run sandbox (none, or one)."""
        if not NAME_PATTERN.match(sandbox):
            raise RecordError(f"{sandbox} is not the name of a run sandbox")
        found = self._list(f"{MANAGED_BY}=openshell,{SANDBOX_NAME}={sandbox}")
        return [r for r in found if is_run_record(r) and sandbox_of(r) == sandbox]

    def runs(self) -> list[dict]:
        """Each run record of the namespace."""
        return [r for r in self._list(f"{MANAGED_BY}=openshell") if is_run_record(r)]

    def wait_gone(self, sandbox: str, seconds: float) -> bool:
        """True when the sandbox has no record, within `seconds`."""
        deadline = self.monotonic() + seconds
        while self.of_sandbox(sandbox):
            if self.monotonic() >= deadline:
                return False
            self.sleep(3)
        return True

    def delete(self, record: dict) -> None:
        """Delete a run record, and what it owns with it (Foreground: its workspace
        claim, its Services and its NetworkPolicy go first). Only a run record,
        and only the one that was read (the uid precondition). A 404 means the
        record is gone already, which is what the caller wants."""
        if not is_run_record(record):
            raise RecordError(f"{record.get('metadata', {}).get('name')} is not a run record; left as it is")
        meta = record["metadata"]
        body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Foreground",
            "preconditions": {"uid": meta["uid"]},
        }
        r = self._call("DELETE", self._path(meta["name"]), json=body)
        if r.status_code in (200, 202, 404):
            return
        if r.status_code == 409:
            raise RecordChanged(f"{meta['name']} changed between its read and its delete (409)")
        raise RecordError(f"delete {meta['name']}: HTTP {r.status_code} {r.text[:200]}")
