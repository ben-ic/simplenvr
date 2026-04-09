"""
Orphan FFmpeg cleanup — belt-and-suspenders catch for the case where a
previous SimpleNVR process died (e.g. SIGKILL, crash) without taking its
FFmpeg children with it. Such orphans hold RTSP sessions on cameras and
saturate the small concurrent-client limits on cheap IP cameras.

Called at startup by the RecordingManager and MotionManager before any
new FFmpeg subprocesses are spawned.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger(__name__)


def kill_orphan_ffmpegs() -> int:
    """
    Scan for ffmpeg orphans from previous bare-python dev sessions and
    SIGKILL them. Returns the count killed.

    DEV-MODE ONLY. The Tauri shell wraps every ffmpeg in tether (the
    cross-platform parent-death supervisor) so production never has
    orphans and never calls this function — both call sites are
    gated on `not SIMPLENVR_TETHER_BIN`, which is always set in
    Tauri mode by src-tauri/src/lib.rs.

    Match anchor: `rtsp://127.0.0.1:58554`. SimpleNVR is the only
    thing on the machine that spawns ffmpeg against go2rtc's loopback
    on this specific port (chosen in src-tauri/src/lib.rs and
    backend/dev_go2rtc.py to be in the IANA unassigned range). The
    earlier broad `rtsp://` match would also kill third-party tools
    (OBS Studio, ffplay against an IP camera, Channels DVR, etc.)
    on the developer's machine. The narrowed anchor is the smallest
    substring that uniquely identifies our orphans.

    Edge case not covered: orphans from a session where go2rtc was
    down and ffmpeg fell back to direct camera URLs. These survive
    the sweep — accepted because (a) dev_go2rtc.py spawns go2rtc on
    bare-python startup so the fallback case is rare, and (b) the
    cost of a missed orphan in dev is a manual `pkill ffmpeg`, far
    less annoying than killing the developer's other RTSP tools.
    """
    my_pid = os.getpid()
    killed: list[int] = []

    try:
        if sys.platform == "win32":
            # Windows: use wmic to get pid, parentpid, commandline
            out = subprocess.run(
                ["wmic", "process", "where", "name='ffmpeg.exe'",
                 "get", "ProcessId,ParentProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=5,
            )
            for line in out.stdout.splitlines():
                if "rtsp://127.0.0.1:58554" not in line:
                    continue
                parts = [p.strip() for p in line.split(",")]
                # CSV: Node,CommandLine,ParentProcessId,ProcessId
                if len(parts) < 4:
                    continue
                try:
                    ppid = int(parts[-2])
                    pid = int(parts[-1])
                except ValueError:
                    continue
                if ppid == my_pid:
                    continue
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=2)
                    killed.append(pid)
                except Exception:
                    pass
        else:
            out = subprocess.run(
                ["ps", "-ax", "-o", "pid=,ppid=,command="],
                capture_output=True, text=True, timeout=5,
            )
            for line in out.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split(None, 2)
                if len(parts) < 3:
                    continue
                try:
                    pid = int(parts[0])
                    ppid = int(parts[1])
                except ValueError:
                    continue
                cmd = parts[2]
                if "ffmpeg" not in cmd or "rtsp://127.0.0.1:58554" not in cmd:
                    continue
                if pid == my_pid or ppid == my_pid:
                    continue
                try:
                    # Try the whole process group first
                    try:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.warning("Failed to kill orphan ffmpeg pid=%s: %s", pid, e)
    except Exception as e:
        logger.warning("Orphan ffmpeg scan failed: %s", e)
        return 0

    if killed:
        logger.warning(
            "Killed %d orphan ffmpeg process(es) from previous session: %s",
            len(killed), killed,
        )
    return len(killed)


def kill_orphan_go2rtc() -> int:
    """
    Scan for go2rtc processes left over from a previous SimpleNVR
    session whose Tauri parent died without taking the sidecar with it.

    Skipped entirely if the configured go2rtc admin URL responds —
    that means a live go2rtc is already serving us (whether our Tauri
    shell spawned it or a developer started it manually) and we should
    not touch any go2rtc processes. The parent-pid heuristic below
    would otherwise false-positive in dev mode where Python and go2rtc
    are spawned by separate bash subshells with no common parent and
    one of them looks like an orphan to the other.

    Otherwise: an "orphan" is a go2rtc whose ppid is neither this Python
    process nor our parent. On Unix, an inherited orphan ends up
    reparented to init (ppid=1).

    SimpleNVR is the only thing that should ever spawn go2rtc on this
    machine — there is no shared go2rtc service. We never run on a
    machine where go2rtc is a system daemon.
    """
    if sys.platform == "win32":
        # Windows: rare in this codebase right now and the production
        # parent process model is different (job objects clean up
        # children automatically when the Tauri shell dies). Skip.
        return 0

    # Live-instance guard: if go2rtc is configured and reachable, the
    # running process is legitimate — leave it alone. Uses urllib so
    # process_cleanup.py stays free of async deps.
    admin_url = os.environ.get("SIMPLENVR_GO2RTC_URL")
    if admin_url:
        try:
            import urllib.request
            with urllib.request.urlopen(
                f"{admin_url.rstrip('/')}/api/streams", timeout=1.0
            ) as resp:
                if resp.status < 300:
                    return 0
        except Exception:
            # Not reachable — fall through to the orphan scan + kill.
            pass

    my_pid = os.getpid()
    parent_pid = os.getppid()
    killed: list[int] = []
    try:
        out = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                ppid = int(parts[1])
            except ValueError:
                continue
            cmd = parts[2]
            # Match the Tauri externalBin naming convention so we
            # don't accidentally kill an unrelated user-spawned tool
            # named "go2rtc" — only the SimpleNVR-shipped binaries
            # have the target-triple suffix.
            if "go2rtc-" not in cmd:
                continue
            if pid in (my_pid, parent_pid):
                continue
            if ppid in (my_pid, parent_pid):
                # Live sibling of ours — owned by the Tauri shell.
                continue
            try:
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning("Failed to kill orphan go2rtc pid=%s: %s", pid, e)
    except Exception as e:
        logger.warning("Orphan go2rtc scan failed: %s", e)
        return 0

    if killed:
        logger.warning(
            "Killed %d orphan go2rtc process(es) from previous session: %s",
            len(killed), killed,
        )
    return len(killed)


def terminate_process_group(proc, sig: int = signal.SIGTERM) -> None:
    """
    Send a signal to the process group of `proc`. Falls back to signaling
    just the direct PID if the process group can't be determined (e.g. the
    child was not started with start_new_session=True, or already exited).
    Safe to call on already-dead processes.
    """
    if proc is None or proc.returncode is not None:
        return
    pid = proc.pid
    try:
        if sys.platform == "win32":
            # Windows: subprocess kill handles its job-object children
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
            return
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
    except Exception as e:
        logger.warning("terminate_process_group(pid=%s) failed: %s", pid, e)
