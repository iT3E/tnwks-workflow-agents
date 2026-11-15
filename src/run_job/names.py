"""The name of a run sandbox: run-<7 hex of the key>-<start time in base 36>.

The sweep reads the start time back from the name. OpenShell 0.1.7 refuses a
sandbox name longer than 19 characters (seen on the kind cluster of CI):
run-<7 hex>-<6 or 7 base36> is 18 or 19.
"""

import hashlib
import re

MAX_NAME_LENGTH = 19
NAME_PATTERN = re.compile(r"^run-[0-9a-f]{7}-[0-9a-z]{1,7}$")


def base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def sandbox_name(key: str, now: float) -> str:
    """run-<7 hex of the key>-<start time in base 36>."""
    return f"run-{hashlib.sha256(key.encode()).hexdigest()[:7]}-{base36(int(now))}"


def started_at(name: str) -> int | None:
    if not NAME_PATTERN.match(name):
        return None
    return int(name.rsplit("-", 1)[1], 36)
