"""
PyInstaller entry shim.

backend/main.py uses relative imports (`from . import db`) and a relative
import inside its launch function, so it cannot itself be the PyInstaller
entry script — PyInstaller would run it as `__main__` and break the
relative imports. This shim imports the backend package properly and
delegates to the shared `_launch_sidecar()` function defined at module
level in `backend.main`.

Historical note: this file used to duplicate the launch sequence inline,
which drifted from `backend/main.py`'s `__main__` block over time. The
canonical example that caused the 2026-04-10 bundled-mode live-preview
regression was `os.environ["SIMPLENVR_BACKEND_PORT"] = str(port)` —
added to `backend/main.py`'s __main__ block in session 9 but never
mirrored here. The bundled binary never set that env var, so
`backend/api/ws.py` fell back to emitting a relative `/g2r`
go2rtc_base_url, the Tauri WebView resolved it against
`tauri://localhost`, and the vendored `video-rtc.js` URL-scheme
converter then mangled it into `wsi://localhost/...` because its
substring(4) assumed "http" was the only 4-character protocol it
would ever see. Collapsing both entry points onto a single
`_launch_sidecar()` function makes that class of drift structurally
impossible — future changes land in one place and both invocations
pick them up.
"""

from __future__ import annotations


def main() -> None:
    # Import the shared launch function from the properly-initialized
    # backend package. This is what the historical inline duplication
    # should have been from day one.
    from backend.main import _launch_sidecar

    _launch_sidecar()


if __name__ == "__main__":
    main()
