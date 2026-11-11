"""Print the list of agents of the run receiver for the test cluster, as JSON.

Usage, in the image under test (bin/image-test runs it with docker):
    render.py <image ref> <fake GitHub URL>

It loads examples/agents.yaml with the code of the receiver, and changes the
template of each agent for the test cluster only: the image under test (loaded
into the node, never pulled), the address of the fake GitHub, and a shorter
time limit. Then it checks the result with the rules of the receiver. JSON is
YAML, so the receiver reads it as its config file.
"""

import json
import sys

sys.path.insert(0, "/app/src")

from run_receiver import config  # noqa: E402

EXAMPLES = "/examples/agents.yaml"


def main() -> int:
    image, github_url = sys.argv[1], sys.argv[2]
    agents = {}
    for name, job in config.load(EXAMPLES).items():
        job["spec"]["activeDeadlineSeconds"] = 900
        container = job["spec"]["template"]["spec"]["containers"][0]
        container["image"] = image
        container["imagePullPolicy"] = "Never"
        env = {e["name"]: e for e in container["env"]}
        env["SANDBOX_IMAGE"]["value"] = image
        env["RUN_TIMEOUT_SECONDS"] = {"name": "RUN_TIMEOUT_SECONDS", "value": "780"}
        env["AGENT_ENV_GITHUB_API_URL"] = {"name": "AGENT_ENV_GITHUB_API_URL", "value": github_url}
        container["env"] = list(env.values())
        agents[name] = {"job": job}
    text = json.dumps({"agents": agents}, indent=1)
    config.parse(text)  # the receiver must accept it
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
