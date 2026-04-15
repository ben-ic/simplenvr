#!/usr/bin/env bash
# Download go2rtc release binaries and place into src-tauri/binaries/
# under Tauri sidecar naming (go2rtc-<triple>[.exe]).
#
# Usage:
#   scripts/fetch_go2rtc.sh                  # auto-detect host triple
#   scripts/fetch_go2rtc.sh --target <triple>
#   scripts/fetch_go2rtc.sh --all            # download all 6 targets
#
# go2rtc is MIT-licensed (https://github.com/AlexxIT/go2rtc). Bundled
# upstream LICENSE is mirrored to src-tauri/resources/go2rtc-LICENSE.
#
# SHA256 is verified against pinned values. On first run for a new
# version, set the relevant *_SHA256 var to __PIN_AFTER_FIRST_RUN__,
# run the script, copy the printed hash back, and re-run.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${REPO_ROOT}/src-tauri/binaries"
RES_DIR="${REPO_ROOT}/src-tauri/resources"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# --- Pinned versions and SHA256 hashes -------------------------------------

GO2RTC_VERSION="v1.9.14"
GO2RTC_BASE="https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}"

# Per-asset URLs and pinned hashes. Fill in after first run.
GO2RTC_MAC_ARM64_URL="${GO2RTC_BASE}/go2rtc_mac_arm64.zip"
GO2RTC_MAC_ARM64_SHA256="919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c"

GO2RTC_MAC_AMD64_URL="${GO2RTC_BASE}/go2rtc_mac_amd64.zip"
GO2RTC_MAC_AMD64_SHA256="9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a"

GO2RTC_WIN64_URL="${GO2RTC_BASE}/go2rtc_win64.zip"
GO2RTC_WIN64_SHA256="dd4167d75cb04abe618855b7c71f8658bd009f60c1a71835d134d2c11c939907"

GO2RTC_WIN_ARM64_URL="${GO2RTC_BASE}/go2rtc_win_arm64.zip"
GO2RTC_WIN_ARM64_SHA256="814be0f6d8669025c7bccdd1f026ffaf613abae5352239f4ec84de543b94594a"

# Linux is shipped as a raw binary, not a zip.
GO2RTC_LINUX_AMD64_URL="${GO2RTC_BASE}/go2rtc_linux_amd64"
GO2RTC_LINUX_AMD64_SHA256="32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6"
GO2RTC_LINUX_ARM64_URL="${GO2RTC_BASE}/go2rtc_linux_arm64"
GO2RTC_LINUX_ARM64_SHA256="359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50"

# Upstream LICENSE — pulled from the matching tag.
GO2RTC_LICENSE_URL="https://raw.githubusercontent.com/AlexxIT/go2rtc/${GO2RTC_VERSION}/LICENSE"

# --- Helpers ---------------------------------------------------------------

log()  { printf '[fetch_go2rtc] %s\n' "$*"; }
die()  { printf '[fetch_go2rtc] ERROR: %s\n' "$*" >&2; exit 1; }

detect_triple() {
    local kernel arch
    kernel="$(uname -s)"
    arch="$(uname -m)"
    case "${kernel}/${arch}" in
        Darwin/arm64)   echo "aarch64-apple-darwin" ;;
        Darwin/x86_64)  echo "x86_64-apple-darwin" ;;
        Linux/x86_64)   echo "x86_64-unknown-linux-gnu" ;;
        Linux/aarch64)  echo "aarch64-unknown-linux-gnu" ;;
        *) die "unsupported host: ${kernel}/${arch}" ;;
    esac
}

sha256_of() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

verify_sha() {
    local file="$1" expected="$2" name="$3"
    local actual
    actual="$(sha256_of "${file}")"
    if [ "${expected}" = "__PIN_AFTER_FIRST_RUN__" ]; then
        log "WARNING: ${name} SHA256 not yet pinned. Computed: ${actual}"
        log "         Edit scripts/fetch_go2rtc.sh and pin this hash."
        return 0
    fi
    if [ "${actual}" != "${expected}" ]; then
        die "${name} SHA256 mismatch: expected ${expected}, got ${actual}"
    fi
    log "${name} SHA256 OK"
}

download() {
    local url="$1" dest="$2"
    log "downloading ${url}"
    curl -fSL --retry 3 -o "${dest}" "${url}"
}

# Extract the single `go2rtc*` binary from a zip and install it under
# the Tauri sidecar naming convention. Handles both Mach-O (no .exe)
# and PE (.exe) outputs.
install_zip_binary() {
    local zip="$1" triple="$2" want_exe="$3"
    local extract="${TMP_DIR}/extract-${triple}"
    rm -rf "${extract}"
    mkdir -p "${extract}"
    (cd "${extract}" && unzip -oq "${zip}")

    local found
    if [ "${want_exe}" = "1" ]; then
        found="$(find "${extract}" -type f -name 'go2rtc*.exe' -print -quit)"
    else
        # Mach-O / ELF — match a file whose name starts with go2rtc and
        # has no .exe suffix. The release zips for mac arm64/amd64 ship
        # a single binary named go2rtc_mac_arm64 / go2rtc_mac_amd64.
        found="$(find "${extract}" -type f -name 'go2rtc*' ! -name '*.exe' -print -quit)"
    fi
    [ -n "${found}" ] || die "couldn't find go2rtc binary inside ${zip}"

    local dest="${BIN_DIR}/go2rtc-${triple}"
    [ "${want_exe}" = "1" ] && dest="${dest}.exe"
    install -m 0755 "${found}" "${dest}"
    log "installed ${dest}"
}

install_target() {
    local triple="$1"
    case "${triple}" in
        aarch64-apple-darwin)
            local zip="${TMP_DIR}/go2rtc_mac_arm64.zip"
            download "${GO2RTC_MAC_ARM64_URL}" "${zip}"
            verify_sha "${zip}" "${GO2RTC_MAC_ARM64_SHA256}" "go2rtc(${triple})"
            install_zip_binary "${zip}" "${triple}" 0
            ;;
        x86_64-apple-darwin)
            local zip="${TMP_DIR}/go2rtc_mac_amd64.zip"
            download "${GO2RTC_MAC_AMD64_URL}" "${zip}"
            verify_sha "${zip}" "${GO2RTC_MAC_AMD64_SHA256}" "go2rtc(${triple})"
            install_zip_binary "${zip}" "${triple}" 0
            ;;
        x86_64-pc-windows-msvc)
            local zip="${TMP_DIR}/go2rtc_win64.zip"
            download "${GO2RTC_WIN64_URL}" "${zip}"
            verify_sha "${zip}" "${GO2RTC_WIN64_SHA256}" "go2rtc(${triple})"
            install_zip_binary "${zip}" "${triple}" 1
            ;;
        aarch64-pc-windows-msvc)
            local zip="${TMP_DIR}/go2rtc_win_arm64.zip"
            download "${GO2RTC_WIN_ARM64_URL}" "${zip}"
            verify_sha "${zip}" "${GO2RTC_WIN_ARM64_SHA256}" "go2rtc(${triple})"
            install_zip_binary "${zip}" "${triple}" 1
            ;;
        x86_64-unknown-linux-gnu)
            local raw="${TMP_DIR}/go2rtc_linux_amd64"
            download "${GO2RTC_LINUX_AMD64_URL}" "${raw}"
            verify_sha "${raw}" "${GO2RTC_LINUX_AMD64_SHA256}" "go2rtc(${triple})"
            install -m 0755 "${raw}" "${BIN_DIR}/go2rtc-${triple}"
            log "installed ${BIN_DIR}/go2rtc-${triple}"
            ;;
        aarch64-unknown-linux-gnu)
            local raw="${TMP_DIR}/go2rtc_linux_arm64"
            download "${GO2RTC_LINUX_ARM64_URL}" "${raw}"
            verify_sha "${raw}" "${GO2RTC_LINUX_ARM64_SHA256}" "go2rtc(${triple})"
            install -m 0755 "${raw}" "${BIN_DIR}/go2rtc-${triple}"
            log "installed ${BIN_DIR}/go2rtc-${triple}"
            ;;
        *) die "unknown target triple: ${triple}" ;;
    esac
}

is_native() {
    local triple="$1"
    local host
    host="$(detect_triple)"
    [ "${triple}" = "${host}" ]
}

install_license() {
    mkdir -p "${RES_DIR}"
    local dest="${RES_DIR}/go2rtc-LICENSE"
    if [ -f "${dest}" ]; then
        return
    fi
    log "fetching upstream LICENSE for ${GO2RTC_VERSION}"
    curl -fSL --retry 3 -o "${dest}" "${GO2RTC_LICENSE_URL}"
}

# --- Main ------------------------------------------------------------------

TARGETS=()
case "${1:-}" in
    --all)
        TARGETS=(aarch64-apple-darwin x86_64-apple-darwin
                 x86_64-pc-windows-msvc aarch64-pc-windows-msvc
                 x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu)
        ;;
    --target)
        [ -n "${2:-}" ] || die "--target requires an argument"
        TARGETS=("$2")
        ;;
    "")
        TARGETS=("$(detect_triple)")
        ;;
    -h|--help)
        sed -n '1,20p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *) die "unknown argument: $1" ;;
esac

mkdir -p "${BIN_DIR}"
install_license

for triple in "${TARGETS[@]}"; do
    log "=== ${triple} ==="
    install_target "${triple}"
    bin="${BIN_DIR}/go2rtc-${triple}"
    case "${triple}" in
        *windows*) bin="${bin}.exe" ;;
    esac
    if is_native "${triple}"; then
        log "running native sanity check: ${bin} --version"
        "${bin}" --version 2>&1 | head -3 || log "WARNING: --version exited non-zero"
    else
        log "skipping runtime check for non-native target ${triple}"
        file "${bin}" 2>/dev/null || true
    fi
done

log "done. go2rtc binaries in ${BIN_DIR}:"
ls -la "${BIN_DIR}" | grep go2rtc || true
