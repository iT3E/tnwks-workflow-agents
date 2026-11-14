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
delete the pod of an ended Job seconds after it ends (the pod GC deletes the
oldest terminated pods of the whole cluster over its threshold), so the
receiver does not wait for the end to read the log. For each running run it
follows the log of the pod as it is written (follow=true), from a call that
makes or finds the run, and from the loop for a run that nobody called about
(after a restart of the receiver). The result line is in hand when the run
job writes it, before its pod can end. The receiver keeps it in the
annotation workflow-agents/result of the Job, and a later call reads the
annotation. When the receiver was down for the whole end of a run, the
answer is the state of the Job, with "output_kept": false.

Environment:
    RUN_RECEIVER_TOKEN            the token of the callers, at least 32 characters
    RUN_RECEIVER_CONFIG           the list of agents. Default /etc/run-receiver/agents.yaml
    RUN_RECEIVER_MAX_INPUT_BYTES  the largest input, as JSON. Default 262144
    RUN_RECEIVER_POLL_SECONDS     the wait between two reads of a Job. Default 2
    RUN_RECEIVER_RECORD_SECONDS   the wait between two rounds of the loop that follows the
                                  runs no call started and keeps the results of ended
                                  Jobs. Default 10; 0 turns it off
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
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from run_job import result
from run_receiver import config
from run_receiver.kube import Kube, KubeError, container_started, own_namespace, pod_ended

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
# The results the receiver read, in memory too: the answer of a call does not
# wait for the annotation, and a refused patch loses nothing while it runs.
RESULTS_IN_MEMORY = 256


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


def kept_result(job: dict | None) -> dict | None:
    """The result kept in the annotation of the Job, or None."""
    kept = (((job or {}).get("metadata") or {}).get("annotations") or {}).get(RESULT_ANNOTATION)
    if kept is None:
        return None
    try:
        value = json.loads(kept)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


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
        follow_wait_seconds: float = 30.0,
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
        self.follow_wait_seconds = follow_wait_seconds
        self._record_error = ""
        self.followers: dict[str, asyncio.Task] = {}
        self.results: OrderedDict[str, dict] = OrderedDict()

    def authorized(self, request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), self._auth)

    def remember(self, name: str, found: dict) -> None:
        self.results[name] = found
        self.results.move_to_end(name)
        while len(self.results) > RESULTS_IN_MEMORY:
            self.results.popitem(last=False)

    async def keep(self, name: str, found: dict) -> None:
        """Keep the result in the annotation of the Job. A failure is a log line: the answer goes on."""
        text = canonical(found)
        if len(text.encode()) > RESULT_MAX_BYTES:
            found = {k: v for k, v in found.items() if k != "output"} | GONE
            text = canonical(found)
        self.remember(name, found)  # the same answer from memory as from the annotation
        try:
            await self.kube.annotate_job(self.namespace, name, {RESULT_ANNOTATION: text})
        except KubeError as e:
            log.warning("run %s: the result is not kept: %s", name, e)
        else:
            log.info("run %s: result kept (%s)", name, found.get("status") or "the pod is gone")

    def follow(self, name: str) -> asyncio.Task:
        """The one task that follows the log of the run of this Job (made once)."""
        task = self.followers.get(name)
        if task is None or task.done():
            task = asyncio.create_task(self._follow(name))
            self.followers[name] = task
            task.add_done_callback(lambda t: self.followers.pop(name, None) if self.followers.get(name) is t else None)
        return task

    async def _follow(self, name: str) -> None:
        """Read the log of the run pod as it is written, and keep the result line when it comes.

        The run job writes the result line last, just before it exits. The pod
        GC deletes only a pod that has ended, so the line reaches this stream
        before the pod can go. A stream that breaks starts again from the start
        of the log. The task ends with the result, or when there is nothing
        more to read: then outcome() reads what is left.
        """
        while True:
            try:
                job = await self.kube.get_job(self.namespace, name)
                if job is None or RESULT_ANNOTATION in ((job.get("metadata") or {}).get("annotations") or {}):
                    return
                pod = await self.kube.newest_pod(self.namespace, name)
                if pod is not None and container_started(pod, config.CONTAINER):
                    found = None
                    async for line in self.kube.log_lines(self.namespace, pod["metadata"]["name"], config.CONTAINER):
                        value = result.find(line)
                        if value is not None:
                            found = value
                    if found is not None:
                        await self.keep(name, found)
                        return
                if job_state(job) != result.RUNNING and (pod is None or pod_ended(pod)):
                    return
            except KubeError as e:
                log.warning("run %s: the log is not followed: %s", name, e)
            await asyncio.sleep(self.poll_seconds)

    async def result_of(self, job: dict) -> dict:
        """The result of an ended run: in memory, kept, from its follower, or from the log of its pod (then kept)."""
        meta = job.get("metadata") or {}
        name = meta["name"]
        if name in self.results:
            return self.results[name]
        value = kept_result(job)
        if value is not None:
            return value
        task = self.followers.get(name)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), self.follow_wait_seconds)
            except asyncio.TimeoutError:
                pass
            if name in self.results:
                return self.results[name]
        got = await self.kube.job_log(self.namespace, name, config.CONTAINER)
        if got is None:
            # Kept in the meantime (by a follower of another call)? Then never write "gone" over it.
            fresh = await self.kube.get_job(self.namespace, name)
            value = kept_result(fresh)
            if value is not None:
                return value
            found = dict(GONE)
        else:
            text, ended = got
            found = result.find(text) or {}
            if not ended:  # the pod still stops: its log is not the last one
                return found
        await self.keep(name, found)
        return self.results.get(name, found)

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
        """One round of the loop: follow each running run that has no follower, keep each ended one with no result."""
        count = 0
        for job in await self.kube.list_jobs(self.namespace, AGENT_LABEL):
            meta = job.get("metadata") or {}
            name = str(meta.get("name", ""))
            if not name.startswith("run-") or RESULT_ANNOTATION in (meta.get("annotations") or {}):
                continue
            if job_state(job) == result.RUNNING:
                self.follow(name)
                continue
            if name in self.followers:
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
        self.follow(name)

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
            for follower in list(receiver.followers.values()):
                follower.cancel()

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


def configure_logging() -> None:
    """INFO for the receiver; WARNING for httpx.

    httpx logs each request at INFO, and the loop that keeps the results reads
    the Jobs every RUN_RECEIVER_RECORD_SECONDS: one line each round, about
    8,600 a day, with nothing in them. The receiver logs its own line when it
    makes a Job and when it keeps a result, and an error of the cluster API
    still gets to the log. (The run job keeps the httpx lines of its calls to
    the sandbox: it configures its own logging.)
    """
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    configure_logging()
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
