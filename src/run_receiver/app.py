"""The run receiver: the one entry for a run of a workflow agent (Design Doc 0003, section 4.4).

    POST /runs          Authorization: Bearer <token>
                        {"agent": "<name>", "input": {...}, "key": "<key of the run>", "wait": true}
    GET  /runs/{key}    Authorization: Bearer <token>
    GET  /healthz       no token

A run is one Job, made from the template of the agent in the list from git.
The Job name comes from the key, so a second call with the same key makes no
second Job: it gets the same run, and its answer when the run ends. The caller
sets the agent name, the input and the key, and nothing else of the Job.

The answer of a run is the result line in the log of its pod. A cluster can
delete the pod of an ended Job long before the Job (the pod GC), so the
receiver keeps the result in the annotation workflow-agents/result of the Job
when it first reads it: on a call, and in a loop over the ended Jobs that have
no result yet. A later call reads the annotation. When the pod went before
the receiver read it, the answer is the state of the Job, with
"output_kept": false.

Environment:
    RUN_RECEIVER_TOKEN            the token of the callers, at least 32 characters
    RUN_RECEIVER_CONFIG           the list of agents. Default /etc/run-receiver/agents.yaml
    RUN_RECEIVER_MAX_INPUT_BYTES  the largest input, as JSON. Default 262144
    RUN_RECEIVER_POLL_SECONDS     the wait between two reads of a Job. Default 2
    RUN_RECEIVER_RECORD_SECONDS   the wait between two rounds of the loop that keeps
                                  the results of ended Jobs. Default 10; 0 turns it off
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
from contextlib import asynccontextmanager

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
AGENT_LABEL = "workflow-agents/agent"
RESULT_ANNOTATION = "workflow-agents/result"
# The kept result is at most 64 KiB: the annotations of an object have 256 KiB
# in all. A larger result is kept without its output.
RESULT_MAX_BYTES = 65536
# The kept result of a run whose pod went before the receiver read its log.
GONE = {"output_kept": False}


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
        record_seconds: float = 10.0,
    ):
        if len(token) < MIN_TOKEN_LENGTH:
            raise config.ConfigError(f"the token must have at least {MIN_TOKEN_LENGTH} characters")
        self._auth = f"Bearer {token}".encode()
        self.templates = templates
        self.kube = kube
        self.namespace = namespace
        self.max_input_bytes = max_input_bytes
        self.poll_seconds = poll_seconds
        self.record_seconds = record_seconds
        self._record_error = ""

    def authorized(self, request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), self._auth)

    async def keep(self, name: str, found: dict) -> None:
        """Keep the result in the annotation of the Job. A failure is a log line: the answer goes on."""
        text = canonical(found)
        if len(text.encode()) > RESULT_MAX_BYTES:
            text = canonical({k: v for k, v in found.items() if k != "output"} | GONE)
        try:
            await self.kube.annotate_job(self.namespace, name, {RESULT_ANNOTATION: text})
        except KubeError as e:
            log.warning("run %s: the result is not kept: %s", name, e)

    async def result_of(self, job: dict) -> dict:
        """The result of an ended run: the kept one, or the one in the log of its pod (and then kept)."""
        meta = job.get("metadata") or {}
        kept = (meta.get("annotations") or {}).get(RESULT_ANNOTATION)
        if kept is not None:
            try:
                value = json.loads(kept)
            except ValueError:
                value = None
            if isinstance(value, dict):
                return value
        name = meta["name"]
        got = await self.kube.job_log(self.namespace, name, config.CONTAINER)
        if got is None:
            found = dict(GONE)
        else:
            text, pod_ended = got
            found = result.find(text) or {}
            if not pod_ended:  # the pod still stops: its log is not the last one
                return found
        await self.keep(name, found)
        return found

    async def outcome(self, job: dict, key: str) -> tuple[int, dict]:
        name = job["metadata"]["name"]
        agent = ((job.get("metadata") or {}).get("labels") or {}).get(AGENT_LABEL, "")
        body = {"key": key, "agent": agent, "job": name}
        state = job_state(job)
        if state == result.RUNNING:
            return 202, body | {"status": result.RUNNING}
        found = await self.result_of(job)
        gone = "status" not in found and found.get("output_kept") is False
        if gone:  # the state of the Job is all that is left
            status = state
        else:
            status = state if state == result.TIMED_OUT else found.get("status", result.FAILED)
            if status not in HTTP_OF or status == result.RUNNING:
                status = result.FAILED
        body["status"] = status
        if found.get("output_kept") is False:
            body["output_kept"] = False
        if status == result.SUCCEEDED:
            if not gone and "output_kept" not in body:
                body["output"] = found.get("output")
        elif gone:
            body["error"] = "the result of the run is no longer kept: its pod is gone"
        else:
            body["error"] = found.get("error") or (
                "the run reached its time limit" if status == result.TIMED_OUT else "the run job gave no result"
            )
        return HTTP_OF[status], body

    async def keep_ended(self) -> int:
        """One round of the loop: keep the result of each ended run Job that has none yet."""
        count = 0
        for job in await self.kube.list_jobs(self.namespace, AGENT_LABEL):
            meta = job.get("metadata") or {}
            if not str(meta.get("name", "")).startswith("run-") or job_state(job) == result.RUNNING:
                continue
            if RESULT_ANNOTATION in (meta.get("annotations") or {}):
                continue
            await self.result_of(job)
            count += 1
        return count

    async def keep_loop(self) -> None:
        while True:
            try:
                await self.keep_ended()
                self._record_error = ""
            except KubeError as e:
                if str(e) != self._record_error:  # one line for each new error, not one each round
                    log.warning("the loop that keeps the results: %s", e)
                self._record_error = str(e)
            await asyncio.sleep(self.record_seconds)

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
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(receiver.keep_loop()) if receiver.record_seconds > 0 else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(title="run receiver", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

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
            record_seconds=float(os.environ.get("RUN_RECEIVER_RECORD_SECONDS", "10")),
        )
    except (OSError, config.ConfigError, KubeError) as e:
        print(f"run-receiver: {e}", file=sys.stderr)
        return 2
    log.info("agents: %s", ", ".join(sorted(templates)))

    import uvicorn

    uvicorn.run(create_app(receiver), host="0.0.0.0", port=PORT, log_level="warning", access_log=False)
    return 0
