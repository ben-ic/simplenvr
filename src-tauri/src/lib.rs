use std::sync::Mutex;

use tauri::{async_runtime, Emitter, Manager, RunEvent, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Default port the backend falls back to in dev mode when no sidecar
/// binary is bundled. Must match `backend.config.DEFAULT_START_PORT`.
const DEV_FALLBACK_PORT: u16 = 57321;

struct BackendState {
    port: Mutex<u16>,
    child: Mutex<Option<CommandChild>>,
}

#[tauri::command]
fn get_backend_port(state: State<'_, BackendState>) -> u16 {
    *state.port.lock().unwrap()
}

/// Try to spawn the bundled `simplenvr-backend` sidecar and read its
/// stdout for the `{"port": N, "ready": true}` ready-signal line.
///
/// Returns `Ok(())` if the sidecar was spawned and reached the ready
/// state. Returns `Err(_)` if the sidecar binary is missing (typical in
/// dev — developer is running `python -m backend.main` manually) or if
/// it crashed before signalling ready.
fn spawn_sidecar(app: &tauri::AppHandle) -> Result<(), String> {
    let sidecar = app
        .shell()
        .sidecar("simplenvr-backend")
        .map_err(|e| format!("sidecar not found: {e}"))?;

    let (mut rx, child) = sidecar
        .spawn()
        .map_err(|e| format!("failed to spawn sidecar: {e}"))?;

    // Stash the child immediately so we can kill it on shutdown even if
    // the readiness handshake below fails.
    app.state::<BackendState>()
        .child
        .lock()
        .unwrap()
        .replace(child);

    // Read stdout lines until we see the ready JSON or the stream
    // closes. We do this synchronously on a worker thread off the
    // tauri-plugin-shell async task; setup() returns immediately.
    let app_handle = app.clone();
    async_runtime::spawn(async move {
        // tauri-plugin-shell delivers stdout/stderr as line-buffered
        // CommandEvent chunks on Tauri's tokio runtime.
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line_bytes) => {
                    let line = String::from_utf8_lossy(&line_bytes);
                    let trimmed = line.trim();
                    if let Some(port) = parse_ready_line(trimmed) {
                        log::info!("backend ready on port {port}");
                        *app_handle.state::<BackendState>().port.lock().unwrap() = port;
                        let _ = app_handle.emit("backend://ready", port);
                        // Keep draining so the pipe never fills, but
                        // the readiness handshake is done.
                    }
                }
                CommandEvent::Stderr(line_bytes) => {
                    log::warn!("backend stderr: {}", String::from_utf8_lossy(&line_bytes));
                }
                CommandEvent::Terminated(payload) => {
                    log::error!("backend terminated: {payload:?}");
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
/// `backend/main.py`, or `None` for any other line.
fn parse_ready_line(line: &str) -> Option<u16> {
    let value: serde_json::Value = serde_json::from_str(line).ok()?;
    let ready = value.get("ready")?.as_bool()?;
    if !ready {
        return None;
    }
    let port = value.get("port")?.as_u64()?;
    u16::try_from(port).ok()
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

            // Try to spawn the bundled sidecar. In dev (no bundled
            // binary) this fails fast and we keep the hardcoded
            // DEV_FALLBACK_PORT — developer is expected to be running
            // `python -m backend.main` in a separate terminal.
            match spawn_sidecar(app.handle()) {
                Ok(()) => log::info!("sidecar spawned, awaiting ready signal"),
                Err(e) => log::info!(
                    "no sidecar in this build ({e}); using dev fallback port {DEV_FALLBACK_PORT}"
                ),
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            if let RunEvent::ExitRequested { .. } = event {
                // Best-effort: kill the sidecar so it doesn't outlive
                // the window. CommandChild::kill consumes self, so we
                // take() it out of the Mutex.
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
