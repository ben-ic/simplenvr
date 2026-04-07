use std::sync::Mutex;
use tauri::State;

struct BackendState {
    port: Mutex<u16>,
}

#[tauri::command]
fn get_backend_port(state: State<'_, BackendState>) -> u16 {
    *state.port.lock().unwrap()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendState {
            port: Mutex::new(57321),
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
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
