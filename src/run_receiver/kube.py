"""The Kubernetes API calls of the run receiver, in its own namespace only.

    POST  /apis/batch/v1/namespaces/{ns}/jobs                  create   jobs
    GET   /apis/batch/v1/namespaces/{ns}/jobs/{name}           get      jobs
    GET   /apis/batch/v1/namespaces/{ns}/jobs?labelSelector=   list     jobs
    PATCH /apis/batch/v1/namespaces/{ns}/jobs/{name}           patch    jobs (an annotation only)
    GET   /api/v1/namespaces/{ns}/pods?labelSelector=...       list     pods
    GET   /api/v1/namespaces/{ns}/pods/{name}/log              get      pods/log (also with follow=true)

In a pod, the client uses the service account token and CA of the pod. The
token file is read again for each call, because Kubernetes rotates it.
"""

import json
import os
from collections.abc import AsyncIterator

import httpx

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
LOG_LIMIT_BYTES = 4 * 1024 * 1024
ENDED_PHASES = ("Succeeded", "Failed")


class KubeError(RuntimeError):
    pass


def in_cluster_url() -> str:
    host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if not host:
        raise KubeError("not in a cluster: KUBERNETES_SERVICE_HOST is not set, and KUBE_API_URL is not set")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}:{port}"


def own_namespace() -> str:
    ns = os.environ.get("POD_NAMESPACE", "")
    if ns:
        return ns
    try:
        with open(f"{SA_DIR}/namespace", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        raise KubeError("the namespace is not known: set POD_NAMESPACE") from e


class Kube:
    def __init__(self, base_url: str | None = None, token_file: str | None = None, ca_file: str | None = None):
        self.base_url = base_url or os.environ.get("KUBE_API_URL") or in_cluster_url()
        self.token_file = token_file or os.environ.get("KUBE_TOKEN_FILE", f"{SA_DIR}/token")
        ca = ca_file or os.environ.get("KUBE_CA_FILE", f"{SA_DIR}/ca.crt")
        verify = ca if os.path.isfile(ca) else True
        self.http = httpx.AsyncClient(base_url=self.base_url, verify=verify, timeout=30)

    async def close(self) -> None:
        await self.http.aclose()

    def _headers(self) -> dict:
        try:
            with open(self.token_file, encoding="utf-8") as f:
                return {"Authorization": f"Bearer {f.read().strip()}"}
        except OSError:
            return {}

    async def _call(self, method: str, path: str, headers_extra: dict | None = None, **kwargs) -> httpx.Response:
        try:
            return await self.http.request(method, path, headers=self._headers() | (headers_extra or {}), **kwargs)
        except httpx.HTTPError as e:
            raise KubeError(f"{method} {path}: {type(e).__name__}") from e

    async def create_job(self, namespace: str, job: dict) -> tuple[bool, dict]:
        """Create the Job. Returns (True, job), or (False, None) when a Job of that name is there."""
        r = await self._call("POST", f"/apis/batch/v1/namespaces/{namespace}/jobs", json=job)
        if r.status_code == 409:
            return False, {}
        if r.status_code >= 300:
            raise KubeError(f"create job: {r.status_code} {r.text[:300]}")
        return True, r.json()

    async def get_job(self, namespace: str, name: str) -> dict | None:
        r = await self._call("GET", f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}")
        if r.status_code == 404:
            return None
        if r.status_code >= 300:
            raise KubeError(f"get job: {r.status_code}")
        return r.json()

    async def list_jobs(self, namespace: str, selector: str) -> list[dict]:
        r = await self._call("GET", f"/apis/batch/v1/namespaces/{namespace}/jobs", params={"labelSelector": selector})
        if r.status_code >= 300:
            raise KubeError(f"list jobs: {r.status_code}")
        return r.json().get("items") or []

    async def annotate_job(self, namespace: str, name: str, annotations: dict[str, str]) -> None:
        """Set annotations of the Job (a merge patch of metadata.annotations, and nothing else)."""
        r = await self._call(
            "PATCH",
            f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}",
            content=json.dumps({"metadata": {"annotations": annotations}}).encode(),
            headers_extra={"Content-Type": "application/merge-patch+json"},
        )
        if r.status_code >= 300:
            raise KubeError(f"patch job: {r.status_code} {r.text[:300]}")

    async def newest_pod(self, namespace: str, job_name: str) -> dict | None:
        """The newest pod of the Job, or None."""
        r = await self._call(
            "GET",
            f"/api/v1/namespaces/{namespace}/pods",
            params={"labelSelector": f"batch.kubernetes.io/job-name={job_name}"},
        )
        if r.status_code >= 300:
            raise KubeError(f"list pods: {r.status_code}")
        pods = r.json().get("items", [])
        if not pods:
            return None
        pods.sort(key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")
        return pods[-1]

    async def job_log(self, namespace: str, job_name: str, container: str) -> tuple[str, bool] | None:
        """The log of the newest pod of the Job, and whether that pod has ended. None when the Job has no pod."""
        pod = await self.newest_pod(namespace, job_name)
        if pod is None:
            return None
        r = await self._call(
            "GET",
            f"/api/v1/namespaces/{namespace}/pods/{pod['metadata']['name']}/log",
            params={"container": container, "limitBytes": LOG_LIMIT_BYTES},
        )
        if r.status_code == 404:  # deleted after the list
            return None
        if r.status_code >= 300:
            raise KubeError(f"pod log: {r.status_code}")
        return r.text, pod_ended(pod)

    async def log_lines(self, namespace: str, pod: str, container: str) -> AsyncIterator[str]:
        """The log of a container, line by line, as it is written (follow=true), to the end of the container.

        No read timeout: a run can write nothing for minutes. The stream ends
        when the container ends, or when the API server or the kubelet ends it.
        """
        path = f"/api/v1/namespaces/{namespace}/pods/{pod}/log"
        try:
            async with self.http.stream(
                "GET",
                path,
                params={"container": container, "follow": "true"},
                headers=self._headers(),
                timeout=httpx.Timeout(30, read=None),
            ) as r:
                if r.status_code >= 300:
                    raise KubeError(f"pod log stream: {r.status_code}")
                async for line in r.aiter_lines():
                    yield line
        except httpx.HTTPError as e:
            raise KubeError(f"GET {path}: {type(e).__name__}") from e


def pod_ended(pod: dict) -> bool:
    return (pod.get("status") or {}).get("phase") in ENDED_PHASES


def container_started(pod: dict, container: str) -> bool:
    """The container runs or ran, so its log can be read."""
    for c in (pod.get("status") or {}).get("containerStatuses") or []:
        if c.get("name") == container:
            state = c.get("state") or {}
            return "running" in state or "terminated" in state
    return False
