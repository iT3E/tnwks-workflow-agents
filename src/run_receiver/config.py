"""The list of agents of the run receiver, with the Job template of each one.

The file comes from git (a ConfigMap of the ops repository):

    agents:
      <agent name>:
        job: <a batch/v1 Job, with no name>

The receiver refuses to start when a template breaks a rule below. Each rule
keeps a caller from changing what a run job is: the caller gives an agent
name, an input and a key, and the receiver changes nothing else.
"""

import copy

import yaml

import agents

CONTAINER = "run-job"
SET_BY_RECEIVER = ("RUN_KEY", "RUN_INPUT")


class ConfigError(ValueError):
    pass


def _check(name: str, job: dict) -> None:
    where = f"agent {name}"
    if not agents.NAME_PATTERN.match(name):
        raise ConfigError(f"{where}: not a valid agent name")
    if name not in agents.AGENTS:
        raise ConfigError(f"{where}: this image has no such agent ({', '.join(agents.names())})")
    if not isinstance(job, dict) or job.get("apiVersion") != "batch/v1" or job.get("kind") != "Job":
        raise ConfigError(f"{where}: `job` must be a batch/v1 Job")
    meta = job.get("metadata") or {}
    if "name" in meta or "generateName" in meta or "namespace" in meta:
        raise ConfigError(f"{where}: the template must not set a name or a namespace")
    spec = job.get("spec") or {}
    if spec.get("backoffLimit") != 0:
        raise ConfigError(f"{where}: spec.backoffLimit must be 0 (one run for one key)")
    deadline = spec.get("activeDeadlineSeconds")
    if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0:
        raise ConfigError(f"{where}: spec.activeDeadlineSeconds must be set (the time limit of a run)")
    pod = (spec.get("template") or {}).get("spec") or {}
    if pod.get("restartPolicy") != "Never":
        raise ConfigError(f"{where}: the pod restartPolicy must be Never")
    containers = pod.get("containers") or []
    run = [c for c in containers if c.get("name") == CONTAINER]
    if len(containers) != 1 or len(run) != 1:
        raise ConfigError(f"{where}: the pod must have one container, named {CONTAINER}")
    env = {e.get("name"): e for e in run[0].get("env") or []}
    if (env.get("AGENT_NAME") or {}).get("value") != name:
        raise ConfigError(f"{where}: the {CONTAINER} container must set AGENT_NAME to {name}")
    for var in SET_BY_RECEIVER:
        if var in env:
            raise ConfigError(f"{where}: the template must not set {var}; the receiver sets it")


def parse(text: str) -> dict[str, dict]:
    data = yaml.safe_load(text) or {}
    entries = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        raise ConfigError("the config must have a non-empty `agents` map")
    out: dict[str, dict] = {}
    for name, entry in entries.items():
        job = (entry or {}).get("job") if isinstance(entry, dict) else None
        _check(str(name), job)
        out[str(name)] = job
    return out


def load(path: str) -> dict[str, dict]:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def fill(template: dict, *, name: str, namespace: str, agent: str, key: str, input_json: str, fingerprint: str) -> dict:
    """The Job of one run: the template, with the name, the labels, the key and the input. Nothing else changes."""
    job = copy.deepcopy(template)
    meta = job.setdefault("metadata", {})
    meta["name"] = name
    meta["namespace"] = namespace
    meta.setdefault("labels", {})["workflow-agents/agent"] = agent
    meta.setdefault("annotations", {}).update(
        {"workflow-agents/run-key": key, "workflow-agents/fingerprint": fingerprint}
    )
    pod_meta = job["spec"]["template"].setdefault("metadata", {})
    pod_meta.setdefault("labels", {})["workflow-agents/agent"] = agent
    container = job["spec"]["template"]["spec"]["containers"][0]
    container.setdefault("env", []).extend(
        [{"name": "RUN_KEY", "value": key}, {"name": "RUN_INPUT", "value": input_json}]
    )
    return job
