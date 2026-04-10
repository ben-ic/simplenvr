#!/usr/bin/env bash
# Download (or build) LGPL-only static FFmpeg + ffprobe and place into
# src-tauri/binaries/ under Tauri sidecar naming (binary-<triple>[.exe]).
#
# Usage:
#   scripts/fetch_ffmpeg.sh                  # auto-detect host triple
#   scripts/fetch_ffmpeg.sh --target <triple>
#   scripts/fetch_ffmpeg.sh --all            # download all 4 targets
#
# Sources:
#   macOS  (aarch64/x86_64-apple-darwin) — built from FFmpeg source with
#                                          LGPL-only ./configure flags. No
#                                          prebuilt LGPL macOS distribution
#                                          exists; evermeet/osxexperts ship
#                                          --enable-gpl builds.
#   Windows x86_64                        — BtbN/FFmpeg-Builds LGPL static
#   Linux   x86_64                        — BtbN/FFmpeg-Builds LGPL static
#
# Aborts if any installed ffmpeg reports --enable-gpl / --enable-libx264 etc.
# in its `configuration:` line. SHA256 is verified against pinned values for
# downloaded artifacts.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${REPO_ROOT}/src-tauri/binaries"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# --- Pinned versions and SHA256 hashes -------------------------------------
# Update these in lockstep when bumping versions.

# FFmpeg source tarball used for the macOS LGPL build.
FFMPEG_SRC_VERSION="8.1"
FFMPEG_SRC_URL="https://ffmpeg.org/releases/ffmpeg-${FFMPEG_SRC_VERSION}.tar.xz"
# Computed by `shasum -a 256` on first download. Pinned for reproducibility.
FFMPEG_SRC_SHA256="b072aed6871998cce9b36e7774033105ca29e33632be5b6347f3206898e0756a"

# BtbN/FFmpeg-Builds — LGPL static, pinned to a specific dated autobuild.
# The `latest` tag is a rolling release whose content changes daily, breaking
# SHA256 verification. To bump FFmpeg: pick a recent autobuild tag from
# https://github.com/BtbN/FFmpeg-Builds/releases, update the tag + filenames
# + hashes below (and in fetch_ffmpeg.ps1 in lockstep).
BTBN_TAG="autobuild-2026-04-10-13-42"
BTBN_BASE="https://github.com/BtbN/FFmpeg-Builds/releases/download/${BTBN_TAG}"
BTBN_WIN_URL="${BTBN_BASE}/ffmpeg-N-123888-g25e187f849-win64-lgpl.zip"
BTBN_WIN_SHA256="a6c43cc952ed923ea2486adad6f577591cea1b90097b50e0964357ed82b199b6"
BTBN_WINARM64_URL="${BTBN_BASE}/ffmpeg-N-123888-g25e187f849-winarm64-lgpl.zip"
BTBN_WINARM64_SHA256="02a6649c8bfff0e124ae20a7ccf72b47fb9c76ca573c82ff1ece4e517d3ce082"
BTBN_LINUX_URL="${BTBN_BASE}/ffmpeg-N-123888-g25e187f849-linux64-lgpl.tar.xz"
BTBN_LINUX_SHA256="c0715418de3bc601ceb0f63aa00241743d30ddf113d4ad0c3060bc7e5cb610eb"

# --- Helpers ---------------------------------------------------------------

log()  { printf '[fetch_ffmpeg] %s\n' "$*"; }
die()  { printf '[fetch_ffmpeg] ERROR: %s\n' "$*" >&2; exit 1; }

detect_triple() {
    local kernel arch
    kernel="$(uname -s)"
    arch="$(uname -m)"
    case "${kernel}/${arch}" in
        Darwin/arm64)   echo "aarch64-apple-darwin" ;;
        Darwin/x86_64)  echo "x86_64-apple-darwin" ;;
        Linux/x86_64)   echo "x86_64-unknown-linux-gnu" ;;
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
        log "         Edit scripts/fetch_ffmpeg.sh and pin this hash."
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

verify_lgpl() {
    # Run the binary and grep its configuration: line for GPL flags.
    # Only valid for binaries that match the host architecture.
    local bin="$1"
    local cfg
    if ! cfg="$("${bin}" -version 2>/dev/null | grep -i '^[ ]*configuration:' || true)"; then
        die "could not run ${bin} -version"
    fi
    if [ -z "${cfg}" ]; then
        die "${bin} produced no configuration: line"
    fi
    log "${bin}: ${cfg}"
    case "${cfg}" in
        *--enable-gpl*)        die "GPL contamination: ${bin} has --enable-gpl" ;;
        *--enable-libx264*)    die "GPL contamination: ${bin} has --enable-libx264" ;;
        *--enable-libx265*)    die "GPL contamination: ${bin} has --enable-libx265" ;;
        *--enable-libfdk-aac*) die "non-redistributable: ${bin} has --enable-libfdk-aac" ;;
    esac
    log "${bin}: LGPL OK (no GPL flags)"
}

# --- Per-target installers -------------------------------------------------

# Build FFmpeg from source for macOS with LGPL-only options. The default
# ./configure is already LGPL — we explicitly disable GPL and nonfree to
# guarantee no x264/x265/fdk-aac slip in even if a future contributor edits
# the script. videotoolbox is enabled because backend/recording/codec.py
# selects h264_videotoolbox for re-encode.
build_macos() {
    local triple="$1"
    local arch
    case "${triple}" in
        aarch64-apple-darwin) arch="arm64" ;;
        x86_64-apple-darwin)  arch="x86_64" ;;
        *) die "build_macos: bad triple ${triple}" ;;
    esac

    local host_arch
    host_arch="$(uname -m)"
    if [ "${host_arch}" = "arm64" ]   && [ "${arch}" = "x86_64" ] || \
       [ "${host_arch}" = "x86_64" ] && [ "${arch}" = "arm64" ]; then
        log "WARNING: cross-arch build (host ${host_arch} → ${arch}); will use --enable-cross-compile"
    fi

    local src="${TMP_DIR}/ffmpeg-src.tar.xz"
    download "${FFMPEG_SRC_URL}" "${src}"
    verify_sha "${src}" "${FFMPEG_SRC_SHA256}" "ffmpeg-source"

    local build="${TMP_DIR}/build-${arch}"
    mkdir -p "${build}"
    tar -xJf "${src}" -C "${build}"
    local srcdir
    srcdir="$(find "${build}" -maxdepth 1 -type d -name 'ffmpeg-*' -print -quit)"
    [ -n "${srcdir}" ] || die "could not find extracted ffmpeg source"

    local prefix="${build}/install"
    mkdir -p "${prefix}"

    local arch_flags=""
    if [ "${arch}" != "${host_arch}" ]; then
        arch_flags="--enable-cross-compile --arch=${arch} --target-os=darwin"
    fi
    # FFmpeg's x86 asm requires nasm, which isn't part of Xcode CLT. Disable
    # it for x86_64 builds — we lose some hand-optimized SIMD but our
    # workload is stream-copy + videotoolbox, neither of which exercises
    # libavcodec/libswscale x86 asm paths meaningfully.
    if [ "${arch}" = "x86_64" ] && ! command -v nasm >/dev/null 2>&1; then
        arch_flags="${arch_flags} --disable-x86asm"
    fi

    log "configuring ffmpeg ${FFMPEG_SRC_VERSION} for ${triple} (LGPL-only)…"
    (
        cd "${srcdir}"
        # shellcheck disable=SC2086
        ./configure \
            --prefix="${prefix}" \
            --disable-gpl \
            --disable-nonfree \
            --disable-debug \
            --disable-doc \
            --disable-ffplay \
            --disable-shared \
            --enable-static \
            --enable-videotoolbox \
            --enable-audiotoolbox \
            --extra-cflags="-arch ${arch}" \
            --extra-ldflags="-arch ${arch}" \
            --pkg-config-flags=--static \
            ${arch_flags} >/dev/null
        log "compiling (this can take several minutes)…"
        make -j"$(sysctl -n hw.ncpu 2>/dev/null || echo 4)" >/dev/null
        make install >/dev/null
    )

    install -m 0755 "${prefix}/bin/ffmpeg"  "${BIN_DIR}/ffmpeg-${triple}"
    install -m 0755 "${prefix}/bin/ffprobe" "${BIN_DIR}/ffprobe-${triple}"
}

install_btbn_windows() {
    # Handles both x86_64-pc-windows-msvc and aarch64-pc-windows-msvc.
    # BtbN publishes separate zips under the same release, identical
    # layout (bin/ffmpeg.exe + bin/ffprobe.exe).
    local triple="$1"
    local url sha
    case "${triple}" in
        x86_64-pc-windows-msvc)
            url="${BTBN_WIN_URL}"
            sha="${BTBN_WIN_SHA256}"
            ;;
        aarch64-pc-windows-msvc)
            url="${BTBN_WINARM64_URL}"
            sha="${BTBN_WINARM64_SHA256}"
            ;;
        *) die "install_btbn_windows: unsupported triple ${triple}" ;;
    esac

    local zip="${TMP_DIR}/ffmpeg-${triple}.zip"
    download "${url}" "${zip}"
    verify_sha "${zip}" "${sha}" "ffmpeg(${triple})"

    local extract="${TMP_DIR}/win-${triple}"
    mkdir -p "${extract}"
    (cd "${extract}" && unzip -oq "${zip}")
    local bindir
    bindir="$(find "${extract}" -type d -name bin -print -quit)"
    [ -n "${bindir}" ] || die "couldn't find bin/ inside BtbN ${triple} zip"

    install -m 0755 "${bindir}/ffmpeg.exe"  "${BIN_DIR}/ffmpeg-${triple}.exe"
    install -m 0755 "${bindir}/ffprobe.exe" "${BIN_DIR}/ffprobe-${triple}.exe"
}

install_btbn_linux() {
    local triple="x86_64-unknown-linux-gnu"
    local tar="${TMP_DIR}/ffmpeg-linux.tar.xz"
    download "${BTBN_LINUX_URL}" "${tar}"
    verify_sha "${tar}" "${BTBN_LINUX_SHA256}" "ffmpeg(${triple})"

    local extract="${TMP_DIR}/lin"
    mkdir -p "${extract}"
    tar -xJf "${tar}" -C "${extract}"
    local bindir
    bindir="$(find "${extract}" -type d -name bin -print -quit)"
    [ -n "${bindir}" ] || die "couldn't find bin/ inside BtbN linux tar"

    install -m 0755 "${bindir}/ffmpeg"  "${BIN_DIR}/ffmpeg-${triple}"
    install -m 0755 "${bindir}/ffprobe" "${BIN_DIR}/ffprobe-${triple}"
}

install_target() {
    local triple="$1"
    case "${triple}" in
        aarch64-apple-darwin|x86_64-apple-darwin) build_macos "${triple}" ;;
        x86_64-pc-windows-msvc|aarch64-pc-windows-msvc)
            install_btbn_windows "${triple}" ;;
        x86_64-unknown-linux-gnu)                 install_btbn_linux ;;
        *) die "unknown target triple: ${triple}" ;;
    esac
}

is_native() {
    local triple="$1"
    local host
    host="$(detect_triple)"
    [ "${triple}" = "${host}" ]
}

# --- Main ------------------------------------------------------------------

TARGETS=()
case "${1:-}" in
    --all)
        TARGETS=(aarch64-apple-darwin x86_64-apple-darwin
                 x86_64-pc-windows-msvc aarch64-pc-windows-msvc
                 x86_64-unknown-linux-gnu)
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

for triple in "${TARGETS[@]}"; do
    log "=== ${triple} ==="
    install_target "${triple}"
    ff="${BIN_DIR}/ffmpeg-${triple}"
    fp="${BIN_DIR}/ffprobe-${triple}"
    case "${triple}" in
        *-pc-windows-*) ff="${ff}.exe"; fp="${fp}.exe" ;;
    esac
    if is_native "${triple}"; then
        verify_lgpl "${ff}"
        "${fp}" -version | head -1
    else
        log "skipping LGPL runtime check for non-native target ${triple}"
    fi
done

log "done. Binaries in ${BIN_DIR}:"
ls -la "${BIN_DIR}"
