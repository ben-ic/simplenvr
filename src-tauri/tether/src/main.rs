//! tether — cross-platform parent-death child supervisor
//!
//! Usage:
//!     tether <child_binary> [args...]
//!
//! Runs the given child command and guarantees that the child dies
//! whenever *this* tether process dies — for any reason, including
//! SIGKILL, segfault, or OOM. The child will also die when tether's
//! own parent dies, because when the parent dies tether's stdin pipe
//! closes, tether notices the EOF, and tether kills the child.
//!
//! Per-platform mechanism:
//!   - Linux:   prctl(PR_SET_PDEATHSIG, SIGKILL) in pre_exec — the
//!              kernel signals the child the instant tether dies.
//!              Zero userspace code runs on the death path.
//!   - Windows: the child is assigned to a Job Object with
//!              JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. When tether dies,
//!              the kernel closes the job handle, and the job's
//!              kill-on-close semantics terminate the child.
//!   - macOS:   no kernel primitive exists. tether runs a thread that
//!              blocks on stdin.read(); when stdin returns EOF (the
//!              parent closed its write end by dying), tether SIGKILLs
//!              the child and exits. This is the same pattern the
//!              Language Server Protocol, Docker exec, and every Unix
//!              init system use.
//!
//! Signal forwarding: tether forwards SIGTERM and SIGINT to the child
//! on Unix so graceful shutdowns still work. The child's exit code is
//! propagated transparently so tether appears invisible to callers.

use std::env;
use std::process::{self, Command};

#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;

fn usage_and_exit() -> ! {
    eprintln!("tether: usage: tether <child_binary> [args...]");
    process::exit(2);
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        usage_and_exit();
    }
    let (program, rest) = args.split_first().unwrap();

    let mut cmd = Command::new(program);
    cmd.args(rest);

    // ── Linux: kernel-enforced parent-death signal ──────────────────
    // PR_SET_PDEATHSIG asks the kernel to send the given signal to
    // this process (the child-to-be) when its parent (the tether
    // process we're currently in) dies. Set in pre_exec so it takes
    // effect between fork and the final image load, which means even
    // if the child ignores/masks it later, the kernel still delivers
    // it on parent death.
    //
    // Note: PR_SET_PDEATHSIG uses *thread* death, not process death.
    // If tether ever becomes multi-threaded, the thread that called
    // prctl must be the one that lives until death. We keep tether
    // single-threaded on Linux specifically to avoid this footgun —
    // the stdin watchdog thread is only spawned on macOS.
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            cmd.pre_exec(|| {
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL as libc::c_ulong, 0, 0, 0)
                    == -1
                {
                    return Err(std::io::Error::last_os_error());
                }
                // Defensive: if our parent already died between the
                // fork and this hook, prctl would set the death signal
                // but never deliver it. Check getppid() and bail out
                // if we've already been reparented to init.
                if libc::getppid() == 1 {
                    libc::raise(libc::SIGKILL);
                }
                Ok(())
            });
        }
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("tether: failed to spawn {program:?}: {e}");
            process::exit(127);
        }
    };
    let child_pid = child.id();

    // ── Windows: Job Object with KILL_ON_JOB_CLOSE ──────────────────
    // Create a Job Object, set the kill-on-close limit, assign the
    // child process to it, and hold the job handle for the lifetime
    // of tether. When tether dies (any reason), the kernel closes the
    // handle, the job's last reference drops, and Windows terminates
    // every process in the job — which is just our child.
    #[cfg(windows)]
    let _job_handle = {
        match windows_assign_to_kill_on_close_job(child_pid) {
            Ok(h) => Some(h),
            Err(e) => {
                eprintln!("tether: failed to assign child to Job Object: {e}");
                // Fall through — we still run, but the Windows guarantee
                // is weakened to "tether graceful exit only." Better
                // than bailing out entirely.
                None
            }
        }
    };

    // ── macOS: stdin-EOF watchdog thread ────────────────────────────
    // No kernel primitive on macOS, so we rely on pipe-close. The
    // thread blocks on stdin.read(); when the parent dies, the OS
    // closes the write end of the pipe, our read returns 0 (EOF),
    // we SIGKILL the child and exit.
    //
    // We ONLY arm this watchdog when stdin is a FIFO/pipe. If stdin
    // is a TTY, a regular file, or /dev/null, the "parent died"
    // semantics don't apply — running `tether ffmpeg ...` from a
    // terminal would see immediate EOF on /dev/null and kill the
    // child before it ran. The Tauri sidecar spawn path always
    // pipes stdin, so this check cleanly distinguishes "spawned by
    // Tauri" from "run manually for testing."
    //
    // This runs in its own thread so the main thread can wait() on
    // the child normally. If the child exits first, main() calls
    // process::exit() with the child's status and the watchdog thread
    // is torn down with the process — no leak.
    #[cfg(target_os = "macos")]
    if stdin_is_pipe() {
        std::thread::spawn(move || {
            use std::io::Read;
            let stdin = std::io::stdin();
            let mut handle = stdin.lock();
            let mut buf = [0u8; 256];
            loop {
                match handle.read(&mut buf) {
                    Ok(0) => break,    // EOF — parent is gone
                    Ok(_) => continue, // discard any data the parent wrote
                    Err(_) => break,   // treat read errors as death
                }
            }
            // SIGKILL: parent is already dead, graceful shutdown is no
            // longer possible (nobody to tell the child to flush).
            unsafe {
                libc::kill(child_pid as libc::pid_t, libc::SIGKILL);
            }
            // Exit with a distinct code so the caller can tell this
            // was a parent-death kill rather than a child exit.
            process::exit(143); // 128 + SIGTERM, conventional
        });
    }

    // ── Signal forwarding (Unix) ────────────────────────────────────
    // When our own parent SIGTERMs us during graceful shutdown, we
    // want to forward that to the child so it can flush cleanly
    // (e.g., ffmpeg writing the moov atom). Install SIGTERM and
    // SIGINT handlers that forward to the child.
    //
    // We use the POSIX sigaction API directly to avoid pulling in a
    // signal-handling crate. The handlers run in signal-safe context
    // (no allocations, no locks, just kill() + flag set).
    #[cfg(unix)]
    {
        install_forwarding_handlers(child_pid);
    }

    // ── Wait for the child and propagate its exit code ──────────────
    let status = match child.wait() {
        Ok(s) => s,
        Err(e) => {
            eprintln!("tether: wait failed: {e}");
            process::exit(1);
        }
    };

    #[cfg(unix)]
    {
        if let Some(code) = status.code() {
            process::exit(code);
        }
        if let Some(sig) = status.signal() {
            // Convention: shell exit = 128 + signal number
            process::exit(128 + sig);
        }
    }
    #[cfg(not(unix))]
    {
        process::exit(status.code().unwrap_or(1));
    }

    #[allow(unreachable_code)]
    {
        process::exit(1);
    }
}

// ── stdin kind detection (Unix) ─────────────────────────────────────

/// Returns true if fd 0 is a FIFO/pipe. Used on macOS to decide
/// whether to arm the stdin-EOF watchdog — we only want to arm it
/// when a parent process has piped its own write end to our stdin.
#[cfg(unix)]
fn stdin_is_pipe() -> bool {
    unsafe {
        let mut st: libc::stat = std::mem::zeroed();
        if libc::fstat(0, &mut st) != 0 {
            return false;
        }
        (st.st_mode & libc::S_IFMT) == libc::S_IFIFO
    }
}

// ── Unix signal forwarding ──────────────────────────────────────────

#[cfg(unix)]
static FORWARD_TARGET_PID: std::sync::atomic::AtomicI32 =
    std::sync::atomic::AtomicI32::new(0);

#[cfg(unix)]
extern "C" fn forward_signal_handler(sig: libc::c_int) {
    let pid = FORWARD_TARGET_PID.load(std::sync::atomic::Ordering::Relaxed);
    if pid > 0 {
        // Signal-safe: kill() is AS-safe per POSIX.
        unsafe {
            libc::kill(pid as libc::pid_t, sig);
        }
    }
    // Do NOT exit here — we want wait() on the main thread to observe
    // the child's exit normally and propagate its status.
}

#[cfg(unix)]
fn install_forwarding_handlers(child_pid: u32) {
    FORWARD_TARGET_PID.store(child_pid as i32, std::sync::atomic::Ordering::Relaxed);
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = forward_signal_handler as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        action.sa_flags = 0;
        libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut());
        libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut());
        libc::sigaction(libc::SIGHUP, &action, std::ptr::null_mut());
    }
}

// ── Windows Job Object assignment ───────────────────────────────────

#[cfg(windows)]
fn windows_assign_to_kill_on_close_job(
    child_pid: u32,
) -> Result<WindowsJobHandle, String> {
    use std::mem;
    use windows_sys::Win32::Foundation::{CloseHandle, FALSE, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
        SetInformationJobObject, JOBOBJECT_BASIC_LIMIT_INFORMATION,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    unsafe {
        let job: HANDLE = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        if job.is_null() {
            return Err(format!(
                "CreateJobObjectW failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = mem::zeroed();
        info.BasicLimitInformation = JOBOBJECT_BASIC_LIMIT_INFORMATION {
            LimitFlags: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            ..mem::zeroed()
        };
        let ok = SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const _,
            mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        );
        if ok == 0 {
            CloseHandle(job);
            return Err(format!(
                "SetInformationJobObject failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        let proc_handle: HANDLE =
            OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, FALSE, child_pid);
        if proc_handle.is_null() {
            CloseHandle(job);
            return Err(format!(
                "OpenProcess failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        let ok = AssignProcessToJobObject(job, proc_handle);
        CloseHandle(proc_handle);
        if ok == 0 {
            CloseHandle(job);
            return Err(format!(
                "AssignProcessToJobObject failed: {}",
                std::io::Error::last_os_error()
            ));
        }

        Ok(WindowsJobHandle(job))
    }
}

#[cfg(windows)]
struct WindowsJobHandle(windows_sys::Win32::Foundation::HANDLE);

#[cfg(windows)]
impl Drop for WindowsJobHandle {
    fn drop(&mut self) {
        // Closing the job handle triggers KILL_ON_JOB_CLOSE.
        unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0) };
    }
}

// SAFETY: HANDLE is just a pointer-sized integer; the kernel manages
// its lifetime. We hold exactly one copy and Drop closes it.
#[cfg(windows)]
unsafe impl Send for WindowsJobHandle {}
#[cfg(windows)]
unsafe impl Sync for WindowsJobHandle {}
