"""The providers of the workflow agents, in the store of the sandbox gateway.

    entrypoint.py providers

The CronJob workflow-providers of tnwks-ops runs it every 15 minutes. It was
scripts/providers.sh there; the steps, the log lines and the exit codes are
the same, so the alerts and the runbook that read them still hold. Each run:

  1. checks that the gateway answers (`profile list`);
  2. imports each profile of PROVIDER_PROFILES_DIR that is not in the store,
     and updates each one whose file changed (it keeps the sha256 of the file
     in the annotation workflow-agents/source-sha256 of the profile);
  3. updates (or creates) model-gateway with MODEL_GATEWAY_KEY;
  4. gets a new installation token of the GitHub App of the reviewer
     (github_app.py);
  5. updates (or creates) github-reviewer with that token.

A failure that a new token can't fix (the gateway, a profile, the model
provider) stops the run before the token call. Each failure logs one line,
    workflow-providers result=failed step=<step> name=<name>
with step gateway, profile-export, profile-import, profile-update,
provider-create, provider-update or github-token, and exits 1. A run that
ends well logs "workflow-providers result=ok" and exits 0. A missing
variable logs step=config and exits 2. Each line starts at the first column,
because the log pipeline of tnwks-ops reads "<check> result=<result>" there.

No `profile lint`: import and update check the profile themselves, and lint
fails on a profile that is already in the store (in OpenShell 0.1.7 and
0.1.8, lint runs the conflict check of import). An update needs the
resource_version of the stored profile, so it goes through an export.

Each credential is an env var of the CLI (`--credential NAME`, an env
lookup), so no value is on a command line or in the log.

Environment:
    OPENSHELL_GATEWAY_URL   the sandbox gateway of the trust domain
    OPENSHELL_MTLS_DIR      the client certificate. Default /etc/openshell/mtls
    OPENSHELL_GATEWAY_NAME  the name of the gateway in the CLI config. Default workflows
    PROVIDER_PROFILES_DIR   the provider profiles (*.yaml). Default /etc/workflow-providers/profiles
    MODEL_GATEWAY_KEY       the key of the trust domain at the model gateway
    GITHUB_APP_*, GITHUB_REPOSITORY, GITHUB_API_URL   as in github_app.py
"""

import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml

from providers import github_app
from run_job.openshell import OpenShell, OpenShellError

ANNOTATION = "workflow-agents/source-sha256"
# name, profile, the env var of the credential
MODEL_PROVIDER = ("model-gateway", "workflow-model-gateway", "MODEL_GATEWAY_KEY")
GITHUB_PROVIDER = ("github-reviewer", "workflow-github-reviewer", "GITHUB_TOKEN")
ID_LINE = re.compile(r"^id: *(.*)$", re.MULTILINE)


class Failed(Exception):
    def __init__(self, step: str, name: str, code: int = 1):
        super().__init__(f"{step} {name}")
        self.step, self.name, self.code = step, name, code


def say(line: str) -> None:
    print(line, flush=True)


def tell(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


class Sync:
    def __init__(
        self,
        cli: OpenShell,
        profiles: Path,
        environ: dict[str, str],
        mint: Callable[[dict[str, str]], str] = github_app.mint,
    ):
        self.cli = cli
        self.profiles = profiles
        self.environ = environ
        self.mint = mint

    def osh(self, step: str, name: str, args: list[str], env: dict[str, str] | None = None) -> str:
        """One CLI call that must work; its error goes to the log, then the failed line."""
        try:
            return self.cli.call(args, env=env).stdout
        except OpenShellError as e:
            tell(str(e))
            raise Failed(step, name) from e

    def exists(self, args: list[str]) -> bool:
        try:
            return self.cli.call(args, check=False).returncode == 0
        except OpenShellError:
            return False

    def profile_file(self, src: Path, digest: str, version: int, out: Path) -> None:
        """The profile as the gateway gets it: the file with the annotation and,
        for an update, the resource_version of the stored profile."""
        profile = yaml.safe_load(src.read_text(encoding="utf-8"))
        profile.setdefault("annotations", {})[ANNOTATION] = digest
        if version:
            profile["resource_version"] = version
        out.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    def profile(self, src: Path, work: Path) -> None:
        text = src.read_text(encoding="utf-8")
        found = ID_LINE.search(text)
        pid = found.group(1) if found else ""
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        out = work / f"profile-{pid}.yaml"
        if not self.exists(["profile", "describe", pid]):
            try:
                self.profile_file(src, digest, 0, out)
            except (OSError, yaml.YAMLError, AttributeError) as e:
                tell(f"profile {pid}: {e}")
                raise Failed("profile-import", pid) from e
            self.osh("profile-import", pid, ["profile", "import", "-f", str(out)])
            say(f"profile {pid}: imported")
            return
        exported = self.osh("profile-export", pid, ["profile", "export", pid, "-o", "json"])
        try:
            stored = json.loads(exported)
            stored_digest = (stored.get("annotations") or {}).get(ANNOTATION) or "-"
            version = int(stored.get("resource_version") or 0)
        except (ValueError, AttributeError, TypeError) as e:
            tell(f"profile {pid}: the export is not a profile: {e}")
            raise Failed("profile-export", pid) from e
        if stored_digest == digest:
            say(f"profile {pid}: unchanged")
            return
        try:
            self.profile_file(src, digest, version, out)
        except (OSError, yaml.YAMLError, AttributeError) as e:
            tell(f"profile {pid}: {e}")
            raise Failed("profile-update", pid) from e
        self.osh("profile-update", pid, ["profile", "update", "-f", str(out), pid])
        say(f"profile {pid}: updated")

    def provider(self, name: str, profile: str, var: str, env: dict[str, str] | None = None) -> None:
        if self.exists(["provider", "get", name]):
            self.osh("provider-update", name, ["provider", "update", name, "--credential", var], env)
            say(f"provider {name}: updated")
        else:
            self.osh(
                "provider-create",
                name,
                ["provider", "create", "--name", name, "--type", profile, "--credential", var],
                env,
            )
            say(f"provider {name}: created")

    def run(self) -> None:
        url = self.environ["OPENSHELL_GATEWAY_URL"]
        self.osh("gateway", url, ["profile", "list"])
        with tempfile.TemporaryDirectory(prefix="providers-") as tmp:
            for src in sorted(self.profiles.glob("*.yaml")):
                self.profile(src, Path(tmp))
        self.provider(*MODEL_PROVIDER)
        # The token goes into the env of the CLI call, and nowhere else.
        try:
            token = self.mint(self.environ)
        except github_app.TokenError as e:
            tell(f"github app token: {e}")
            raise Failed("github-token", GITHUB_PROVIDER[0]) from e
        name, profile, var = GITHUB_PROVIDER
        self.provider(name, profile, var, env=dict(os.environ) | {var: token})
        say("workflow-providers result=ok")


def main(environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    try:
        url = environ.get("OPENSHELL_GATEWAY_URL", "")
        if not url:
            raise Failed("config", "OPENSHELL_GATEWAY_URL", code=2)
        cli = OpenShell(environ.get("OPENSHELL_BIN", "openshell"), environ.get("OPENSHELL_GATEWAY_NAME", "workflows"))
        try:
            cli.register(url, environ.get("OPENSHELL_MTLS_DIR", "/etc/openshell/mtls"))
        except (OpenShellError, OSError) as e:
            tell(str(e))
            raise Failed("gateway", url) from e
        profiles = Path(environ.get("PROVIDER_PROFILES_DIR", "/etc/workflow-providers/profiles"))
        Sync(cli, profiles, environ).run()
    except Failed as f:
        tell(f"workflow-providers result=failed step={f.step} name={f.name}")
        return f.code
    return 0
