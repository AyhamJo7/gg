//! GG Orchestrator Tauri shell.
//!
//! Responsibilities:
//! - spawn the Python backend (`gg-backend`) as a sidecar process on startup
//! - wait for the health endpoint before loading the UI
//! - terminate the backend cleanly on exit
//!
//! NOTE: requires a Rust toolchain (`rustup`) to build — see DEVELOPMENT.md.

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::Duration;

struct BackendState(Mutex<Option<Child>>);

fn spawn_backend() -> Option<Child> {
    // The backend is expected on PATH (installed via `uv tool install` or run
    // from the repo venv). If it cannot be spawned the UI still loads and the
    // user can start the backend manually — connection errors are visible in-app.
    let child = Command::new("gg-backend")
        .env("GG_SERVER_PORT", "8787")
        .spawn();
    match child {
        Ok(c) => Some(c),
        Err(e) => {
            eprintln!("gg-backend spawn failed: {e} — start it manually with `make backend`");
            None
        }
    }
}

/// Exposed to the frontend via Tauri IPC (`invoke("get_auth_token")`) — not a
/// network route, so it is unreachable from arbitrary web content the way an
/// HTTP endpoint would be (see backend/src/orchestrator/api/auth.py, which
/// deliberately has no such route). `spawn_backend` above does not set
/// `.current_dir()` on the child, so `gg-backend` inherits this process's cwd
/// and writes its token file relative to it — reading the same relative path
/// here always finds it.
#[tauri::command]
fn get_auth_token() -> Result<String, String> {
    let path = std::env::current_dir()
        .map_err(|e| e.to_string())?
        .join(".orchestrator")
        .join("auth_token");
    std::fs::read_to_string(&path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("failed to read auth token at {}: {e}", path.display()))
}

fn wait_for_health(attempts: u32) -> bool {
    for _ in 0..attempts {
        if let Ok(resp) = ureq::get("http://127.0.0.1:8787/api/health")
            .timeout(Duration::from_millis(500))
            .call()
        {
            if resp.status() == 200 {
                return true;
            }
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

fn main() {
    let backend = spawn_backend();
    if backend.is_some() {
        let healthy = wait_for_health(40); // ~10s
        if !healthy {
            eprintln!("backend did not become healthy in time");
        }
    }
    tauri::Builder::default()
        .manage(BackendState(Mutex::new(backend)))
        .invoke_handler(tauri::generate_handler![get_auth_token])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.state::<BackendState>();
                if let Some(mut child) = state.0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error running GG Orchestrator");
}
