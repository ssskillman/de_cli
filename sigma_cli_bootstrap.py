#!/usr/bin/env python3
"""Create a Sigma CLI API-key profile from ~/.config/sigma/env if none exists.

sigma 0.2.1 still requires a stored profile for `sigma api …` even when
SIGMA_BASE_URL / SIGMA_CLIENT_ID / SIGMA_CLIENT_SECRET are set. Official
`sigma auth login` is a TUI; this drives the API-key path once.
"""

from __future__ import annotations

import os
import pty
import select
import sys
import time

PROFILE = os.environ.get("DE_SIGMA_PROFILE", "de")
DOWN = b"\x1b[B"
ENTER = b"\r"

REGIONS = [
    "https://aws-api.sigmacomputing.com",
    "https://api.us-a.aws.sigmacomputing.com",
    "https://api.ca.aws.sigmacomputing.com",
    "https://api.eu.aws.sigmacomputing.com",
    "https://api.au.aws.sigmacomputing.com",
    "https://api.uk.aws.sigmacomputing.com",
    "https://api.us.azure.sigmacomputing.com",
    "https://api.eu.azure.sigmacomputing.com",
    "https://api.ca.azure.sigmacomputing.com",
    "https://api.uk.azure.sigmacomputing.com",
    "https://api.sigmacomputing.com",
    "https://api.sa.gcp.sigmacomputing.com",
]


def _read_until(fd: int, needle: bytes, timeout: float = 8.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if fd not in r:
            continue
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if needle in buf:
            return buf
    raise TimeoutError(
        f"timed out waiting for {needle!r} in sigma auth login TUI"
    )


def _type_line(fd: int, text: str) -> None:
    os.write(fd, text.encode() + ENTER)


def bootstrap(sigma_bin: str, base_url: str, client_id: str, client_secret: str) -> None:
    base_url = base_url.rstrip("/")
    downs = 0
    if base_url in REGIONS:
        downs = REGIONS.index(base_url)
    else:
        downs = len(REGIONS)  # "Other: enter custom URL"

    pid, fd = pty.fork()
    if pid == 0:
        os.execv(sigma_bin, [sigma_bin, "auth", "login"])

    try:
        _read_until(fd, b"Authentication method")
        os.write(fd, DOWN + ENTER)
        _read_until(fd, b"Profile name")
        _type_line(fd, PROFILE)
        _read_until(fd, b"Sigma region")
        os.write(fd, DOWN * downs + ENTER)
        if base_url not in REGIONS:
            _read_until(fd, b"http")
            _type_line(fd, base_url)
        _read_until(fd, b"Client ID")
        _type_line(fd, client_id)
        _read_until(fd, b"Secret")
        _type_line(fd, client_secret)
        # Drain completion (do not print — may echo id).
        end = time.time() + 5
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd not in r:
                continue
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        os.waitpid(pid, 0)


def main() -> int:
    sigma_bin = sys.argv[1] if len(sys.argv) > 1 else "sigma"
    base = os.environ.get("SIGMA_BASE_URL", "").strip()
    cid = os.environ.get("SIGMA_CLIENT_ID", "").strip()
    secret = os.environ.get("SIGMA_CLIENT_SECRET", "").strip()
    if not (base and cid and secret):
        print(
            "Error: SIGMA_BASE_URL, SIGMA_CLIENT_ID, SIGMA_CLIENT_SECRET required",
            file=sys.stderr,
        )
        return 1
    print(f"Creating Sigma CLI profile '{PROFILE}' from ~/.config/sigma/env …", file=sys.stderr)
    bootstrap(sigma_bin, base, cid, secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
