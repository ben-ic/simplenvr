fn main() {
    // Expose the rustc target triple to the crate as env!("TARGET_TRIPLE").
    // Cargo passes TARGET to build scripts but not to the crate itself, so
    // we forward it through cargo:rustc-env. Used to locate externalBin
    // sidecars in dev builds (binaries/<name>-<triple>).
    let target = std::env::var("TARGET").expect("cargo always sets TARGET for build scripts");
    println!("cargo:rustc-env=TARGET_TRIPLE={target}");

    // Point the linker at our bundled libmpv so it doesn't need Homebrew.
    // scripts/fetch_mpv.sh (macOS) or scripts/fetch_mpv.ps1 (Windows)
    // downloads the pre-built library into src-tauri/lib/.
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let lib_dir = std::path::Path::new(&manifest_dir).join("lib");
    if lib_dir.exists() {
        println!("cargo:rustc-link-search=native={}", lib_dir.display());
    }

    // Set rpath so the app finds libmpv.2.dylib in Frameworks/ at runtime.
    #[cfg(target_os = "macos")]
    println!("cargo:rustc-link-arg=-Wl,-rpath,@executable_path/../Frameworks");

    tauri_build::build()
}
