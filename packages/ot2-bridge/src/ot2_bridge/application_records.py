"""Application execution records; never authoritative physical or simulation state."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import threading
import time

from .wire import fingerprint


def qualified_profile(profile):
    """Device/model/calibration identity, excluding only gateway-local routing."""
    value = copy.deepcopy(profile)
    value["real"].pop("host", None)
    value["real"].pop("port", None)
    return value


def plan_identity(plan):
    operations = [{k: v for k, v in op.items() if k != "run_id"} for op in plan["operations"]]
    return {
        "workflow_sha256": fingerprint({"schema": plan["binding"]["schema"], "operations": operations}),
        "qualified_profile_sha256": fingerprint(qualified_profile(plan["profile"])),
        "plan_sha256": fingerprint(plan),
    }


class ApplicationRecords:
    """One Hub owns an output directory. Commit intent before external dispatch.

    A restarted Hub never resumes work. Outstanding runs/requests become holds;
    only an explicit operator reconciliation clears them. Keep this database and
    the gateway's local ledgers when moving or restoring an installation.
    """

    def __init__(self, directory):
        import fcntl

        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        self.owner = (root / ".hub.lock").open("a")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.owner.close()
            raise RuntimeError("Another Hub owns this output directory") from None
        self.lock = threading.RLock()
        self.db = None
        try:
            self._initialize(root)
        except BaseException:
            self.close()
            raise

    def _initialize(self, root):
        self.db = sqlite3.connect(root / "application.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, updated REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, fault TEXT NOT NULL, active_run TEXT);
            CREATE TABLE IF NOT EXISTS dispatch (
                id TEXT PRIMARY KEY, device TEXT, kind TEXT, state TEXT, command TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS reconciliations (created REAL, device TEXT, note TEXT);
        """)
        # Upgrade existing output in place, including interrupted pre-journal
        # runs. Never turn an old plan without a terminal report into idle.
        for path in root.glob("*/plan.json"):
            from .instruments import validate_plan

            plan = validate_plan(json.loads(path.read_text()))
            if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (plan["binding"]["run_id"],)).fetchone():
                continue
            report_file = path.parent / "report.json"
            report = json.loads(report_file.read_text()) if report_file.exists() else {}
            completed = report.get("status") == "completed"
            self.save_job(
                {
                    "run_id": plan["binding"]["run_id"],
                    "profile": plan["profile"],
                    "mode": report.get("mode", "unknown"),
                    "status": "completed" if completed else "held",
                    "recovery_required": not completed,
                    "events": [],
                    "report": report,
                    "output": str(path.parent),
                    **plan_identity(plan),
                }
            )
        with self.lock, self.db:
            for key, data in self.db.execute("SELECT id, data FROM jobs").fetchall():
                job = json.loads(data)
                if job["status"] == "running":
                    job.update(status="held", recovery_required=True)
                    job["report"] = {
                        "status": "held",
                        "error": "Hub restarted during this run. "
                        "Verify the device and native process; no execution was resumed.",
                    }
                    self.db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), key))
            self.db.execute(
                """UPDATE devices SET fault=?
                WHERE active_run IS NOT NULL OR id IN (SELECT device FROM dispatch WHERE state='pending')""",
                ("Hub restarted with an unfinished device session; reconcile before a new run.",),
            )
            self.db.execute("UPDATE dispatch SET state='unknown' WHERE state='pending'")

    def save_job(self, job):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?)", (job["run_id"], time.time(), json.dumps(job))
            )

    def jobs(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM jobs ORDER BY updated DESC")]

    def latest_job(self):
        return next(iter(self.jobs()), {"status": "idle", "events": []})

    def rehearsal(self, plan, run_id=None):
        identity = plan_identity(plan)
        for job in self.jobs():
            if run_id is not None and job["run_id"] != run_id:
                continue
            if (
                job["status"] == "completed"
                and job.get("mode") == "sim-only"
                and all(job.get(k) == identity[k] for k in ("workflow_sha256", "qualified_profile_sha256"))
            ):
                return job["run_id"]
        return None

    def device(self, key):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO devices VALUES (?, '', NULL)", (key,))
            fault, run = self.db.execute("SELECT fault, active_run FROM devices WHERE id=?", (key,)).fetchone()
            return {"fault": fault, "active_run": run}

    def hold(self, key, reason):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO devices VALUES (?, '', NULL)", (key,))
            self.db.execute("UPDATE devices SET fault=? WHERE id=?", (reason, key))

    def intent(self, key, command):
        with self.lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO devices VALUES (?, '', NULL)", (key,))
            if command["kind"] == "begin":
                self.db.execute(
                    "UPDATE devices SET active_run=? WHERE id=?", (command["payload"]["binding"]["run_id"], key)
                )
            self.db.execute(
                "INSERT INTO dispatch VALUES (?, ?, ?, 'pending', ?, NULL)",
                (command["id"], key, command["kind"], json.dumps(command)),
            )

    def result(self, key, command, result):
        with self.lock, self.db:
            self.db.execute(
                "UPDATE dispatch SET state='returned', result=? WHERE id=?", (json.dumps(result), command["id"])
            )
            if command["kind"] == "finish" and not result["error"]:
                self.db.execute("UPDATE devices SET active_run=NULL WHERE id=?", (key,))

    def reconcile(self, key, note):
        with self.lock, self.db:
            self.db.execute("UPDATE devices SET fault='', active_run=NULL WHERE id=?", (key,))
            self.db.execute(
                "UPDATE dispatch SET state='reconciled' WHERE device=? AND state IN ('pending', 'unknown')", (key,)
            )
            self.db.execute("INSERT INTO reconciliations VALUES (?, ?, ?)", (time.time(), key, note))
            for job in self.jobs():
                if job.get("profile", {}).get("device_id") == key and job.get("recovery_required"):
                    job.update(recovery_required=False, reconciliation_note=note)
                    self.db.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job["run_id"]))

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None
            self.owner.close()
