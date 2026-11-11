"""The sweep: delete each run sandbox that is older than its time limit.

A run job deletes its sandbox at the end, also on an error and on SIGTERM. A
run job that is killed with no chance to clean up leaves its sandbox. The CLI
has no lifetime flag for a sandbox (OBS-167), so this command is the time limit:
a CronJob runs it with the client certificate of the trust domain, lists the
sandboxes with the run label, reads the start time from each name, and
deletes each one that is older than SWEEP_MAX_AGE_SECONDS.

Environment: OPENSHELL_GATEWAY_URL, OPENSHELL_MTLS_DIR, OPENSHELL_GATEWAY_NAME
(as for the run job), and SWEEP_MAX_AGE_SECONDS (default 3600).
"""

import logging
import os
import sys
import time

from run_job.job import RUN_LABEL, started_at
from run_job.openshell import OpenShell, OpenShellError


def main(dry_run: bool = False) -> int:
    logging.basicConfig(stream=sys.stderr, level="INFO", format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("sweep")
    url = os.environ.get("OPENSHELL_GATEWAY_URL", "")
    if not url:
        log.error("OPENSHELL_GATEWAY_URL is not set")
        return 2
    max_age = int(os.environ.get("SWEEP_MAX_AGE_SECONDS", "3600"))
    cli = OpenShell(os.environ.get("OPENSHELL_BIN", "openshell"), os.environ.get("OPENSHELL_GATEWAY_NAME", "workflows"))
    try:
        cli.register(url, os.environ.get("OPENSHELL_MTLS_DIR", "/etc/openshell/mtls"))
        names = cli.names(f"{RUN_LABEL}=true")
    except OpenShellError as e:
        log.error("%s", e)
        return 3
    now = time.time()
    failed = 0
    for name in names:
        start = started_at(name)
        if start is None:
            log.warning("%s has the run label and no run name; left as it is", name)
            continue
        age = now - start
        if age <= max_age:
            continue
        log.info("%s is %.0f s old, over %d s: %s", name, age, max_age, "would delete" if dry_run else "deleting")
        if dry_run:
            continue
        try:
            cli.delete(name)
        except OpenShellError as e:
            failed += 1
            log.error("%s", e)
    return 1 if failed else 0
