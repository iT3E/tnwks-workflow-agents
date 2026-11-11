"""The run job: one run of one workflow agent in a sandbox that runs one time.

Steps (Design Doc 0003, appendix A.1, steps 3 to 6):
  1. Register the sandbox gateway of the trust domain with its client certificate.
  2. Make a sandbox from the image of the workflow agents, with the agent name,
     its providers, its policy, and the command that serves the contract.
  3. Forward the contract port with the client certificate, and wait for GET /ping.
  4. Send the input in the body of POST /invocations.
  5. Print the answer as the result line, and delete the sandbox. The delete
     runs also when a step fails and when the pod gets SIGTERM, for example at
     the time limit of the Job.

The input goes in the body, never in a command argument (32 KiB) or in a
sandbox variable (8 KiB). No credential goes into the sandbox: the providers
of the sandbox gateway add each one outside it.

Environment (the Job template of each agent sets them; the run receiver adds RUN_KEY and RUN_INPUT):
    AGENT_NAME                  the workflow agent that the sandbox serves
    RUN_KEY                     the key of the run
    RUN_INPUT                   the `input` object of the contract, as JSON
    SANDBOX_IMAGE               the image of the sandbox: this image, at the same digest
    SANDBOX_POLICY              the path of the sandbox policy file
    SANDBOX_PROVIDERS           the providers of the agent, comma separated
    SANDBOX_CPU, SANDBOX_MEMORY optional limits of the sandbox
    AGENT_ENV_<NAME>            sets <NAME> in the sandbox. Never a credential
    OPENSHELL_GATEWAY_URL       the sandbox gateway, https://<service>.<namespace>.svc.cluster.local:8080
    OPENSHELL_MTLS_DIR          the client certificate: ca.crt, tls.crt, tls.key. Default /etc/openshell/mtls
    OPENSHELL_GATEWAY_NAME      the name of the gateway for the CLI. Default workflows
    READY_TIMEOUT_SECONDS       the wait for GET /ping. Default 180
    RUN_TIMEOUT_SECONDS         the wait for the answer. Default 1800
    SANDBOX_PORT                the contract port. Default 8080

Exit codes: 0 the run succeeded, 1 the agent answered with an error, 2 the
environment is not complete, 3 the sandbox did not start or answer /ping,
4 the run took more than RUN_TIMEOUT_SECONDS, 5 the pod was stopped,
6 the sandbox was not deleted.
"""

import hashlib
import json
import logging
import os
import re
import signal
import sys
import time

import httpx

from run_job import result
from run_job.openshell import OpenShell, OpenShellError

log = logging.getLogger("run_job")

AGENT_COMMAND = ["python3", "/app/src/entrypoint.py", "serve"]
ENV_PREFIX = "AGENT_ENV_"
SECRET_WORDS = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE", re.IGNORECASE)
# OpenShell 0.1.7 refuses a sandbox name longer than 19 characters (seen on
# the kind cluster of CI): run-<7 hex>-<6 or 7 base36> is 18 or 19.
MAX_NAME_LENGTH = 19
NAME_PATTERN = re.compile(r"^run-[0-9a-f]{7}-[0-9a-z]{1,7}$")
RUN_LABEL = "workflow-run"
AGENT_LABEL = "workflow-agent"
DELETE_ATTEMPTS = 3


class ConfigError(ValueError):
    pass


class Stopped(Exception):
    pass


def base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def sandbox_name(key: str, now: float) -> str:
    """run-<7 hex of the key>-<start time in base 36>. The sweep reads the time back from it."""
    return f"run-{hashlib.sha256(key.encode()).hexdigest()[:7]}-{base36(int(now))}"


def started_at(name: str) -> int | None:
    if not NAME_PATTERN.match(name):
        return None
    return int(name.rsplit("-", 1)[1], 36)


def agent_env(environ: dict[str, str], agent: str) -> dict[str, str]:
    env = {"AGENT_NAME": agent}
    for key, value in sorted(environ.items()):
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX) :]
        if not re.match(r"^[A-Z_][A-Z0-9_]*$", name):
            raise ConfigError(f"{key}: not a variable name")
        if SECRET_WORDS.search(name):
            raise ConfigError(f"{key}: a credential goes to a sandbox through a provider, not a variable")
        if "\n" in value or len(value.encode()) > 8192:
            raise ConfigError(f"{key}: a sandbox variable is one line of at most 8 KiB")
        env[name] = value
    return env


class Config:
    def __init__(self, environ: dict[str, str]):
        def need(name: str) -> str:
            value = environ.get(name, "")
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        self.agent = need("AGENT_NAME")
        self.key = need("RUN_KEY")
        raw = need("RUN_INPUT")
        try:
            self.input = json.loads(raw)
        except ValueError as e:
            raise ConfigError("RUN_INPUT is not JSON") from e
        if not isinstance(self.input, dict):
            raise ConfigError("RUN_INPUT is not a JSON object")
        self.image = need("SANDBOX_IMAGE")
        self.policy = need("SANDBOX_POLICY")
        if not os.path.isfile(self.policy):
            raise ConfigError(f"SANDBOX_POLICY: no file {self.policy}")
        self.providers = [p.strip() for p in environ.get("SANDBOX_PROVIDERS", "").split(",") if p.strip()]
        self.cpu = environ.get("SANDBOX_CPU", "")
        self.memory = environ.get("SANDBOX_MEMORY", "")
        self.env = agent_env(environ, self.agent)
        self.gateway_url = need("OPENSHELL_GATEWAY_URL")
        self.mtls_dir = environ.get("OPENSHELL_MTLS_DIR", "/etc/openshell/mtls")
        self.gateway = environ.get("OPENSHELL_GATEWAY_NAME", "workflows")
        self.binary = environ.get("OPENSHELL_BIN", "openshell")
        self.ready_timeout = float(environ.get("READY_TIMEOUT_SECONDS", "180"))
        self.run_timeout = float(environ.get("RUN_TIMEOUT_SECONDS", "1800"))
        self.port = int(environ.get("SANDBOX_PORT", "8080"))


def wait_ready(base: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=3) as http:
        while True:
            try:
                if http.get(f"{base}/ping").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"no answer on /ping in {timeout:.0f} s")
            time.sleep(1)


def invoke(base: str, payload: dict, timeout: float) -> tuple[int, dict]:
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10)) as http:
        r = http.post(f"{base}/invocations", json={"input": payload})
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:500]}
    return r.status_code, body if isinstance(body, dict) else {"error": "the answer is not a JSON object"}


def run(cfg: Config, cli: OpenShell, now: float) -> tuple[int, dict]:
    name = sandbox_name(cfg.key, now)
    base = f"http://127.0.0.1:{cfg.port}"
    out: dict = {"run_key": cfg.key, "agent": cfg.agent, "sandbox": name}
    made = False
    code = 0
    try:
        cli.register(cfg.gateway_url, cfg.mtls_dir)
        made = True  # from here on, a sandbox can exist
        cli.create(
            name,
            cfg.image,
            AGENT_COMMAND,
            cfg.providers,
            cfg.policy,
            cfg.env,
            {RUN_LABEL: "true", AGENT_LABEL: cfg.agent},
            cpu=cfg.cpu,
            memory=cfg.memory,
        )
        cli.forward_start(cfg.port, name)
        wait_ready(base, cfg.ready_timeout)
        log.info("sandbox %s is ready after %.0f s", name, time.time() - now)
        try:
            status, body = invoke(base, cfg.input, cfg.run_timeout)
        except httpx.TimeoutException:
            code, out["status"], out["error"] = 4, result.TIMED_OUT, f"no answer in {cfg.run_timeout:.0f} s"
        else:
            out["http_status"] = status
            if status == 200 and "output" in body:
                out["status"], out["output"] = result.SUCCEEDED, body["output"]
            else:
                code, out["status"] = 1, result.FAILED
                out["error"] = str(body.get("error") or body.get("detail") or f"HTTP {status}")[:900]
    except Stopped:
        code, out["status"], out["error"] = 5, result.FAILED, "the run job was stopped"
    except (OpenShellError, TimeoutError, httpx.HTTPError) as e:
        code, out["status"], out["error"] = 3, result.FAILED, str(e)[:900]
    finally:
        if made:
            out["sandbox_deleted"] = cleanup(cli, cfg.port, name)
            if not out["sandbox_deleted"] and code == 0:
                code = 6
    out["seconds"] = round(time.time() - now, 1)
    return code, out


def cleanup(cli: OpenShell, port: int, name: str) -> bool:
    """Stop the forward and delete the sandbox. A second SIGTERM must not stop this."""
    previous = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        cli.forward_stop(port, name)
        for attempt in range(1, DELETE_ATTEMPTS + 1):
            try:
                cli.delete(name)
                log.info("sandbox %s deleted", name)
                return True
            except OpenShellError as e:
                if "not found" in str(e).lower():
                    log.info("sandbox %s is not there", name)
                    return True
                log.error("delete of %s, attempt %d: %s", name, attempt, e)
                time.sleep(2 * attempt)
        return False
    finally:
        signal.signal(signal.SIGTERM, previous)


def _stop(signum, frame):
    raise Stopped()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s"
    )
    now = time.time()
    try:
        cfg = Config(dict(os.environ))
    except ConfigError as e:
        print(result.line({"status": result.FAILED, "error": f"run job config: {e}"}), flush=True)
        return 2
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    code, out = run(cfg, OpenShell(cfg.binary, cfg.gateway), now)
    log.info("run %s of %s: %s in %.0f s", out["sandbox"], cfg.agent, out["status"], out["seconds"])
    print(result.line(out), flush=True)
    return code
