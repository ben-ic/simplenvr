"""
PyInstaller entry shim.

backend/main.py uses relative imports (`from . import db`) and a relative
import inside its `if __name__ == "__main__":` block, so it cannot itself
be the PyInstaller entry script (PyInstaller would run it as `__main__`,
breaking the relative imports). This shim imports the package properly
and replicates the launch sequence.
"""

from __future__ import annotations


def main() -> None:
    import json
    import os
    import sys
    import uvicorn
    from backend.port_finder import pick_port_with_socket

    port, sock = pick_port_with_socket()

    # Write the port signal directly via os.write on fd 1 to bypass any
    # buffering or stdout redirection that PyInstaller's onefile bootloader
    # may interpose. The Tauri Rust shell reads this line from the child's
    # stdout pipe to discover the bound port.
    signal = json.dumps({"port": port, "ready": True}) + "\n"
    os.write(1, signal.encode("utf-8"))
    try:
        sys.stdout.flush()
    except Exception:
        pass

    uvicorn.run(
        "backend.main:app",
        host=None,
        fd=sock.fileno(),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
