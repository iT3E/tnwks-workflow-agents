"""Write the provider profiles of the test cluster.

Usage, in the image under test, in the setup Job:
    profiles.py <dir of the example profiles> <out dir> <fake GitHub host> <fake GitHub port>

The profile of the model gateway is the example as it is: the test cluster has
a fake model gateway at its address. The profile of GitHub gets the address of
the fake GitHub (plain HTTP) in place of api.github.com, with the same rules.
"""

import sys
from pathlib import Path

import yaml

NAMES = ("workflow-model-gateway.yaml", "workflow-github-reviewer.yaml")


def main() -> int:
    src, out, host, port = sys.argv[1:5]
    Path(out).mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        profile = yaml.safe_load(Path(src, name).read_text())
        if profile["id"] == "workflow-github-reviewer":
            for endpoint in profile["endpoints"]:
                endpoint.update(host=host, port=int(port), tls="")
        Path(out, name).write_text(yaml.safe_dump(profile, sort_keys=False))
        print(f"== {name}\n{Path(out, name).read_text()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
