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

    // macOS: make dev/runtime lookup deterministic for libmpv.
    #[cfg(target_os = "macos")]
    {
        use std::path::PathBuf;

        // Set rpath so the app finds libmpv.2.dylib in Frameworks/ at runtime.
        println!("cargo:rustc-link-arg=-Wl,-rpath,@executable_path/../Frameworks");

        // Cargo puts the debug app binary in src-tauri/target/<profile>/app,
        // and dyld resolves @executable_path/../Frameworks to
        // src-tauri/target/Frameworks. Our fetch script downloads libmpv into
        // src-tauri/lib (link time) and src-tauri/Frameworks (bundle time),
        // so copy it into target/Frameworks for dev runs as well.
        let libmpv_src = lib_dir.join("libmpv.2.dylib");
        if libmpv_src.exists() {
            let out_dir = PathBuf::from(
                std::env::var("OUT_DIR").expect("cargo always sets OUT_DIR in build scripts"),
            );

            // OUT_DIR looks like: target/<profile>/build/<pkg-hash>/out
            let target_profile_dir = out_dir.ancestors().nth(3).map(PathBuf::from);
            let target_dir = out_dir.ancestors().nth(4).map(PathBuf::from);

            if let Some(target_dir) = target_dir {
                let frameworks_dir = target_dir.join("Frameworks");
                let _ = std::fs::create_dir_all(&frameworks_dir);
                let dst = frameworks_dir.join("libmpv.2.dylib");
                if let Err(err) = std::fs::copy(&libmpv_src, &dst) {
                    panic!(
                        "failed to copy {} to {}: {err}",
                        libmpv_src.display(),
                        dst.display()
                    );
                }
            }

            // Also copy next to target/<profile>/ for fallback search paths.
            if let Some(profile_dir) = target_profile_dir {
                let dst = profile_dir.join("libmpv.2.dylib");
                let _ = std::fs::copy(&libmpv_src, &dst);
            }
        } else {
            println!(
                "cargo:warning=libmpv.2.dylib not found at {}. Run scripts/fetch_mpv.sh",
                libmpv_src.display()
            );
        }
    }

    tauri_build::build()
}
