"""Local run-controller exclusion and attempt records; never a device-state ledger."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

from .wire import fingerprint


@contextmanager
def device_session(profile, *, run_id=None):
    """Exclude another local runner and reject automatic replay of the same run.

    External orchestrators still own their device lock. This local advisory lock
    cannot exclude a controller running on another host.
    """
    import fcntl  # Supported control hosts: macOS and Ubuntu.

    directory = Path(os.environ.get("MATTERIX_BRIDGE_STATE", Path.home() / ".local/state/matterix-bridge"))
    directory.mkdir(parents=True, exist_ok=True)
    identity = profile["real"]["server_uuid"]
    if not identity:
        raise ValueError("Bind a SiLA server UUID before acquiring execution control")
    key = fingerprint(identity)
    with (directory / f"{key}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another local run controller owns this instrument") from error
        try:
            if run_id is not None:
                attempt = directory / f"{fingerprint([identity, run_id])}.attempt.json"
                try:
                    with attempt.open("x") as file:
                        json.dump({"run_id": run_id, "profile_sha256": fingerprint(profile)}, file)
                except FileExistsError as error:
                    raise RuntimeError(
                        "This run was already attempted. Inspect the result and review a new run; no replay."
                    ) from error
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
