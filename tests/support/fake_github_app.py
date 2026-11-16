"""A fake of the GitHub App API that the providers command calls. Test data only.

    GET  /app                                       the permissions of the App
    GET  /repos/{owner}/{name}/installation         its installation on the repository
    POST /app/installations/{id}/access_tokens      an installation token

Each call must carry a JWT of the App (RS256, `iss` the App ID). With a public
key, the fake checks the signature with openssl. The token it mints is the
test token of fake_github, so the reviewer can use it against that fake.
"""

import base64
import json
import subprocess
import tempfile
import time

from . import fake_github

APP_ID = "12345"
INSTALLATION_ID = 77
PREFIXES = ("/app", f"/repos/{fake_github.REPO}/installation")


def is_app_path(path: str) -> bool:
    p = path.split("?", 1)[0]
    return p == "/app" or p.startswith("/app/") or p == PREFIXES[1]


def unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGitHubApp:
    def __init__(self, permissions: dict | None = None, public_key: str | None = None):
        wanted = permissions or {"contents": "read", "pull_requests": "write", "checks": "read"}
        self.app_permissions = dict(wanted) | {"metadata": "read"}
        self.installation_permissions = dict(wanted) | {"metadata": "read"}
        self.repository_selection = "selected"
        self.token_permissions: dict | None = None  # None: what the call asks, and metadata
        self.public_key = public_key
        self.minted: list[dict] = []
        # path: bytes that the fake answers with 200 in place of its JSON
        self.raw: dict[str, bytes] = {}

    def jwt_ok(self, header: str) -> bool:
        if not header.startswith("Bearer "):
            return False
        parts = header[len("Bearer ") :].split(".")
        if len(parts) != 3:
            return False
        try:
            claims = json.loads(unb64(parts[1]))
        except ValueError:
            return False
        now = time.time()
        if str(claims.get("iss")) != APP_ID or not claims.get("iat", now + 1) <= now < claims.get("exp", 0):
            return False
        if self.public_key is None:
            return True
        with tempfile.NamedTemporaryFile() as sig:
            sig.write(unb64(parts[2]))
            sig.flush()
            done = subprocess.run(
                ["openssl", "dgst", "-sha256", "-verify", self.public_key, "-signature", sig.name],
                input=f"{parts[0]}.{parts[1]}".encode(),
                capture_output=True,
                check=False,
            )
        return done.returncode == 0

    def app(self, handler, method, path, raw):
        if not self.jwt_ok(handler.headers.get("authorization") or ""):
            return 401, {"message": "A JSON web token could not be decoded"}, None
        p = path.split("?", 1)[0]
        if p in self.raw:
            return 200, self.raw[p], {"content-type": "text/html"}
        if method == "GET" and p == "/app":
            return 200, {"id": int(APP_ID), "permissions": self.app_permissions}, None
        if method == "GET" and p == PREFIXES[1]:
            return (
                200,
                {
                    "id": INSTALLATION_ID,
                    "permissions": self.installation_permissions,
                    "repository_selection": self.repository_selection,
                },
                None,
            )
        if method == "POST" and p == f"/app/installations/{INSTALLATION_ID}/access_tokens":
            body = json.loads(raw or b"{}")
            self.minted.append(body)
            granted = self.token_permissions
            if granted is None:
                granted = dict(body.get("permissions") or {}) | {"metadata": "read"}
            return (
                201,
                {
                    "token": fake_github.TOKEN,
                    "expires_at": "2099-01-01T00:00:00Z",
                    "permissions": granted,
                    "repositories": [{"full_name": fake_github.REPO}],
                },
                None,
            )
        return 404, {"message": "Not Found"}, None
