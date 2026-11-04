#!/usr/bin/env python3
"""Human-only hidden prompt for a protected controller credential."""

import getpass
import os
from pathlib import Path
import sys


def store_key(path, key):
    if not key or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("Expected one Runpod API key with no whitespace")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("Refusing a symlinked credential path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("RUNPOD_API_KEY=" + key + "\n")
        handle.flush()
        os.fsync(handle.fileno())


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run with sudo in your own terminal; the file must be protected from agent processes.")
    if not sys.stdin.isatty():
        raise SystemExit("Run interactively in your terminal. Do not pass the key in a command or pipe.")
    # /etc is a standard macOS alias for /private/etc.
    destination = Path("/etc").resolve() / "research-compute/runpod.env"
    try:
        store_key(destination, getpass.getpass("CBAI Runpod API key (hidden): "))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Credential was not stored: {type(exc).__name__}; existing files are never overwritten.")
    print(f"Stored privately at {destination}; no API calls were made.")
