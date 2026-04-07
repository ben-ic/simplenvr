use std::path::PathBuf;
use std::sync::Mutex;
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

/// Health-check polling: 20 attempts × 250 ms = 5 s budget for the
/// FastAPI lifespan to come up after the stdout ready signal.
const HEALTH_ATTEMPTS: u32 = 20;
const HEALTH_INTERVAL: Duration = Duration::from_millis(250);

struct BackendState {
    port: Mutex<u16>,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn get_backend_port(state: State<'_, BackendState>) -> u16 {
    *state.port.lock().unwrap()
}

/// Resolve the on-disk path to a Tauri-bundled `externalBin` binary.
///
/// In a bundled `.app` / `.exe`, Tauri places external binaries next to
/// the main executable with the target-triple suffix stripped. In a
/// `cargo build` / `cargo run` debug workflow, the files live in
/// `src-tauri/binaries/<name>-<triple>` and are NOT copied next to the
/// debug binary, so we resolve them via `CARGO_MANIFEST_DIR`.
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

/// Spawn the bundled `simplenvr-backend` Python sidecar with the
/// environment variables it needs to find ffmpeg/ffprobe and its data
/// directory, then return a receiver for its stdout/stderr.
fn spawn_sidecar(app: &AppHandle) -> Result<(), String> {
    // Where the Python backend should store SQLite + recordings. Tauri
    // resolves this to ~/Library/Application Support/com.simplenvr.app
    // on macOS, %APPDATA%\com.simplenvr.app on Windows. Creating the
    // directory up-front avoids the Python `init_db` racing the OS.
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("could not resolve app data dir: {e}"))?;
    if let Err(e) = std::fs::create_dir_all(&data_dir) {
        log::warn!("could not pre-create data dir {data_dir:?}: {e}");
    }

    let ffmpeg = external_bin_path("ffmpeg");
    let ffprobe = external_bin_path("ffprobe");

    let sidecar = app
        .shell()
        .sidecar("simplenvr-backend")
        .map_err(|e| format!("sidecar not found: {e}"))?
        .env("SIMPLENVR_FFMPEG_BIN", ffmpeg.as_os_str())
        .env("SIMPLENVR_FFPROBE_BIN", ffprobe.as_os_str())
        .env("SIMPLENVR_DATA_DIR", data_dir.as_os_str())
        // Orphan protection: backend's stdin watchdog thread exits the
        // Python process the moment Tauri closes the pipe (i.e. we die
        // or get force-quit). Gated by an env var because a bare
        // `python -m backend.main` from a terminal must NOT EOF on
        // start. See backend/main.py:112.
        .env("SIMPLENVR_STDIN_WATCHDOG", "1");

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar: {e}"))?;

    // Stash the child immediately so the shutdown hook can kill it even
    // if the readiness handshake below fails.
    app.state::<BackendState>()
        .child
        .lock()
        .unwrap()
        .replace(child);

    let app_handle = app.clone();
    async_runtime::spawn(async move {
        let mut got_ready = false;
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes);
                    let trimmed = line.trim();
                    if !got_ready {
                        if let Some(port) = parse_ready_line(trimmed) {
                            log::info!("backend ready signal received: port {port}");
                            *app_handle.state::<BackendState>().port.lock().unwrap() = port;
                            got_ready = true;
                            // Phase 9: confirm /api/health is actually
                            // serving before we surface the port to the
                            // webview. The stdout signal fires once
                            // uvicorn picks the port; the FastAPI
                            // lifespan (DB init, recorder/scanner
                            // startup) may still be in flight.
                            spawn_health_poll(app_handle.clone(), port);
                        }
                    }
                }
                CommandEvent::Stderr(line_bytes) => {
                    log::warn!("backend stderr: {}", String::from_utf8_lossy(&line_bytes));
                }
                CommandEvent::Terminated(payload) => {
                    log::error!("backend terminated unexpectedly: {payload:?}");
                    show_crash_dialog(
                        &app_handle,
                        "SimpleNVR backend stopped",
                        &format!(
                            "The recording backend exited unexpectedly (code: {:?}).\n\n\
                             SimpleNVR will not work until you restart the app.",
                            payload.code
                        ),
                    );
                    break;
                }
                _ => {}
            }
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
/// times. Emits `backend://ready` with the port on success, or shows a
/// crash dialog and emits `backend://failed` on timeout.
fn spawn_health_poll(app: AppHandle, port: u16) {
    async_runtime::spawn(async move {
        for attempt in 0..HEALTH_ATTEMPTS {
            // Use blocking IO on a worker so we don't depend on a
            // tokio-aware HTTP client. The poll wakes every 250ms which
            // is fine for a health check on a localhost socket.
            let ok = async_runtime::spawn_blocking(move || health_check_once(port))
                .await
                .unwrap_or(false);
            if ok {
                log::info!("backend health check passed on attempt {}", attempt + 1);
                let _ = app.emit("backend://ready", port);
                return;
            }
            async_runtime::spawn_blocking(|| std::thread::sleep(HEALTH_INTERVAL))
                .await
                .ok();
        }
        log::error!(
            "backend failed health check after {HEALTH_ATTEMPTS} attempts on port {port}"
        );
        let _ = app.emit("backend://failed", port);
        show_crash_dialog(
            &app,
            "SimpleNVR backend not responding",
            &format!(
                "The recording backend did not respond on port {port} within {} seconds.\n\n\
                 Please restart SimpleNVR. If this keeps happening, check the logs.",
                (HEALTH_ATTEMPTS as u64 * HEALTH_INTERVAL.as_millis() as u64) / 1000
            ),
        );
    });
}

/// Single blocking health check: open a TCP connection to
/// `127.0.0.1:<port>`, send a hand-crafted `GET /api/health HTTP/1.1`,
/// return true iff the response status is 200.
///
/// Hand-rolled rather than depending on `reqwest` because the request
/// is fixed, the target is loopback (no TLS, no DNS), and adding a
/// real HTTP client would pull in ~50 transitive crates.
fn health_check_once(port: u16) -> bool {
    use std::io::{Read, Write};
    use std::net::{SocketAddr, TcpStream};

    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(200)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(200)));

    let req =
        b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
    if stream.write_all(req).is_err() {
        return false;
    }

    let mut buf = [0u8; 64];
    match stream.read(&mut buf) {
        Ok(n) if n > 0 => {
            // Status line is "HTTP/1.1 200 OK\r\n" — first 15 bytes
            // contain the code.
            let head = &buf[..n.min(64)];
            head.windows(3).any(|w| w == b"200")
        }
        _ => false,
    }
}

/// Show a non-blocking error dialog. Used both for hard sidecar
/// crashes and for health-check timeouts.
fn show_crash_dialog(app: &AppHandle, title: &str, body: &str) {
    app.dialog()
        .message(body)
        .title(title)
        .kind(MessageDialogKind::Error)
        .buttons(MessageDialogButtons::Ok)
        .show(|_| {});
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendState {
            port: Mutex::new(DEV_FALLBACK_PORT),
            child: Mutex::new(None),
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

            // Try to spawn the bundled sidecar. If the binary is
            // missing (rare — should only happen in `cargo run` before
            // Phase 7's PyInstaller output exists) we keep the
            // hardcoded DEV_FALLBACK_PORT and assume the developer is
            // running `python -m backend.main` in a terminal.
            match spawn_sidecar(app.handle()) {
                Ok(()) => log::info!("sidecar spawned, awaiting ready signal"),
                Err(e) => log::warn!(
                    "no sidecar in this build ({e}); using dev fallback port {DEV_FALLBACK_PORT}"
                ),
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::ExitRequested { .. } = event {
                if let Some(child) = app_handle
                    .state::<BackendState>()
                    .child
                    .lock()
                    .unwrap()
                    .take()
                {
                    if let Err(e) = child.kill() {
                        log::warn!("failed to kill backend sidecar: {e}");
                    }
                }
            }
        });
}
