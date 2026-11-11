"""The container contract of a workflow agent: POST /invocations and GET /ping on port 8080.

The contract is the one of the reference model (a managed agent runtime): the
request body is `{"input": {...}}`, the answer is `{"output": {...}}`, and
`/ping` reports `Healthy`, or `HealthyBusy` while a run is in progress.
"""


class InputError(ValueError):
    """The input of a run is not valid. The contract answers 400 with the reason."""


class ConfigError(RuntimeError):
    """The environment of the agent is not complete. The agent does not start."""
