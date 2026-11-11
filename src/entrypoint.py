"""The one entrypoint of the image.

    entrypoint.py [serve]      the container contract for the agent that AGENT_NAME names
    entrypoint.py run-job      one run: a sandbox, the input, the answer, the delete
    entrypoint.py sweep        delete each run sandbox that is older than its time limit

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
    sub.add_parser("run-job", help="run one workflow agent in a sandbox")
    sweep = sub.add_parser("sweep", help="delete run sandboxes past their time limit")
    sweep.add_argument("--dry-run", action="store_true", help="list, delete nothing")
    args = parser.parse_args(argv)

    command = args.command or "serve"
    if command == "serve":
        from contract import server

        return server.main()
    if command == "run-job":
        from run_job import job

        return job.main()
    from run_job import sweep as sweeper

    return sweeper.main(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
