fn main() {
    // `get_auth_token` is the first app-defined command on this shell. Tauri
    // v2's ACL gates app commands the same as plugin commands: without this
    // declaration (which autogenerates an `allow-get-auth-token` permission,
    // referenced by capabilities/default.json), `invoke("get_auth_token")`
    // is denied at the capability layer before src/main.rs's handler ever runs.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(&["get_auth_token"])),
    )
    .expect("failed to run tauri-build");
}
