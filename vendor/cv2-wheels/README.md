# Self-built cv2 wheels

LGPL-clean `opencv-python-headless` wheels — one per OS/arch, built with `-DWITH_FFMPEG=OFF` so they do not bundle `libavcodec`/`libx264`/`libx265`. Committed in-tree so a fresh `pip install -r backend/requirements.txt` on any supported platform pulls an LGPL-clean cv2.

See `docs/cv2-selfbuild.md` for the full rationale and regeneration instructions. Regenerate via `scripts/build_cv2_wheel.{sh,ps1}` — the output lands here, replacing the prior wheel for the same OS/arch.

**Platform coverage today:**
- [x] macOS arm64
- [ ] Linux x64
- [x] Windows x64
- [ ] Windows arm64

Platforms without a committed wheel fall through to stock PyPI at `pip install` time. The bundle scripts (`scripts/bundle_python.{sh,ps1}`) refuse to ship a bundle containing GPL FFmpeg deps — a platform without a vendored wheel cannot produce a shippable bundle.
