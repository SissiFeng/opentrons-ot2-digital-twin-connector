"""Installation credentials owned by site operators, separate from browser login."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile


def credential_digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def registry_path(entry):
    return Path(entry.get("credentials_file") or (str(entry["token_file"]) + ".credentials.json"))


def read_registry(entry):
    path = registry_path(entry)
    if not path.exists():
        # Legacy installations remain usable until the first explicit rotation.
        from .gateway import private_secret

        return {"default": credential_digest(private_secret(entry["token_file"]))}
    if path.stat().st_mode & 0o077:
        raise ValueError("Gateway credential registry must be private (chmod 600)")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for key, digest in value.items()
    ):
        raise ValueError("Invalid gateway credential registry")
    return value


def write_registry(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def issue(entry, gateway_id, token_output, *, replace=False):
    import fcntl

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", gateway_id):
        raise ValueError("Gateway ID must be 1-64 letters, digits, dots, underscores or hyphens")
    path = registry_path(entry)
    with Path(str(path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = read_registry(entry)
        if gateway_id in registry and not replace:
            raise ValueError("Gateway ID already exists; use --replace after stopping the old gateway")
        if replace:
            registry = {}
        token = secrets.token_urlsafe(32)
        fd = os.open(token_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        registry[gateway_id] = credential_digest(token)
        write_registry(path, registry)
