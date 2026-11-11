"""The run receiver: the one entry for a run of a workflow agent (Design Doc 0003, section 4.4).

    POST /runs          Authorization: Bearer <token>
                        {"agent": "<name>", "input": {...}, "key": "<key of the run>", "wait": true}
    GET  /runs/{key}    Authorization: Bearer <token>
    GET  /healthz       no token

A run is one Job, made from the template of the agent in the list from git.
The Job name comes from the key, so a second call with the same key makes no
second Job: it gets the same run, and its answer when the run ends. The caller
sets the agent name, the input and the key, and nothing else of the Job.

Environment:
    RUN_RECEIVER_TOKEN            the token of the callers, at least 32 characters
    RUN_RECEIVER_CONFIG           the list of agents. Default /etc/run-receiver/agents.yaml
    RUN_RECEIVER_MAX_INPUT_BYTES  the largest input, as JSON. Default 262144
    RUN_RECEIVER_POLL_SECONDS     the wait between two reads of a Job. Default 2
    POD_NAMESPACE                 the namespace of the receiver and its Jobs
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from run_job import result
from run_receiver import config
from run_receiver.kube import Kube, KubeError, own_namespace

log = logging.getLogger("run_receiver")

PORT = 8080
KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
HTTP_OF = {result.SUCCEEDED: 200, result.RUNNING: 202, result.FAILED: 502, result.TIMED_OUT: 504}
WAIT_MARGIN_SECONDS = 60
MIN_TOKEN_LENGTH = 32


def job_name(key: str) -> str:
    return "run-" + hashlib.sha256(key.encode()).hexdigest()[:20]


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(agent: str, payload: dict) -> str:
    return hashlib.sha256(canonical({"agent": agent, "input": payload}).encode()).hexdigest()


def job_state(job: dict) -> str:
    for c in (job.get("status") or {}).get("conditions") or []:
        if c.get("status") != "True":
            continue
        if c.get("type") == "Complete":
            return result.SUCCEEDED
        if c.get("type") == "Failed":
            return result.TIMED_OUT if c.get("reason") == "DeadlineExceeded" else result.FAILED
    return result.RUNNING


class Receiver:
    def __init__(
        self,
        token: str,
        templates: dict[str, dict],
        kube: Kube,
        namespace: str,
        max_input_bytes: int = 262144,
        poll_seconds: float = 2.0,
    ):
        if len(token) < MIN_TOKEN_LENGTH:
            raise config.ConfigError(f"the token must have at least {MIN_TOKEN_LENGTH} characters")
        self._auth = f"Bearer {token}".encode()
        self.templates = templates
        self.kube = kube
        self.namespace = namespace
        self.max_input_bytes = max_input_bytes
        self.poll_seconds = poll_seconds

    def authorized(self, request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), self._auth)

    async def outcome(self, job: dict, key: str) -> tuple[int, dict]:
        name = job["metadata"]["name"]
        agent = ((job.get("metadata") or {}).get("labels") or {}).get("workflow-agents/agent", "")
        body = {"key": key, "agent": agent, "job": name}
        state = job_state(job)
        if state == result.RUNNING:
            return 202, body | {"status": result.RUNNING}
        found = result.find(await self.kube.job_log(self.namespace, name, config.CONTAINER)) or {}
        status = state if state == result.TIMED_OUT else found.get("status", result.FAILED)
        if status not in HTTP_OF or status == result.RUNNING:
            status = result.FAILED
        body["status"] = status
        if status == result.SUCCEEDED:
            body["output"] = found.get("output")
        else:
            body["error"] = found.get("error") or (
                "the run reached its time limit" if status == result.TIMED_OUT else "the run job gave no result"
            )
        return HTTP_OF[status], body

    async def wait(self, name: str, until: float) -> dict | None:
        while True:
            job = await self.kube.get_job(self.namespace, name)
            if job is None or job_state(job) != result.RUNNING or time.monotonic() >= until:
                return job
            await asyncio.sleep(self.poll_seconds)

    async def start(self, request: Request) -> tuple[int, dict]:
        try:
            body = await request.json()
        except Exception:
            return 400, {"error": "the body is not JSON"}
        if not isinstance(body, dict):
            return 400, {"error": "the body must be an object"}
        agent, payload, key, wait = body.get("agent"), body.get("input"), body.get("key"), body.get("wait", True)
        extra = sorted(set(body) - {"agent", "input", "key", "wait"})
        if extra:
            return 400, {"error": f"unknown fields: {', '.join(extra)}"}
        if not isinstance(key, str) or not KEY_PATTERN.match(key):
            return 400, {"error": "`key` must be 1 to 200 characters: letters, digits, `.`, `_`, `-`"}
        if not isinstance(agent, str) or not agent:
            return 400, {"error": "`agent` must be a name"}
        if not isinstance(payload, dict):
            return 400, {"error": "`input` must be an object"}
        if not isinstance(wait, bool):
            return 400, {"error": "`wait` must be true or false"}
        template = self.templates.get(agent)
        if template is None:
            return 404, {"error": f"no agent {agent!r} in the list"}
        input_json = canonical(payload)
        if len(input_json.encode()) > self.max_input_bytes:
            return 413, {"error": f"the input is larger than {self.max_input_bytes} bytes"}

        name = job_name(key)
        mark = fingerprint(agent, payload)
        job = config.fill(
            template, name=name, namespace=self.namespace, agent=agent, key=key, input_json=input_json, fingerprint=mark
        )
        created, _ = await self.kube.create_job(self.namespace, job)
        if not created:
            current = await self.kube.get_job(self.namespace, name)
            if current is None:  # deleted in between: make it once more
                created, _ = await self.kube.create_job(self.namespace, job)
            else:
                annotations = (current.get("metadata") or {}).get("annotations") or {}
                if annotations.get("workflow-agents/fingerprint") != mark:
                    return 409, {"error": "the key belongs to a run with a different agent or input", "key": key}
        log.info("run %s of %s: %s", name, agent, "created" if created else "already there")

        deadline = template["spec"]["activeDeadlineSeconds"] + WAIT_MARGIN_SECONDS
        until = time.monotonic() + (deadline if wait else 0)
        current = await self.wait(name, until)
        if current is None:
            return 502, {
                "key": key,
                "agent": agent,
                "job": name,
                "created": created,
                "status": result.FAILED,
                "error": "the Job is gone",
            }
        code, out = await self.outcome(current, key)
        return code, out | {"created": created}

    async def get(self, key: str) -> tuple[int, dict]:
        if not KEY_PATTERN.match(key):
            return 400, {"error": "not a key"}
        job = await self.kube.get_job(self.namespace, job_name(key))
        if job is None:
            return 404, {"error": "no run with this key", "key": key}
        return await self.outcome(job, key)


def create_app(receiver: Receiver) -> FastAPI:
    app = FastAPI(title="run receiver", docs_url=None, redoc_url=None, openapi_url=None)

    async def guarded(request: Request, call) -> JSONResponse:
        if not receiver.authorized(request):
            return JSONResponse({"error": "no valid token"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            code, body = await call()
        except KubeError as e:
            log.error("cluster API: %s", e)
            code, body = 503, {"error": "the cluster API did not answer"}
        return JSONResponse(body, status_code=code)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/runs")
    async def post_run(request: Request):
        return await guarded(request, lambda: receiver.start(request))

    @app.get("/runs/{key}")
    async def get_run(key: str, request: Request):
        return await guarded(request, lambda: receiver.get(key))

    return app


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    try:
        templates = config.load(os.environ.get("RUN_RECEIVER_CONFIG", "/etc/run-receiver/agents.yaml"))
        receiver = Receiver(
            os.environ.get("RUN_RECEIVER_TOKEN", ""),
            templates,
            Kube(),
            own_namespace(),
            max_input_bytes=int(os.environ.get("RUN_RECEIVER_MAX_INPUT_BYTES", "262144")),
            poll_seconds=float(os.environ.get("RUN_RECEIVER_POLL_SECONDS", "2")),
        )
    except (OSError, config.ConfigError, KubeError) as e:
        print(f"run-receiver: {e}", file=sys.stderr)
        return 2
    log.info("agents: %s", ", ".join(sorted(templates)))

    import uvicorn

    uvicorn.run(create_app(receiver), host="0.0.0.0", port=PORT, log_level="warning", access_log=False)
    return 0
