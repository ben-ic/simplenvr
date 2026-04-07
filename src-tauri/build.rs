fn main() {
    // Expose the rustc target triple to the crate as env!("TARGET_TRIPLE").
    // Cargo passes TARGET to build scripts but not to the crate itself, so
    // we forward it through cargo:rustc-env. Used to locate externalBin
    // sidecars in dev builds (binaries/<name>-<triple>).
    let target = std::env::var("TARGET").expect("cargo always sets TARGET for build scripts");
    println!("cargo:rustc-env=TARGET_TRIPLE={target}");

    tauri_build::build()
}
