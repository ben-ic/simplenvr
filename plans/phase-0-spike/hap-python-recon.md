# HAP-python reconnaissance (2026-04-18)

## Verdict

**USABLE WITH CAVEATS.** HAP-python is actively maintained, `pyhap.camera.Camera` is a complete HAP camera accessory implementation, and Home Assistant — the largest consumer, with 100k+ installs — pins it at the current version and contributes fixes upstream. The caveats: (1) the bundled `FFMPEG_CMD` default is a macOS `avfoundation` demo not suitable for production; you must supply your own ffmpeg command; (2) audio requires an external RTP clock-rate proxy (Home Assistant ships its own `homekit-audio-proxy` package); (3) ffmpeg is the only production reference path — examples are minimal.

## Latest version + commit cadence

- **PyPI:** `HAP-python==5.0.0`, released **2025-08-25** (source: https://pypi.org/pypi/HAP-python/json).
- **Repo:** `github.com/ikalchev/HAP-python`, 661 stars, 123 forks, not archived (https://api.github.com/repos/ikalchev/HAP-python).
- **Cadence:** commits across 2024 and 2025 — v4.9.2 on 2024-11-03, v5.0.0 on 2025-08-25 (dropped Python 3.7/3.8, modernized packaging). Issue #490 "Python 3.14" opened 2026-01-06 and still open, but the repo is alive.
- **Top contributors:** ikalchev (245 commits), **bdraco (123 commits)** — the Home Assistant maintainer. cdce8p (60) is also HA. Effective co-maintenance by the HA core team (https://api.github.com/repos/ikalchev/HAP-python/contributors).

## Camera API (from `pyhap/camera.py` @ master, 980 lines)

Source: https://raw.githubusercontent.com/ikalchev/HAP-python/master/pyhap/camera.py

`class Camera(Accessory)` constructor signature:
```python
Camera(options: dict, driver: AccessoryDriver, display_name: str)
```

`options` keys (from docstring at line 383):
- `video.codec.profiles` / `video.codec.levels` (H.264 enum bytes)
- `video.resolutions` — list of `[width, height, fps]`
- `audio.codecs` — list of `{type: 'OPUS'|'AAC-eld', samplerate: int}`
- `address` — IP the camera streams FROM (use `pyhap.util.get_local_address()`)
- `srtp: bool` (default False)
- `start_stream_cmd: str` — ffmpeg template (defaults to a macOS avfoundation demo, see below)
- `stream_count: int` — number of concurrent streams (default 1)

**Methods to override** (documented "For client extensions" at line 840):
- `async start_stream(session_info, stream_config) -> bool` — default spawns `start_stream_cmd.format(**stream_config)` as an asyncio subprocess and stores PID in `session_info["process"]`.
- `async stop_stream(session_info)` — default terminates `session_info["process"]` with 2s grace timeout then SIGKILL.
- `async reconfigure_stream(session_info, stream_config)` — default just re-calls `start_stream`.
- `get_snapshot(image_size) -> bytes` — default returns a bundled placeholder JPEG. **Must override** for real snapshot support (iOS preview thumbnails).

The `stream_config` dict handed to `start_stream` contains everything negotiated with iOS: `address`, `v_port`, `v_srtp_key` (base64 AES_CM_128_HMAC_SHA1_80 key||salt), `v_ssrc`, `v_max_bitrate`, `v_max_mtu`, `width`, `height`, `fps`, `v_profile_id`, `v_level`, and matching `a_*` for audio.

## Bundled camera example

`camera_main.py` at repo root (73 lines): https://raw.githubusercontent.com/ikalchev/HAP-python/master/camera_main.py — constructs `options` dict with resolutions/codecs and spawns a `Camera` on port 51826. No RTSP input; the default `FFMPEG_CMD` (line 218 of `camera.py`) uses `-f avfoundation -framerate {fps} -i 0:0` (macOS webcam). Unusable as-is for an RTSP NVR.

**Production reference** — Home Assistant's `type_cameras.py` (https://github.com/home-assistant/core/blob/dev/homeassistant/components/homekit/type_cameras.py). Its `VIDEO_OUTPUT` template for HAP SRTP:
```
-map {v_map} -an -c:v {v_codec} {v_profile}
-tune zerolatency -pix_fmt yuv420p -r {fps}
-b:v {v_max_bitrate}k -bufsize {v_bufsize}k -maxrate {v_max_bitrate}k
-payload_type 99 -ssrc {v_ssrc} -f rtp
-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key}
srtp://{address}:{v_port}?rtcpport={v_port}&localrtpport={v_port}&pkt_size={v_pkt_size}
```
Audio is routed through a local UDP proxy (`homekit-audio-proxy==1.2.1`) because ffmpeg's Opus RTP clock is hardcoded at 48kHz but HomeKit negotiates 16/24kHz — the proxy rewrites timestamps. If we want HomeKit audio, we pip-install this same package.

## Known issues / limitations (2024-2026)

Open issues (https://github.com/ikalchev/HAP-python/issues):
- **#472** "Streaming With SRTP Question" (2024-07) — open, unanswered by maintainer.
- **#446** "Camera 2-way Audio" (2023-07) — not supported; microphone works, speaker back-channel does not.
- **#409** "No camera response" (2022) — lone reproduction of the "preview works, tap shows No Response" behavior; zero comments, probably a user-side ffmpeg config issue since HA has no such bug.
- **#395** "USB camera on RPi" (2021) — community-driven ffmpeg tuning questions.

**iOS 17/18:** Issue #455 ("Stopped working after upgrading to iOS 17") was **resolved in v4.9.0 on 2023-10-15** — the fix was in the protocol handler (PRs #464, #465), not the camera code. No open iOS-18-specific regression. Home Assistant shipped HAP-python 5.0.0 against iOS 18/26 without a camera-breaking bug report in the HA tracker. Pairing + streaming from iOS 17/18 is the happy path.

## Pip install

Minimal camera-capable deps:
```
pip install "HAP-python[qrcode]==5.0.0" homekit-audio-proxy==1.2.1
# Runtime system deps: ffmpeg binary on PATH
```

The library itself pulls: `cryptography`, `chacha20poly1305-reuseable`, `orjson>=3.7.2`, `zeroconf>=0.36.2`, `h11`, plus `base36` and `pyqrcode` from the `[qrcode]` extra. No C compilation beyond what `cryptography` already needs. No pyperclip, no native deps beyond those.

## Alternative forks

**None better.** bdraco's fork `bdraco/ha-HAP-python` last updated 2024-11-03 and is effectively merged upstream — he commits directly to `ikalchev/HAP-python`. All other forks (kormax, balloob, maximkulkin) are stale (2019-2023) and have <5 stars. Upstream is the canonical home.

## Commercial / polished use

- **Home Assistant** — the HomeKit Bridge integration ships HAP-python 5.0.0 for millions of users including camera support. Proves camera accessories work end-to-end with iOS 17/18 at scale. (https://github.com/home-assistant/core/blob/dev/homeassistant/components/homekit/manifest.json)
- **Scrypted** — does NOT use HAP-python. Its HomeKit plugin is TypeScript/Node on top of `@koush/werift` (https://github.com/koush/scrypted/tree/main/plugins/homekit).
- **go2rtc** — HomeKit mode is a Go implementation (`internal/homekit/`), not HAP-python.

So for Python-based NVRs, HAP-python is effectively the only game in town, and Home Assistant is the proof that the camera surface is production-ready when paired with a proper ffmpeg config.
