"""The result line of a run job, which the run receiver reads from the log of the job pod.

The run job prints exactly one line that starts with PREFIX, as its last line on
standard output. Each other line of the run job goes to standard error.
"""

import json

PREFIX = "workflow-run-result: "

SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"
RUNNING = "running"


def line(result: dict) -> str:
    return PREFIX + json.dumps(result, separators=(",", ":"), sort_keys=True)


def find(log_text: str) -> dict | None:
    """The last result line of a log, or None."""
    for raw in reversed(log_text.splitlines()):
        at = raw.find(PREFIX)
        if at == -1:
            continue
        try:
            value = json.loads(raw[at + len(PREFIX) :])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    return None
