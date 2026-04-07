"""
Port selection for the SimpleNVR backend.

Picks an available TCP port starting at DEFAULT_START_PORT and incrementing
on conflict. The pick_port_with_socket() variant returns the already-bound
socket alongside the port number, eliminating the TOCTOU window between
probing and the server binding — the caller passes sock.fileno() to uvicorn
via the fd= parameter, and the OS holds the port continuously.

DEFAULT_START_PORT (57321) is in the IANA dynamic range (49152-65535), is
not assigned to any registered service, and is deliberately non-round to
avoid collisions with other self-hosted apps that gravitate to 50000/55000/
60000.
"""

from __future__ import annotations

import socket

DEFAULT_START_PORT = 57321
MAX_ATTEMPTS = 100


def pick_port(start_port: int = DEFAULT_START_PORT) -> int:
    """
    Find the first available TCP port on 127.0.0.1 starting at start_port.

    Probes by binding a temporary socket and immediately closing it, so
    there is a small TOCTOU window between returning and the caller's
    actual bind. Use pick_port_with_socket() when that matters.
    """
    for attempt in range(MAX_ATTEMPTS):
        port = start_port + attempt
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            sock.close()
            continue
    raise RuntimeError(
        f"No available port in {MAX_ATTEMPTS} attempts starting at {start_port}"
    )


def pick_port_with_socket(
    start_port: int = DEFAULT_START_PORT,
) -> tuple[int, socket.socket]:
    """
    Find an available port and return the already-bound socket alongside.

    The caller takes ownership of the returned socket — either close it or
    inherit its fd in a server (e.g., uvicorn.run(fd=sock.fileno(), ...)).
    The OS holds the port continuously from this bind until the inheriting
    server accepts on the same fd, so there is no race.
    """
    for attempt in range(MAX_ATTEMPTS):
        port = start_port + attempt
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            return (port, sock)
        except OSError:
            sock.close()
            continue
    raise RuntimeError(
        f"No available port in {MAX_ATTEMPTS} attempts starting at {start_port}"
    )
