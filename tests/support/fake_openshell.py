"""A fake of the OpenShell CLI for the run job tests. It keeps no secret and calls no gateway.

`sandbox create` starts the command of the sandbox as a local process, with
only the --env variables of the call, and with the test credential of each
provider, as the supervisor of a sandbox would add it. The forward is a no-op:
the process listens on 127.0.0.1 in the same container. `sandbox delete` stops
the process. Each call is a JSON line in $FAKE_OPENSHELL_STATE/calls.jsonl.

FAKE_OPENSHELL_MODE holds words that change one step: create-fails,
delete-fails, no-serve (the sandbox never answers /ping).
"""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

PROVIDER_ENV = {"model-gateway": "MODEL_GATEWAY_KEY", "github-reviewer": "GITHUB_TOKEN"}


def main(argv: list[str]) -> int:
    state = Path(os.environ["FAKE_OPENSHELL_STATE"])
    state.mkdir(parents=True, exist_ok=True)
    mode = os.environ.get("FAKE_OPENSHELL_MODE", "").split()
    with open(state / "calls.jsonl", "a") as f:
        f.write(json.dumps(argv) + "\n")

    args = list(argv)
    if args[:1] == ["--version"]:
        print("openshell 0.0.0-fake")
        return 0
    while args and args[0] in ("--color", "-g"):
        args = args[2:]
    if args[:2] == ["gateway", "add"]:
        name = args[args.index("--name") + 1]
        mtls = Path(os.environ.get("HOME", "/tmp")) / ".config" / "openshell" / "gateways" / name / "mtls"
        missing = [n for n in ("ca.crt", "tls.crt", "tls.key") if not (mtls / n).is_file()]
        if missing:
            print(f"error: no {missing} in {mtls}", file=sys.stderr)
            return 1
        return 0
    if args[:2] == ["sandbox", "create"]:
        if "create-fails" in mode:
            print("error: the gateway refused the sandbox", file=sys.stderr)
            return 1
        name = args[args.index("--name") + 1]
        command = args[args.index("--") + 1 :]
        env = {"PATH": os.environ["PATH"], "HOME": "/tmp", "BIND": "127.0.0.1"}
        for i, a in enumerate(args):
            if a == "--env":
                k, v = args[i + 1].split("=", 1)
                env[k] = v
            if a == "--provider":
                p = args[i + 1]
                env[PROVIDER_ENV[p]] = os.environ[f"FAKE_PROVIDER_{PROVIDER_ENV[p]}"]
        if "no-serve" in mode:
            command = ["sleep", "600"]
        log = open(state / f"{name}.log", "w")
        proc = subprocess.Popen(command, env=env, stdout=log, stderr=log, start_new_session=True)
        (state / f"{name}.pid").write_text(str(proc.pid))
        return 0
    if args[:1] == ["forward"]:
        return 0
    if args[:2] == ["sandbox", "delete"]:
        name = args[2]
        pid_file = state / f"{name}.pid"
        if "delete-fails" in mode:
            print("error: the gateway is not available", file=sys.stderr)
            return 1
        if not pid_file.exists():
            print(f"error: sandbox {name} not found", file=sys.stderr)
            return 1
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        pid_file.unlink()
        return 0
    if args[:2] == ["sandbox", "list"]:
        names = [p.stem for p in state.glob("*.pid")]
        names += os.environ.get("FAKE_OPENSHELL_EXTRA", "").split()
        print("\n".join(names))
        return 0
    print(f"fake openshell: unknown call {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
