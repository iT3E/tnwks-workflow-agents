"""Shared parts of the tests: the import path, local servers, and test data.

Each server listens on 127.0.0.1 only. No test calls a real model, GitHub or a
cluster: the model, GitHub, the trace collector, the Kubernetes API and the
OpenShell CLI are fakes in this directory.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.environ.get("APP_SRC") or ("/app/src" if os.path.isdir("/app/src") else os.path.join(REPO, "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
ENTRYPOINT = os.path.join(SRC, "entrypoint.py")
EXAMPLES = os.environ.get("EXAMPLES_DIR") or (
    "/examples" if os.path.isdir("/examples") else os.path.join(REPO, "examples")
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"nothing listens on {port}")


class JsonHandler(BaseHTTPRequestHandler):
    """A handler base: `self.server.app(handler, method, path, body)` returns (status, object or bytes, headers)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _handle(self, method: str):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        self.server.requests.append({"method": method, "path": self.path, "headers": dict(self.headers), "body": raw})
        status, payload, headers = self.server.app(self, method, self.path, raw)
        if payload is None:  # the app wrote the answer itself (a stream)
            return
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", (headers or {}).get("content-type", "application/json"))
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


class Server:
    """An HTTP server in a thread, on a free port of 127.0.0.1."""

    def __init__(self, app, port: int | None = None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port or 0), JsonHandler)
        self.httpd.daemon_threads = True
        self.httpd.app = app
        self.httpd.requests = []
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def requests(self) -> list:
        return self.httpd.requests

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class Process:
    """The entrypoint of the image in a child process, stopped and reaped at the end."""

    def __init__(self, args: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(
            [sys.executable, ENTRYPOINT, *args],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def stop(self) -> tuple[str, str]:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            out, err = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
        return out, err


class Asgi:
    """An ASGI app (the contract, the run receiver) under uvicorn in a thread, on a free port."""

    def __init__(self, app, port: int | None = None):
        import uvicorn

        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False)
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("uvicorn did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=20)


def base_env(**extra: str) -> dict[str, str]:
    """A clean environment for a child process: no variable of the host but the path."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("TMPDIR", "/tmp")}
    env.update(extra)
    return env
