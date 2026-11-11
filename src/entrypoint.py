"""The one entrypoint of the image.

    entrypoint.py [serve]      the container contract for the agent that AGENT_NAME names

With no argument it serves the contract, so a runtime that starts the image
with no command gets the agent that AGENT_NAME names. Python puts the
directory of this file on the import path, so the image needs no PYTHONPATH.
"""

import argparse
import sys

# Before anything imports Strands: spans carry no prompt, answer or tool text.
from contract import telemetry

telemetry.enforce_redaction()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="entrypoint.py")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="serve the container contract (the default)")
    parser.parse_args(argv)

    from contract import server

    return server.main()


if __name__ == "__main__":
    sys.exit(main())
