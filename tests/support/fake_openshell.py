"""A fake of the OpenShell CLI for the run job tests. It keeps no secret and calls no gateway.

`sandbox create` starts the command of the sandbox as a local process, with
only the --env variables of the call, and with the test credential of each
provider, as the supervisor of a sandbox would add it. The process listens on
127.0.0.1 in the same container, so the forward moves no data; but `forward
start --background` leaves a child that holds the standard output and error
of the CLI, as the ssh child of the real CLI does (seen on the kind cluster).
`forward stop` stops that child. `sandbox delete` stops the process. Each call
is a JSON line in $FAKE_OPENSHELL_STATE/calls.jsonl.

`sandbox create` also writes the Sandbox record of the sandbox to
$FAKE_OPENSHELL_STATE/records/, as the gateway does in Kubernetes, and a fake
API (fake_sandboxes.py) serves those files; `sandbox delete` removes it.

FAKE_OPENSHELL_MODE holds words that change one step: create-fails,
delete-fails, list-fails, no-serve (the sandbox never answers /ping), and
delete-keeps-record (the delete answers success, and the gateway forgets the
sandbox and leaves its record, as OpenShell 0.1.8 did).

For the providers command, the store of the gateway is in
$FAKE_OPENSHELL_STATE/profiles/<id>.json and providers/<name>.json. A
provider keeps the sha256 of its credential value (read from the env var
that --credential names, as the real CLI does), never the value. More modes:
gateway-down (`profile list` fails), and <call>-fails for profile-export,
profile-import, profile-update, provider-create and provider-update.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from support.fake_sandboxes import record

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
        records = state / "records"
        records.mkdir(exist_ok=True)
        rec = record(name)
        (records / f"{rec['metadata']['name']}.json").write_text(json.dumps(rec))
        return 0
    if args[:2] == ["forward", "start"]:
        child = subprocess.Popen(["sleep", "600"], start_new_session=True)  # inherits stdout and stderr
        (state / f"{args[3]}.fwd").write_text(str(child.pid))
        return 0
    if args[:2] == ["forward", "stop"]:
        fwd = state / f"{args[3]}.fwd"
        if fwd.exists():
            try:
                os.killpg(int(fwd.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
            fwd.unlink()
        return 0
    if args[:2] == ["sandbox", "delete"]:
        name = args[2]
        pid_file = state / f"{name}.pid"
        if "delete-fails" in mode:
            print("error: the gateway is not available", file=sys.stderr)
            return 1
        rec_file = state / "records" / f"default--{name}.json"
        if not pid_file.exists():
            print(f"error: sandbox {name} not found", file=sys.stderr)
            return 1
        try:
            os.killpg(int(pid_file.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass
        pid_file.unlink()
        if rec_file.exists() and "delete-keeps-record" not in mode:
            rec_file.unlink()
        return 0
    if args[:2] == ["sandbox", "list"]:
        if "list-fails" in mode:
            print("error: the gateway is not available", file=sys.stderr)
            return 1
        names = [p.stem for p in state.glob("*.pid")]
        names += os.environ.get("FAKE_OPENSHELL_EXTRA", "").split()
        print("\n".join(names))
        return 0
    if args[:1] in (["profile"], ["provider"]):
        return store(state, mode, args)
    print(f"fake openshell: unknown call {argv}", file=sys.stderr)
    return 2


def store(state: Path, mode: list[str], args: list[str]) -> int:
    """The profile and provider calls of the providers command."""
    import yaml

    profiles, providers = state / "profiles", state / "providers"
    profiles.mkdir(exist_ok=True)
    providers.mkdir(exist_ok=True)
    call = "-".join(args[:2])
    if f"{call}-fails" in mode:
        print(f"error: {call} refused by the fake gateway", file=sys.stderr)
        return 1
    if args[:2] == ["profile", "list"]:
        if "gateway-down" in mode:
            print("error: connection refused", file=sys.stderr)
            return 1
        print("\n".join(p.stem for p in sorted(profiles.glob("*.json"))))
        return 0
    if args[:2] in (["profile", "describe"], ["profile", "export"]):
        stored = profiles / f"{args[2]}.json"
        if not stored.exists():
            print(f"error: profile {args[2]} not found", file=sys.stderr)
            return 1
        if args[1] == "export":
            print(stored.read_text())
        return 0
    if args[:2] in (["profile", "import"], ["profile", "update"]):
        profile = yaml.safe_load(Path(args[args.index("-f") + 1]).read_text())
        stored = profiles / f"{profile['id']}.json"
        if args[1] == "import":
            if stored.exists():
                print(f"error: profile {profile['id']} already exists", file=sys.stderr)
                return 1
            profile["resource_version"] = 1
        else:
            if args[-1] != profile["id"] or not stored.exists():
                print("error: no such profile", file=sys.stderr)
                return 1
            current = json.loads(stored.read_text())["resource_version"]
            if profile.get("resource_version") != current:
                print("error: resource_version conflict", file=sys.stderr)
                return 1
            profile["resource_version"] = current + 1
        stored.write_text(json.dumps(profile))
        return 0
    if args[:2] == ["provider", "get"]:
        return 0 if (providers / f"{args[2]}.json").exists() else 1
    if args[:2] in (["provider", "create"], ["provider", "update"]):
        name = args[args.index("--name") + 1] if args[1] == "create" else args[2]
        var = args[args.index("--credential") + 1]
        if "=" in var or var not in os.environ:
            print(f"error: the credential {var} is not an env var of the CLI", file=sys.stderr)
            return 1
        stored = providers / f"{name}.json"
        if args[1] == "update" and not stored.exists():
            print(f"error: provider {name} not found", file=sys.stderr)
            return 1
        kind = args[args.index("--type") + 1] if args[1] == "create" else json.loads(stored.read_text())["type"]
        if kind not in [p.stem for p in profiles.glob("*.json")]:
            print(f"error: no profile {kind}", file=sys.stderr)
            return 1
        digest = hashlib.sha256(os.environ[var].encode()).hexdigest()
        stored.write_text(json.dumps({"name": name, "type": kind, "credential": var, "sha256": digest}))
        return 0
    print(f"fake openshell: unknown call {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
