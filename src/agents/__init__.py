"""The workflow agents of this image, one package each.

A package gives:

    NAME                          the name of the agent, as in AGENT_NAME and in the list of the run receiver
    check_config() -> None        raises contract.ConfigError when the environment is not complete
    async invoke(input) -> dict   one run: the `input` object of the contract in, the `output` object out.
                                  Raises contract.InputError for an input that is not valid.

A new agent is one more package here and one more line in AGENTS.
"""

import importlib
import re

AGENTS = {
    "renovate-reviewer": "agents.renovate_reviewer",
}

NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")


class UnknownAgent(LookupError):
    pass


def names() -> list[str]:
    return sorted(AGENTS)


def load(name: str):
    if name not in AGENTS:
        raise UnknownAgent(f"no workflow agent {name!r}; this image has {', '.join(names())}")
    module = importlib.import_module(AGENTS[name])
    if module.NAME != name:
        raise UnknownAgent(f"the package {AGENTS[name]} names itself {module.NAME!r}, not {name!r}")
    return module
