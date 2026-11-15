"""The sweep: remove each run sandbox that is older than its time limit.

A run job deletes its sandbox at the end, also on an error and on SIGTERM. A
run job that is killed with no chance to clean up leaves its sandbox. The CLI
has no lifetime flag for a sandbox (OBS-167), so this command is the time limit.
A CronJob runs it with the client certificate of the trust domain and a
service account that can list and delete Sandbox records in its namespace.

1. Through the gateway: list the sandboxes with the run label, read the start
   time from each name, and delete each one that is older than
   SWEEP_MAX_AGE_SECONDS. Then read its Sandbox record through the Kubernetes
   API: the gateway can answer a delete with success and leave the record
   (OpenShell 0.1.8). A record that stays goes through the API.
2. Through the Kubernetes API: list the run records (records.py), and remove
   each one that the gateway did not list and that is older than the limit by
   both of its clocks (the name and the creationTimestamp). The gateway forgot
   these, so its delete can't remove them.

A record that the gateway left, or forgot, fails the sweep after the API
removes it, so that the alert on a failed CronJob shows it. A young record is
never removed, and a record that the gateway lists is never removed through
the API before the gateway delete. The limit must be more than the time limit
of a run job with its grace period (the admission policy of the run namespace
admits one hour at most), so that a record older than the limit belongs to no
live run. A 404 between a read and a delete counts as gone. The sweep starts
no new sandbox when the rest of its deadline is less than the worst case of
one, and leaves the rest for the next sweep.

Environment: OPENSHELL_GATEWAY_URL, OPENSHELL_MTLS_DIR, OPENSHELL_GATEWAY_NAME
(as for the run job), KUBE_API_URL and POD_NAMESPACE (optional, as for the run
job), SWEEP_MAX_AGE_SECONDS (default 3600), and SWEEP_DEADLINE_SECONDS
(default 420; keep it under the activeDeadlineSeconds of the Job, with room for
the start of the pod).

Exit codes: 0 nothing was left, 1 a sandbox was not removed, the gateway left
or forgot a record, or the deadline left work for the next sweep, 2 the
environment is not complete, 3 neither list worked.
"""

import logging
import os
import sys
import time

from run_job.job import RUN_LABEL, started_at
from run_job.openshell import OpenShell, OpenShellError
from run_job.records import RecordChanged, RecordError, Records, age_seconds, sandbox_of

log = logging.getLogger("sweep")

# The waits of one sandbox, in seconds: the gateway delete, the wait for its
# record, and the removal through the API with its wait. ONE_SANDBOX is their
# worst case, with room for the API calls (15 s each at most).
GATEWAY_DELETE_TIMEOUT = 60
RECORD_WAIT = 45
API_WAIT = 60
ONE_SANDBOX = GATEWAY_DELETE_TIMEOUT + RECORD_WAIT + API_WAIT + 45


def remove(records: Records, record: dict) -> None:
    """Remove a run record through the API, and wait until it is gone."""
    sandbox = sandbox_of(record)
    if not (record.get("metadata") or {}).get("deletionTimestamp"):
        records.delete(record)
    if not records.wait_gone(sandbox, API_WAIT):
        raise RecordError(f"the Sandbox record is still there {API_WAIT} s after its delete through the API")


class Sweep:
    def __init__(self, cli: OpenShell, records: Records, max_age: int, deadline: float, dry_run: bool):
        self.cli = cli
        self.records = records
        self.max_age = max_age
        self.deadline = deadline
        self.dry_run = dry_run
        self.start = time.monotonic()
        self.failed = 0

    def time_left(self, what: str, left: int) -> bool:
        if time.monotonic() - self.start + ONE_SANDBOX <= self.deadline:
            return True
        log.error("the deadline of the sweep: %d %s left for the next sweep", left, what)
        self.failed += 1
        return False

    def through_gateway(self, name: str) -> None:
        """Delete an old run sandbox through the gateway, then check its record."""
        gateway_ok = True
        try:
            self.cli.delete(name, timeout=GATEWAY_DELETE_TIMEOUT)
        except OpenShellError as e:
            if "not found" not in str(e).lower():
                gateway_ok = False
                log.error("%s: the gateway delete failed: %s", name, e)
                self.failed += 1
        try:
            if self.records.wait_gone(name, RECORD_WAIT):
                log.info("%s deleted: its Sandbox record is gone", name)
                return
            for record in self.records.of_sandbox(name):
                remove(self.records, record)
        except RecordError as e:
            log.error("%s: the Sandbox record after the gateway delete: %s; left for the next sweep", name, e)
            self.failed += 1
            return
        if gateway_ok:
            log.error("%s: the gateway answered the delete and left the Sandbox record; removed through the API", name)
            self.failed += 1
        else:
            log.info("%s: removed through the API after the gateway delete failed", name)

    def forgotten(self, record: dict) -> None:
        """Remove a run record that the gateway does not list. The gateway lost
        the sandbox, so the sweep fails, also when the removal works."""
        sandbox = sandbox_of(record)
        self.failed += 1
        try:
            remove(self.records, record)
        except RecordChanged as e:
            log.error("%s: %s; left for the next sweep", sandbox, e)
        except RecordError as e:
            log.error("%s: a Sandbox record that the gateway does not list, not removed: %s", sandbox, e)
        else:
            log.error("%s: a Sandbox record that the gateway does not list; removed through the API", sandbox)

    def run(self) -> int:
        now = time.time()
        listed: list[str] | None = None
        runs: list[dict] | None = None
        try:
            listed = self.cli.names(f"{RUN_LABEL}=true")
        except OpenShellError as e:
            log.error("the list of the gateway: %s", e)
            self.failed += 1
        try:
            runs = self.records.runs()
        except RecordError as e:
            log.error("the list of the Sandbox records: %s", e)
            self.failed += 1
        if listed is None and runs is None:
            return 3

        old = []
        for name in listed or []:
            start = started_at(name)
            if start is None:
                log.warning("%s has the run label and no run name; left as it is", name)
                continue
            if now - start > self.max_age:
                old.append((name, now - start))
        action = "would delete" if self.dry_run else "deleting"
        for i, (name, age) in enumerate(old):
            if not self.dry_run and not self.time_left("sandboxes of the gateway are", len(old) - i):
                return 1
            log.info("%s is %.0f s old, over %d s: %s", name, age, self.max_age, action)
            if not self.dry_run:
                self.through_gateway(name)

        known = set(listed or [])
        lost = []
        for record in runs or []:
            if sandbox_of(record) in known:
                continue  # the gateway knows it: the old ones went above, a young one belongs to a live run
            age = age_seconds(record, now)
            if age is not None and age > self.max_age:
                lost.append((record, age))
        action = "would remove it through the API" if self.dry_run else "removing it through the API"
        for i, (record, age) in enumerate(lost):
            if not self.dry_run and not self.time_left("Sandbox records are", len(lost) - i):
                return 1
            log.info(
                "%s: the gateway does not list it, and its Sandbox record is %.0f s old, over %d s: %s",
                sandbox_of(record),
                age,
                self.max_age,
                action,
            )
            if not self.dry_run:
                self.forgotten(record)
        return 1 if self.failed else 0


def main(dry_run: bool = False) -> int:
    logging.basicConfig(stream=sys.stderr, level="INFO", format="%(levelname)s %(name)s: %(message)s")
    url = os.environ.get("OPENSHELL_GATEWAY_URL", "")
    if not url:
        log.error("OPENSHELL_GATEWAY_URL is not set")
        return 2
    max_age = int(os.environ.get("SWEEP_MAX_AGE_SECONDS", "3600"))
    deadline = float(os.environ.get("SWEEP_DEADLINE_SECONDS", "420"))
    try:
        records = Records()
    except RecordError as e:
        log.error("the Kubernetes API: %s", e)
        return 2
    cli = OpenShell(os.environ.get("OPENSHELL_BIN", "openshell"), os.environ.get("OPENSHELL_GATEWAY_NAME", "workflows"))
    try:
        cli.register(url, os.environ.get("OPENSHELL_MTLS_DIR", "/etc/openshell/mtls"))
    except OpenShellError as e:
        log.error("%s", e)
        return 3
    return Sweep(cli, records, max_age, deadline, dry_run).run()
