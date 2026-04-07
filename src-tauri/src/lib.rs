use std::collections::VecDeque;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::{async_runtime, AppHandle, Emitter, Manager, RunEvent, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Compile-time rustc target triple, forwarded by `build.rs`.
const TARGET_TRIPLE: &str = env!("TARGET_TRIPLE");

/// Default port the backend falls back to in dev mode when no sidecar
/// binary is bundled. Must match `backend.config.DEFAULT_START_PORT`.
const DEV_FALLBACK_PORT: u16 = 57321;

/// Health-check polling: 60 attempts × 250 ms = 15 s budget. Sized to
/// cover PyInstaller's ~10 s cold-start in addition to the FastAPI
/// lifespan (DB init, recorder/scanner startup).
const HEALTH_ATTEMPTS: u32 = 60;
const HEALTH_INTERVAL: Duration = Duration::from_millis(250);

/// How long after spawn we wait for the `{"port": N, "ready": true}`
/// stdout signal before declaring the sidecar wedged. Must be >= the
/// PyInstaller cold-start budget.
const PORT_SIGNAL_TIMEOUT: Duration = Duration::from_secs(30);

/// How many trailing stderr lines to keep for the crash dialog.
const STDERR_RING_CAPACITY: usize = 12;

/// go2rtc loopback endpoints. Hardcoded — go2rtc is bound to these
/// ports via the generated config file, and the Python sidecar reads
/// them from env vars below. If you change these, change the YAML
/// generator and the Python client base URLs in lockstep.
const GO2RTC_API_BASE: &str = "http://127.0.0.1:1984";
const GO2RTC_RTSP_BASE: &str = "rtsp://127.0.0.1:8554";

/// Health-check polling for go2rtc startup. 40 × 250 ms = 10 s budget.
/// go2rtc cold-starts in ~150 ms on Apple Silicon, this is generous.
const GO2RTC_HEALTH_ATTEMPTS: u32 = 40;
const GO2RTC_HEALTH_INTERVAL: Duration = Duration::from_millis(250);

type StderrRing = Arc<Mutex<VecDeque<String>>>;

struct BackendState {
    port: Mutex<u16>,
    child: Mutex<Option<CommandChild>>,
    /// Set true the moment the stdout `ready` signal lands. Used by
    /// the no-port-signal watchdog to decide whether to crash.
    got_port_signal: Arc<Mutex<bool>>,
    /// Last N stderr lines from the sidecar — surfaced in the crash
    /// dialog if startup fails.
    stderr_tail: StderrRing,
    /// go2rtc sidecar handle. None if go2rtc is not bundled (dev) or
    /// failed to start (graceful fallback — Python continues with
    /// direct camera URLs).
    go2rtc_child: Mutex<Option<CommandChild>>,
    /// Stderr ring for go2rtc, kept separately so a go2rtc crash
    /// doesn't pollute the Python sidecar's crash-dialog tail.
    go2rtc_stderr_tail: StderrRing,
}

#[tauri::command]
fn get_backend_port(state: State<'_, BackendState>) -> u16 {
    *state.port.lock().unwrap()
}

/// Resolve the on-disk path to a Tauri-bundled `externalBin` binary.
///
/// In a bundled `.app` / `.exe`, Tauri places external binaries next
/// to the main executable with the target-triple suffix stripped.
/// `BaseDirectory::Resource` does NOT work here — that resolves to
/// `Contents/Resources/`, but `externalBin` files live in
/// `Contents/MacOS/` next to the main binary. In a `cargo build`
/// debug workflow, the files live in `src-tauri/binaries/<name>-<triple>`
/// and are NOT copied next to `target/debug/app`, so we resolve them
/// via `CARGO_MANIFEST_DIR`.
fn external_bin_path(name: &str) -> PathBuf {
    if cfg!(debug_assertions) {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join(format!("{name}-{TARGET_TRIPLE}"))
    } else {
        let exe = std::env::current_exe().expect("current_exe");
        let dir = exe.parent().expect("exe has parent");
        dir.join(name)
    }
}

/// Write a minimal go2rtc YAML config (no streams — discovery scanner
/// adds them at runtime via the HTTP admin API). Returns the path on
/// disk so we can pass `-c` to the sidecar.
fn write_go2rtc_config(data_dir: &std::path::Path) -> std::io::Result<PathBuf> {
    let path = data_dir.join("go2rtc.yaml");
    // listen on 127.0.0.1 only — go2rtc must never be reachable from
    // the LAN, the loopback is an internal implementation detail.
    //
    // The streams key is intentionally omitted (NOT written as
    // `streams: {}` inline mapping). go2rtc's YAML serializer chokes
    // on the inline form when it tries to persist a newly-added
    // stream and returns HTTP 400 to the admin API caller, which the
    // Python client then interprets as a failed registration and
    // falls back to direct camera URLs. Omitting the key entirely
    // lets go2rtc create the streams map fresh in block style on
    // its first PUT.
    let body = "\
log:
  level: info
api:
  listen: 127.0.0.1:1984
rtsp:
  listen: 127.0.0.1:8554
";
    std::fs::write(&path, body)?;
    Ok(path)
}

/// Poll `GET /api/streams` on the go2rtc admin API until it answers
/// 200 OK or we exhaust the attempt budget. Returns true on success.
async fn wait_for_go2rtc_ready() -> bool {
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_millis(500))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            log::error!("failed to build reqwest client for go2rtc: {e}");
            return false;
        }
    };
    let url = format!("{GO2RTC_API_BASE}/api/streams");
    for attempt in 0..GO2RTC_HEALTH_ATTEMPTS {
        if let Ok(resp) = client.get(&url).send().await {
            if resp.status().is_success() {
                log::info!(
                    "go2rtc ready after {} attempts ({}ms budget used)",
                    attempt + 1,
                    (attempt as u128 + 1) * GO2RTC_HEALTH_INTERVAL.as_millis()
                );
                return true;
            }
        }
        async_runtime::spawn_blocking(|| std::thread::sleep(GO2RTC_HEALTH_INTERVAL))
            .await
            .ok();
    }
    log::error!(
        "go2rtc did not become ready within {}s",
        (GO2RTC_HEALTH_ATTEMPTS as u64 * GO2RTC_HEALTH_INTERVAL.as_millis() as u64) / 1000
    );
    false
}

/// Spawn the bundled go2rtc sidecar. Best-effort: any failure logs at
/// WARN and returns Err — the caller (setup hook) treats that as a
/// graceful fallback signal and continues without go2rtc, in which
/// case Python falls back to direct camera URLs.
///
/// Returns Ok(()) only after `wait_for_go2rtc_ready()` confirms the
/// admin API answers, so by the time this returns the Python sidecar
/// can immediately start POSTing streams.
fn spawn_go2rtc(app: &AppHandle, data_dir: &std::path::Path) -> Result<(), String> {
    let config_path = write_go2rtc_config(data_dir)
        .map_err(|e| format!("could not write go2rtc.yaml: {e}"))?;

    let sidecar = app
        .shell()
        .sidecar("go2rtc")
        .map_err(|e| format!("go2rtc sidecar binary not found: {e}"))?
        .args(["-c", config_path.to_string_lossy().as_ref()]);

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn go2rtc: {e}"))?;

    // Stash the child so the shutdown hook + the fallback path below
    // can both reach it.
    {
        let state = app.state::<BackendState>();
        state.go2rtc_child.lock().unwrap().replace(child);
    }

    // ── go2rtc stdout/stderr reader ────────────────────────────────
    let app_handle = app.clone();
    async_runtime::spawn(async move {
        let state = app_handle.state::<BackendState>();
        let stderr_tail = state.go2rtc_stderr_tail.clone();
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes);
                    log::info!("go2rtc: {}", line.trim_end());
                }
                CommandEvent::Stderr(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes).into_owned();
                    log::warn!("go2rtc stderr: {}", line.trim_end());
                    let mut tail = stderr_tail.lock().unwrap();
                    if tail.len() == STDERR_RING_CAPACITY {
                        tail.pop_front();
                    }
                    tail.push_back(line);
                }
                CommandEvent::Terminated(payload) => {
                    log::error!("go2rtc terminated: {payload:?}");
                    // Don't crash the app — Phase 1 graceful-fallback
                    // contract: if go2rtc dies, Python keeps recording
                    // (it will fall back to direct camera URLs on the
                    // next stream open). A future phase will add
                    // restart logic.
                    break;
                }
                _ => {}
            }
        }
    });

    // Block until the admin API answers, on the runtime that spawned us.
    let ready = async_runtime::block_on(wait_for_go2rtc_ready());
    if !ready {
        // Kill the half-started child so it doesn't dangle.
        if let Some(child) = app
            .state::<BackendState>()
            .go2rtc_child
            .lock()
            .unwrap()
            .take()
        {
            let _ = child.kill();
        }
        return Err("go2rtc spawned but admin API never answered".to_string());
    }
    Ok(())
}

/// Spawn the bundled `simplenvr-backend` Python sidecar with all the
/// environment variables it needs and start the stdout/stderr reader
/// + no-port-signal watchdog tasks. The `go2rtc_enabled` flag controls
/// whether we hand the Python side the loopback URLs — if false,
/// Python falls back to direct camera RTSP connections.
fn spawn_sidecar(app: &AppHandle, go2rtc_enabled: bool) -> Result<(), String> {
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("could not resolve app data dir: {e}"))?;
    if let Err(e) = std::fs::create_dir_all(&data_dir) {
        log::warn!("could not pre-create data dir {data_dir:?}: {e}");
    }

    let ffmpeg = external_bin_path("ffmpeg");
    let ffprobe = external_bin_path("ffprobe");

    let mut sidecar = app
        .shell()
        .sidecar("simplenvr-backend")
        .map_err(|e| format!("sidecar not found: {e}"))?
        .env("SIMPLENVR_FFMPEG_BIN", ffmpeg.as_os_str())
        .env("SIMPLENVR_FFPROBE_BIN", ffprobe.as_os_str())
        .env("SIMPLENVR_DATA_DIR", data_dir.as_os_str());

    if go2rtc_enabled {
        // Python's go2rtc_client + codec.py read these to know where
        // to POST stream configs and how to build loopback input URLs.
        // Absence of these vars is the signal to fall back to direct
        // camera connections.
        sidecar = sidecar
            .env("SIMPLENVR_GO2RTC_URL", GO2RTC_API_BASE)
            .env("SIMPLENVR_GO2RTC_RTSP_URL", GO2RTC_RTSP_BASE);
    }

    let sidecar = sidecar
        // Orphan protection. The Python sidecar (backend/main.py)
        // spawns a daemon thread that blocks on stdin.read(); when the
        // Tauri parent dies, the OS closes the pipe and read() returns
        // EOF, triggering os._exit(0). Gated by env var so a bare
        // `python -m backend.main` from a terminal does NOT EOF on
        // start.
        //
        // CRITICAL: tauri-plugin-shell's sidecar spawn keeps stdin
        // piped by default and we never close it from the Rust side.
        // The pipe stays open for the lifetime of the Tauri parent.
        .env("SIMPLENVR_STDIN_WATCHDOG", "1");

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar: {e}"))?;

    // Stash the child immediately so the shutdown hook can kill it
    // even if the readiness handshake below fails.
    {
        let state = app.state::<BackendState>();
        state.child.lock().unwrap().replace(child);
    }

    // ── stdout/stderr reader ────────────────────────────────────────
    let app_handle = app.clone();
    async_runtime::spawn(async move {
        let state = app_handle.state::<BackendState>();
        let got_port_signal = state.got_port_signal.clone();
        let stderr_tail = state.stderr_tail.clone();

        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes);
                    let trimmed = line.trim();
                    let already_signalled = *got_port_signal.lock().unwrap();
                    if !already_signalled {
                        if let Some(port) = parse_ready_line(trimmed) {
                            log::info!("backend ready signal received: port {port}");
                            *app_handle.state::<BackendState>().port.lock().unwrap() = port;
                            *got_port_signal.lock().unwrap() = true;
                            spawn_health_poll(app_handle.clone(), port);
                        }
                    }
                }
                CommandEvent::Stderr(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes).into_owned();
                    log::warn!("backend stderr: {line}");
                    let mut tail = stderr_tail.lock().unwrap();
                    if tail.len() == STDERR_RING_CAPACITY {
                        tail.pop_front();
                    }
                    tail.push_back(line);
                }
                CommandEvent::Terminated(payload) => {
                    log::error!("backend terminated unexpectedly: {payload:?}");
                    // If we haven't received the ready signal yet,
                    // this is a hard startup failure — surface to the
                    // user and exit. If we already had a healthy port,
                    // the user is in mid-session and we just log;
                    // restart logic is a Phase 9.5 follow-up.
                    if !*got_port_signal.lock().unwrap() {
                        crash_and_exit(
                            &app_handle,
                            "SimpleNVR backend failed to start",
                            &format!(
                                "The recording backend exited before signalling readiness (code: {:?}).",
                                payload.code
                            ),
                            &stderr_tail,
                        );
                    }
                    break;
                }
                _ => {}
            }
        }
    });

    // ── no-port-signal watchdog ─────────────────────────────────────
    let app_handle_wd = app.clone();
    async_runtime::spawn(async move {
        async_runtime::spawn_blocking(|| std::thread::sleep(PORT_SIGNAL_TIMEOUT))
            .await
            .ok();
        let state = app_handle_wd.state::<BackendState>();
        if !*state.got_port_signal.lock().unwrap() {
            log::error!(
                "backend produced no port signal within {}s of spawn",
                PORT_SIGNAL_TIMEOUT.as_secs()
            );
            let stderr_tail = state.stderr_tail.clone();
            crash_and_exit(
                &app_handle_wd,
                "SimpleNVR backend failed to start",
                &format!(
                    "The recording backend did not produce a startup signal within {} seconds.",
                    PORT_SIGNAL_TIMEOUT.as_secs()
                ),
                &stderr_tail,
            );
        }
    });

    Ok(())
}

/// Parse a line of sidecar stdout. Returns `Some(port)` if it matches
/// the `{"port": N, "ready": true}` ready-signal contract from
/// `backend/main.py`.
fn parse_ready_line(line: &str) -> Option<u16> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    if !value.get("ready")?.as_bool()? {
        return None;
    }
    let port = value.get("port")?.as_u64()?;
    u16::try_from(port).ok()
}

/// Poll `GET /api/health` on `127.0.0.1:<port>` up to HEALTH_ATTEMPTS
/// times. Emits `backend://ready` with the port on success, or shows
/// the crash dialog and exits on timeout.
fn spawn_health_poll(app: AppHandle, port: u16) {
    async_runtime::spawn(async move {
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_millis(750))
            .build()
        {
            Ok(c) => c,
            Err(e) => {
                log::error!("failed to build reqwest client: {e}");
                return;
            }
        };
        let url = format!("http://127.0.0.1:{port}/api/health");

        for attempt in 0..HEALTH_ATTEMPTS {
            match client.get(&url).send().await {
                Ok(resp) if resp.status().is_success() => {
                    log::info!("backend health check passed on attempt {}", attempt + 1);
                    let _ = app.emit("backend://ready", port);
                    return;
                }
                Ok(resp) => {
                    log::debug!(
                        "health check attempt {}: status {}",
                        attempt + 1,
                        resp.status()
                    );
                }
                Err(e) => {
                    log::debug!("health check attempt {} failed: {e}", attempt + 1);
                }
            }
            async_runtime::spawn_blocking(|| std::thread::sleep(HEALTH_INTERVAL))
                .await
                .ok();
        }

        log::error!(
            "backend failed health check after {HEALTH_ATTEMPTS} attempts on port {port}"
        );
        let _ = app.emit("backend://failed", port);
        let stderr_tail = app.state::<BackendState>().stderr_tail.clone();
        crash_and_exit(
            &app,
            "SimpleNVR backend not responding",
            &format!(
                "The recording backend did not respond on port {port} within {} seconds.",
                (HEALTH_ATTEMPTS as u64 * HEALTH_INTERVAL.as_millis() as u64) / 1000
            ),
        &stderr_tail,
        );
    });
}

/// Show a blocking error dialog with the last few stderr lines, then
/// exit the process. Used for any unrecoverable startup failure.
fn crash_and_exit(app: &AppHandle, title: &str, body: &str, stderr_tail: &StderrRing) {
    let tail = stderr_tail.lock().unwrap();
    let stderr_block = if tail.is_empty() {
        String::from("(no stderr captured)")
    } else {
        tail.iter()
            .map(|s| s.trim_end().to_string())
            .collect::<Vec<_>>()
            .join("\n")
    };
    drop(tail);

    let full_body = format!(
        "{body}\n\n\
         Last backend output:\n\
         ────────────────────\n\
         {stderr_block}\n\
         ────────────────────\n\n\
         SimpleNVR will now exit. Please relaunch to try again."
    );

    // blocking_show must run on a thread that can drive the OS event
    // loop on macOS. The dialog plugin handles main-thread dispatch
    // internally, but blocking_show on Tauri 2 returns once the user
    // dismisses, so we can call exit immediately after.
    app.dialog()
        .message(full_body)
        .title(title)
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::Ok)
        .blocking_show();

    std::process::exit(1);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendState {
            port: Mutex::new(DEV_FALLBACK_PORT),
            child: Mutex::new(None),
            got_port_signal: Arc::new(Mutex::new(false)),
            stderr_tail: Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_RING_CAPACITY))),
            go2rtc_child: Mutex::new(None),
            go2rtc_stderr_tail: Arc::new(Mutex::new(VecDeque::with_capacity(
                STDERR_RING_CAPACITY,
            ))),
        })
        .invoke_handler(tauri::generate_handler![get_backend_port])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // ── Phase 1 startup order ──────────────────────────────
            // 1. Spawn go2rtc and wait for its admin API.
            // 2. Then spawn the Python sidecar — go2rtc must be up
            //    first so backend.main's startup can register every
            //    camera via the HTTP admin API.
            // If go2rtc fails (binary missing in dev, or refuses to
            // start), we fall back to direct camera RTSP connections
            // by spawning Python without the SIMPLENVR_GO2RTC_* env
            // vars. Recording continues to work — go2rtc is an
            // optimisation, not a load-bearing dependency in Phase 1.
            let data_dir = app
                .handle()
                .path()
                .app_data_dir()
                .ok();
            let go2rtc_enabled = match data_dir.as_deref() {
                Some(dir) => match spawn_go2rtc(app.handle(), dir) {
                    Ok(()) => {
                        log::info!("go2rtc sidecar ready on {GO2RTC_API_BASE}");
                        true
                    }
                    Err(e) => {
                        log::warn!(
                            "go2rtc unavailable ({e}); SimpleNVR will use direct camera connections"
                        );
                        false
                    }
                },
                None => {
                    log::warn!("no app data dir; skipping go2rtc spawn");
                    false
                }
            };

            // Try to spawn the bundled Python sidecar. If the binary
            // is missing (rare — only happens in `cargo run` before
            // Phase 7's PyInstaller output exists), keep the hardcoded
            // DEV_FALLBACK_PORT and assume the developer is running
            // `python -m backend.main` in a terminal. We also mark
            // got_port_signal=true so the watchdog doesn't fire.
            match spawn_sidecar(app.handle(), go2rtc_enabled) {
                Ok(()) => log::info!("sidecar spawned, awaiting ready signal"),
                Err(e) => {
                    log::warn!(
                        "no sidecar in this build ({e}); using dev fallback port {DEV_FALLBACK_PORT}"
                    );
                    *app.state::<BackendState>().got_port_signal.lock().unwrap() = true;
                }
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::ExitRequested { .. } = event {
                let state = app_handle.state::<BackendState>();
                let py_child = state.child.lock().unwrap().take();
                let go_child = state.go2rtc_child.lock().unwrap().take();
                drop(state);
                if let Some(child) = py_child {
                    if let Err(e) = child.kill() {
                        log::warn!("failed to kill backend sidecar: {e}");
                    }
                }
                if let Some(child) = go_child {
                    if let Err(e) = child.kill() {
                        log::warn!("failed to kill go2rtc sidecar: {e}");
                    }
                }
            }
        });
}
