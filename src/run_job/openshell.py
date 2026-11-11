"""The calls of the run job to the OpenShell CLI, the client of the sandbox gateway.

The CLI talks to the sandbox gateway of the trust domain with the client
certificate of that trust domain (mTLS). The run job copies the certificate
files from the Secret mount to the config directory of the CLI, and registers
the gateway under one name. These are the steps of the test rig (RUN-013).
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

CERT_FILES = ("ca.crt", "tls.crt", "tls.key")


class OpenShellError(RuntimeError):
    pass


class OpenShell:
    def __init__(self, binary: str, gateway: str):
        self.binary = binary
        self.gateway = gateway

    def _run(self, args: list[str], timeout: float, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.binary, "--color", "never", "-g", self.gateway, *args]
        log.info("openshell %s", " ".join(args[:2]))
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise OpenShellError(f"openshell {' '.join(args[:2])} took more than {timeout:.0f} s") from e
        if check and done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
            raise OpenShellError(f"openshell {' '.join(args[:2])} exited {done.returncode}: {' | '.join(tail)}")
        return done

    def register(self, endpoint: str, mtls_dir: str) -> None:
        """Copy the client certificate to the CLI config, and add the gateway."""
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        target = config / "openshell" / "gateways" / self.gateway / "mtls"
        target.mkdir(parents=True, exist_ok=True)
        for name in CERT_FILES:
            source = Path(mtls_dir) / name
            if not source.is_file():
                raise OpenShellError(f"no {name} in {mtls_dir}")
            shutil.copyfile(source, target / name)
            (target / name).chmod(0o600)
        cmd = [self.binary, "--color", "never", "gateway", "add", endpoint, "--local", "--name", self.gateway]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
            raise OpenShellError(f"openshell gateway add exited {done.returncode}: {' | '.join(tail)}")

    def create(
        self,
        name: str,
        image: str,
        command: list[str],
        providers: list[str],
        policy: str,
        env: dict[str, str],
        labels: dict[str, str],
        cpu: str = "",
        memory: str = "",
        timeout: float = 300,
    ) -> None:
        args = ["sandbox", "create", "--name", name, "--from", image, "--policy", policy]
        for p in providers:
            args += ["--provider", p]
        for key, value in env.items():
            args += ["--env", f"{key}={value}"]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        if cpu:
            args += ["--cpu", cpu]
        if memory:
            args += ["--memory", memory]
        args += ["--no-auto-providers", "--detach", "--no-tty", "--", *command]
        self._run(args, timeout)

    def forward_start(self, port: int, name: str) -> None:
        self._run(["forward", "start", str(port), name, "--background"], timeout=60)

    def forward_stop(self, port: int, name: str) -> None:
        self._run(["forward", "stop", str(port), name], timeout=30, check=False)

    def delete(self, name: str, timeout: float = 120) -> None:
        self._run(["sandbox", "delete", name], timeout)

    def names(self, selector: str) -> list[str]:
        done = self._run(["sandbox", "list", "--names", "--selector", selector], timeout=60)
        return [n.strip() for n in done.stdout.splitlines() if n.strip()]
