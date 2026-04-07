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
    Scan for ffmpeg processes left over from previous SimpleNVR sessions
    and SIGKILL them. Returns the count of processes killed.

    An "orphan" is any ffmpeg process whose command line contains
    'rtsp://' AND whose parent PID is not the current Python process.
    On a fresh machine with no other RTSP tools running, this is safe.
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
                if "rtsp://" not in line:
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
                if "ffmpeg" not in cmd or "rtsp://" not in cmd:
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
